"""工作流拓扑 Schema 服务(设计《工作流流程图与实时节点追踪》§6.2-A / §4.3)。

职责与边界
----------
- **唯一事实来源是编译后的 LangGraph 图**:节点与边一律程序化提取,不在本模块
  维护第二份边定义(设计 §11.6)。
- **边真源是 ``compiled_graph.builder``**(产生该编译图的 StateGraph)的
  ``edges`` / ``waiting_edges`` / ``branches`` 三件套,而**不是**
  ``compiled_graph.get_graph()``。2026-11-01 实测(langgraph 1.2.9,
  ``langgraph/pregel/_draw.py``):``get_graph()`` 是**静态模拟 + 画图启发式**,
  会 (a) 给所有"只入不出"的 termini 节点**合成** ``→__end__`` 边(storyline
  死胡同/侧支节点各多一条),并 (b) **漏掉**"有出边的节点的普通边直连 END"
  (``storyline_join→END``)。真实边数与画图边数因此不一致,偏差规则已在
  ``tests/integration/test_workflow_graph_schema.py`` 固化为 langgraph 行为护栏。
- 本模块是**纯函数层**:输入编译图 + config 模块,输出 §4.3 的图 JSON dict。
  不接 API、不碰前端、不触发任何图执行(阶段 4 再由 FastAPI router 消费)。
- 元数据(中文显示名 / 分组 / 描述 / 关卡编号 / 条件边标签)来自本模块内的
  静态注册表 —— 注册表只补**展示信息**,不补拓扑;拓扑漂移由
  ``tests/integration/test_workflow_graph_schema.py`` 防守。
- START/END 归一化:``__start__``/``__end__`` 输出为 ``START``/``END``。

输出的节点字段(§4.3 + 两处扩展):

    id / name / group / description / kind / checkpoint / enabled / reachable
    + modes        —— 该节点归属哪条链:2026-11-04 起图收敛为 auto-only
                      单流水线,非 terminal 节点一律 ["auto"];START/END 为 []
    + waiting_kind —— 人工等待类别(阶段3 删除 import_video 后,当前图内
                      无该类节点;字段仅当注册表声明时透出)

`reachable` 语义:从 START 出发、沿编译图全部边(含条件边 —— 条件边**可能**被
选中)BFS 可达。``ASSEMBLY_QC_GATE_ENABLED=False`` 时 6 个 assembly 节点注册
无边,天然不可达 —— 与设计 §6.2-A 的"注册但未连通"一致,无需特判。

`enabled` 语义:本构建下是否可能参与执行(与 reachable 互补):
    - assembly 6 节点:跟随 ``ASSEMBLY_QC_GATE_ENABLED``;
    - 其余:True。

用法::

    from graph import build_graph, get_checkpointer
    from monitoring.workflow_graph import build_graph_schema

    async with get_checkpointer() as saver:
        g = build_graph(checkpointer=saver)
    schema = build_graph_schema(g)          # config 缺省取 config 模块
"""

from __future__ import annotations

from collections import deque
from typing import Any, Optional

__all__ = [
    "WORKFLOW_ID",
    "SCHEMA_VERSION",
    "CHECKPOINT_MARKS",
    "build_graph_schema",
    "node_display_names",
]

WORKFLOW_ID = "auto_video_editor_main"
#: schema 结构版本;布局缓存 key 纳入它(设计 §6.2-F 第 7 条)。改字段结构时 +1。
#: 2026-11-10 前置化阶段3/4:web_topic 拓扑移位(group_clips 条件前置)→ bump 2,
#: 否则浏览器布局缓存仍按 v1 旧图渲染(设计方案 §5.8)。
#: 2026-11-12 noop 治理阶段1:模板推荐节点前置为 generate_script 主链节点、
#: 条件路由改挂其出口 → bump 3(拓扑变更,布局缓存同步失效)。
#: 2026-11-12 noop 治理阶段3(方案A):render_video 展示名改"故事线预览"
#: (真实 ffmpeg 预览,非最终导出)→ bump 4(仅节点元数据文案,无拓扑变更,
#: bump 只为让布局缓存里的旧名失效)。
SCHEMA_VERSION = 4

#: 关卡编号全集(阶段3 删除关卡⓪后剩 ①②③)
CHECKPOINT_MARKS = ("①", "②", "③")

QC_FLAG = "ASSEMBLY_QC_GATE_ENABLED"

_ASSEMBLY_NODES = (
    "assembly_discover_and_probe",
    "assembly_visual_observe",
    "assembly_build_timeline",
    "assembly_validate_render_qc",
    "assembly_repair_loop",
    "assembly_write_report",
)

# ---------------------------------------------------------------------------
# 节点元数据注册表(只补展示信息,不补拓扑)
# ---------------------------------------------------------------------------
# node_id -> {name, group, description, checkpoint(可选), waiting_kind(可选)}
_NODE_META: dict[str, dict[str, Any]] = {
    # ---- 终端(START/END 归一化目标) ----
    "START": {"name": "开始", "group": "terminal", "description": "LangGraph 工作流入口"},
    "END": {"name": "结束", "group": "terminal", "description": "LangGraph 工作流出口"},
    # ---- 草稿 ----
    "generate_draft": {
        "name": "生成剪映草稿",
        "group": "draft",
        "description": "生成初始剪映草稿;storyline 链与主图链在此衔接",
    },
    # ---- assembly QC 通道(ASSEMBLY_QC_GATE_ENABLED 控制) ----
    "assembly_discover_and_probe": {
        "name": "素材发现与探测",
        "group": "assembly_qc",
        "description": "扫描源视频与渲染产物,做 ffprobe 探测",
    },
    "assembly_visual_observe": {
        "name": "视觉观察",
        "group": "assembly_qc",
        "description": "对草稿素材做关键帧抽帧与 contact sheet 观察",
    },
    "assembly_build_timeline": {
        "name": "装配时间线",
        "group": "assembly_qc",
        "description": "根据观察结果装配时间线草案",
    },
    "assembly_validate_render_qc": {
        "name": "渲染与质量检查",
        "group": "assembly_qc",
        "description": "检查时间线、渲染产物及质量指标;条件边决定修复回路或写报告",
    },
    "assembly_repair_loop": {
        "name": "QC 修复回路",
        "group": "assembly_qc",
        "description": "按 QC 结论执行修复动作,复检走回 assembly_validate_render_qc",
    },
    "assembly_write_report": {
        "name": "写 QC 报告",
        "group": "assembly_qc",
        "description": "达标/达重试上限后写质检报告,交回关卡①",
    },
    # ---- 人工关卡与护栏 ----
    "node_06_human_reorder": {
        "name": "人工调整镜头顺序①",
        "group": "edit_review",
        "description": "interrupt:关卡①,人工在剪映里调整镜头顺序",
        "checkpoint": "①",
    },
    "node_07_speed_fit": {
        "name": "时长适配(35s)",
        "group": "edit_review",
        "description": "把成片总时长压到目标以内;未达标自环重试,超限升级护栏",
    },
    "escalate_guardrail_failure": {
        "name": "护栏失败升级",
        "group": "edit_review",
        "description": "node_07 连续重试超限后的兜底:写 error_log 并终止",
    },
    "bridge_snapshot2": {
        "name": "桥接快照2",
        "group": "edit_review",
        "description": "条件边不能改 state,由本节点把 snapshot2 路径写入 state",
    },
    # ---- 字幕 / 封面 / 英文线 ----
    "node_08_add_subtitles": {
        "name": "添加中文字幕",
        "group": "subtitles_covers",
        "description": "中文字幕原生落草稿唯一写入口+中文配音权威轨重写(旁白逐句、时间取自当前草稿,2026-11-09 阶段1/2);配音摘要含 provider/stub 统计;英文线 16a 依赖它",
    },
    "node_08b_add_subtitles_en": {
        "name": "添加英文字幕",
        "group": "subtitles_covers",
        "description": "英文字幕写入英文分支草稿",
    },
    "node_10_inject_text_fx": {
        "name": "注入中文花字",
        "group": "subtitles_covers",
        "description": "中文字幕入场动画原生落草稿(material_animations+轨段引用,只动我方字幕)",
    },
    "node_10b_inject_text_fx_en": {
        "name": "注入英文花字",
        "group": "subtitles_covers",
        "description": "英文字幕入场动画原生落草稿;先于关卡③落地,人工校对看到最终效果(K3=B)",
    },
    "node_14_make_covers": {
        "name": "生成封面",
        "group": "subtitles_covers",
        "description": "生成中文封面",
    },
    "node_15_localize_covers_en": {
        "name": "封面英文本地化",
        "group": "subtitles_covers",
        "description": "封面英文化;与 node_17 列表 fan-in 汇合后才触发尾链",
    },
    "node_16a_translate_and_check": {
        "name": "翻译与布局检测",
        "group": "subtitles_covers",
        "description": "翻译字幕并做布局检测;命中问题才触发关卡③",
    },
    "node_checkpoint3_layout_review": {
        "name": "布局人工复核③",
        "group": "subtitles_covers",
        "description": "interrupt:关卡③,仅布局检测命中时触发;未命中走旁路",
        "checkpoint": "③",
    },
    "node_17_inject_english_tts": {
        "name": "注入英文 TTS",
        "group": "subtitles_covers",
        "description": "调 FireRedTTS2 生成英文配音并写入英文分支草稿;与 node_15 列表 fan-in 汇合后才触发尾链",
    },
    "fork_draft_for_english_branch": {
        "name": "复制英文分支草稿",
        "group": "subtitles_covers",
        "description": "从中文主草稿复制出英文分支草稿",
    },
    # ---- 尾链 / 交付 ----
    "node_09_inject_fx": {
        "name": "注入特效",
        "group": "fx_delivery",
        "description": "转场/视频特效原生绑定到视频段(消费转场推荐计划,边界=段数-1);中英两份草稿同时处理",
    },
    "node_11_inject_sticker": {
        "name": "注入贴纸",
        "group": "fx_delivery",
        "description": "贴纸原生注入(占位资源 fail-closed 不落草稿,实机回填真实 ID 后生效);中英两份草稿同时处理",
    },
    "node_12_human_add_bgm": {
        "name": "人工添加 BGM②",
        "group": "fx_delivery",
        "description": "interrupt:关卡②,人工选曲一次;post_resume 记录人工稿 SHA+按角色合并到英文草稿(含伴随素材,保留英文配音轨)",
        "checkpoint": "②",
    },
    "node_13_adjust_volume": {
        "name": "音量调节",
        "group": "fx_delivery",
        "description": "分角色音量(main/bgm/voiceover 按轨名识别)+ 原生 audio_fades 淡入淡出绑定(不删轨不覆盖人工 BGM)",
    },
    "join_before_delivery": {
        "name": "交付前 QA 闸门",
        "group": "fx_delivery",
        "description": "交付前 QA;单前驱,不再承担 fan-in;2026-11-07 起做双语深度 QA(draft_integrity)",
    },
    "export_final_video": {
        "name": "最终 MP4 导出",
        "group": "fx_delivery",
        "description": "可选:FFmpeg 渲染中文主草稿为 MP4(阶段 6;默认关闭记 skipped)",
    },
    "delivery_qa_repair": {
        "name": "交付 QA 定向修复",
        "group": "fx_delivery",
        "description": "阶段 6:required 类别 failed/degraded 时重跑幂等修复节点(转场/动画/贴纸/淡入淡出),回 QA 复检",
    },
    "delivery_qa_failed": {
        "name": "交付 QA 终止",
        "group": "fx_delivery",
        "description": "阶段 6:复检仍失败 → raise 终止为 run.failed(不允许静默 END)",
    },
    # ---- storyline(auto 链) ----
    "storyline_load_media": {
        "name": "载入媒体",
        "group": "storyline_media",
        "description": "auto 链起点(START 直连);解析/导入源视频素材",
    },
    "storyline_search_web_topic": {
        "name": "网页话题检索",
        "group": "storyline_media",
        "description": "group_clips 后条件前置节点:WEB_TOPIC 开启且能构造出检索词时先检索再文案;产物经 web_research 独立上下文进入 generate_script",
    },
    "storyline_split_shots": {
        "name": "分镜切分",
        "group": "storyline_media",
        "description": "按镜头切分素材;切完静态直连 understand_clips(B1 ASR 移除)",
    },
    "storyline_understand_clips": {
        "name": "理解片段",
        "group": "storyline_understand",
        "description": "对片段做语义理解,产出理解摘要",
    },
    "storyline_filter_clips": {
        "name": "过滤片段",
        "group": "storyline_understand",
        "description": "按质量/相关性过滤片段",
    },
    "storyline_group_clips": {
        "name": "片段分组",
        "group": "storyline_understand",
        "description": "qa 重试回路的目标节点;分组产物供文案/时间线使用",
    },
    "storyline_generate_script": {
        "name": "生成文案",
        "group": "storyline_script",
        "description": "按分组与话题生成长文案;并行 fan-out 出模板/配音/BGM/文本四路",
    },
    "storyline_script_template_recommendation": {
        "name": "文案模板推荐",
        "group": "storyline_script",
        "description": "文案生成前置:检索模板库并选出风格参考,供 generate_script 注入(2026-11-12 起为主链节点)",
    },
    "storyline_generate_voiceover": {
        "name": "生成配音",
        "group": "storyline_script",
        "description": "生成配音时长参考,供 BGM 与时间线对齐",
    },
    "storyline_select_bgm": {
        "name": "选择 BGM",
        "group": "storyline_script",
        "description": "等配音完成后选择 BGM",
    },
    "storyline_recommend_transition": {
        "name": "推荐转场",
        "group": "storyline_script",
        "description": "等 BGM 完成后推荐转场",
    },
    "storyline_recommend_text": {
        "name": "推荐文本",
        "group": "storyline_script",
        "description": "推荐字幕样式(P4 起经 node_08/08b 落到草稿)",
    },
    "storyline_plan_timeline_pro": {
        "name": "时间线规划",
        "group": "storyline_timeline",
        "description": "4 源屏障(分组/文案/配音/BGM)后规划时间线",
    },
    "storyline_generate_ai_transition": {
        "name": "生成 AI 转场",
        "group": "storyline_timeline",
        "description": "与 plan_timeline_pro 共用同一道 4 源屏障;按 STORYLINE_ENABLE_AI_TRANSITION 接边",
    },
    "storyline_plan_timeline_ai_transition": {
        "name": "AI 转场时间线规划",
        "group": "storyline_timeline",
        "description": "屏障 [plan_timeline_pro, generate_ai_transition] 之后;qa 校验含 AI 转场的最终时间线",
    },
    "storyline_render_video": {
        "name": "故事线预览",
        "group": "storyline_render",
        "description": "QA 通过后用现有 FFmpeg 导出链生成初始时间线预览 MP4(方案A;最终导出仍归 node_18)",
    },
    "storyline_qa_gate": {
        "name": "剧情 QA 闸门",
        "group": "storyline_render",
        "description": "3 源屏障后校验;未过回 group_clips 重试,通过走渲染",
    },
    "storyline_join": {
        "name": "剧情结果汇合",
        "group": "storyline_render",
        "description": "反序列化规划产物;有 storyline_plan 进 generate_draft,无则 END",
    },
}


def node_display_names() -> dict[str, str]:
    """``node_id -> 中文显示名`` 只读视图(轻量消费者用,不触发图编译)。

    与 :func:`build_graph_schema` 输出的 ``nodes[].name`` 同一事实源(都是
    ``_NODE_META``);未收录的节点由调用方回退展示(拓扑真源是编译图)。
    """
    return {node_id: meta["name"] for node_id, meta in _NODE_META.items()}

# ---------------------------------------------------------------------------
# 条件边标签注册表:(from, to) -> 标签(归一化后的 id;START/END 用大写)
# ---------------------------------------------------------------------------
_EDGE_LABELS: dict[tuple[str, str], str] = {
    ("generate_draft", "assembly_discover_and_probe"): "QC 通道启用时",
    ("assembly_validate_render_qc", "assembly_repair_loop"): "未达标且仍可重试",
    ("assembly_validate_render_qc", "assembly_write_report"): "通过/达上限/软降级",
    ("assembly_repair_loop", "assembly_validate_render_qc"): "修复后复检",
    ("generate_draft", "node_06_human_reorder"): "QC 通道关闭时",
    ("node_07_speed_fit", "bridge_snapshot2"): "时长达标",
    ("node_07_speed_fit", "node_07_speed_fit"): "未达标且可重试",
    ("node_07_speed_fit", "escalate_guardrail_failure"): "重试超限",
    ("node_10b_inject_text_fx_en", "node_checkpoint3_layout_review"): "布局检测命中",
    ("node_10b_inject_text_fx_en", "node_17_inject_english_tts"): "布局无问题",
    ("node_15_localize_covers_en", "node_09_inject_fx"): "列表 fan-in(与 17 汇合)",
    ("node_17_inject_english_tts", "node_09_inject_fx"): "列表 fan-in(与 15 汇合)",
    # ---- auto 链条件边 ----
    # (2026-11-10:load_media → search_media「已配检索源」旁路已随检索素材节点移除删除;
    #  load_media → search_web_topic 并行旁支已随前置化阶段3 拆除,条件路由移到 group_clips 之后)
    ("storyline_load_media", "storyline_split_shots"): "主干",
    ("storyline_group_clips", "storyline_search_web_topic"): "WEB_TOPIC 开且有可检索输入",
    ("storyline_search_web_topic", "storyline_generate_script"): "检索→文案",
    ("storyline_split_shots", "storyline_understand_clips"): "主干",
    ("storyline_group_clips", "storyline_plan_timeline_pro"): "列表 fan-in(4 源屏障)",
    ("storyline_generate_script", "storyline_plan_timeline_pro"): "列表 fan-in(4 源屏障)",
    ("storyline_generate_voiceover", "storyline_plan_timeline_pro"): "列表 fan-in(4 源屏障)",
    ("storyline_select_bgm", "storyline_plan_timeline_pro"): "列表 fan-in(4 源屏障)",
    ("storyline_group_clips", "storyline_generate_ai_transition"): "列表 fan-in(4 源屏障)",
    ("storyline_generate_script", "storyline_generate_ai_transition"): "列表 fan-in(4 源屏障)",
    ("storyline_generate_voiceover", "storyline_generate_ai_transition"): "列表 fan-in(4 源屏障)",
    ("storyline_select_bgm", "storyline_generate_ai_transition"): "列表 fan-in(4 源屏障)",
    ("storyline_plan_timeline_pro", "storyline_plan_timeline_ai_transition"): "AI 转场屏障",
    ("storyline_generate_ai_transition", "storyline_plan_timeline_ai_transition"): "AI 转场屏障",
    ("storyline_plan_timeline_pro", "storyline_qa_gate"): "列表 fan-in(3 源屏障)",
    ("storyline_plan_timeline_ai_transition", "storyline_qa_gate"): "列表 fan-in(3 源屏障)",
    ("storyline_recommend_transition", "storyline_qa_gate"): "列表 fan-in(3 源屏障)",
    ("storyline_recommend_text", "storyline_qa_gate"): "列表 fan-in(3 源屏障)",
    ("storyline_qa_gate", "storyline_group_clips"): "QA 未过,重试",
    ("storyline_qa_gate", "storyline_render_video"): "QA 通过",
    ("storyline_join", "generate_draft"): "有规划产物",
    ("storyline_join", "END"): "无规划产物",
    ("join_before_delivery", "export_final_video"): "QA gate 通过",
    ("join_before_delivery", "delivery_qa_repair"): "required 缺失,定向修复",
    ("join_before_delivery", "delivery_qa_failed"): "复检仍失败,终止",
    ("delivery_qa_repair", "join_before_delivery"): "修复后回 QA 复检",
}

# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _normalize_node_id(raw: str) -> str:
    """``__start__``/``__end__`` → ``START``/``END``,其余原样。"""
    return {"__start__": "START", "__end__": "END"}.get(raw, raw)


def _real_topology(compiled_graph: Any) -> tuple[list[str], set[tuple[str, str, bool]]]:
    """从编译图提取**真实**节点与边(边真源 = ``compiled_graph.builder``)。

    Returns:
        (node_ids, edges):``node_ids`` 为 ["START", *注册节点, "END"](注册序);
        ``edges`` 元素为 (from, to, is_conditional),已做 START/END 归一化。
        三类真实边:普通 ``add_edge``(含列表 fan-in 逐源展开)、条件边
        ``add_conditional_edges`` 的 mapping 端点、``→__end__`` 直连。

    Raises:
        RuntimeError: 编译图上没有 ``.builder``(langgraph 版本行为变化)。
    """
    builder = getattr(compiled_graph, "builder", None)
    if builder is None:
        raise RuntimeError(
            "compiled_graph.builder 不存在 —— langgraph 版本行为变化,"
            "monitoring.workflow_graph 的边真源提取需要相应适配"
        )

    node_ids = ["START", *builder.nodes.keys(), "END"]

    edges: set[tuple[str, str, bool]] = set()
    # 普通 add_edge(含 (__start__, x) 与 (x, __end__))
    for src, dst in getattr(builder, "edges", None) or ():
        edges.add((_normalize_node_id(src), _normalize_node_id(dst), False))
    # 列表 fan-in:add_edge([...], dst) → 每个源一条边
    for srcs, dst in getattr(builder, "waiting_edges", None) or ():
        for src in srcs:
            edges.add((_normalize_node_id(src), _normalize_node_id(dst), False))
    # 条件边:add_conditional_edges(src, fn, mapping) 的每个 mapping 端点
    branches_map = getattr(builder, "branches", None) or {}
    for src, named in branches_map.items():
        for _name, spec in named.items():
            ends = getattr(spec, "ends", None) or {}
            for _key, dst in ends.items():
                edges.add((_normalize_node_id(src), _normalize_node_id(dst), True))
    return node_ids, edges


def _edge_label(src: str, dst: str) -> Optional[str]:
    """边的展示标签:注册表优先,未注册的边无标签。"""
    return _EDGE_LABELS.get((src, dst))


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def build_graph_schema(compiled_graph: Any, config: Any = None) -> dict[str, Any]:
    """从编译后的 LangGraph 图生成 §4.3 结构的图 JSON(纯函数,不触发执行)。

    Args:
        compiled_graph: ``build_graph(...)`` 返回的 CompiledStateGraph;
            边真源取 ``compiled_graph.builder``(见模块 docstring 的画图伪边说明)。
        config: config 模块(monkeypatch 测试传模块对象);None 时 ``import config``。
            只读 ``ASSEMBLY_QC_GATE_ENABLED``(``STORYLINE_MODE`` 已固定为
            ``"auto"``,见 config import 期 fail-fast,此处不再读取)。

    Returns:
        dict: ``workflow_id / schema_version / mode / flags / nodes / edges``。
    """
    if config is None:
        import config as config_module
    else:
        config_module = config

    # 单流水线收敛(阶段3R):STORYLINE_MODE 恒为 "auto"(config import 期
    # fail-fast),mode 直接输出常量,不随 config 读法漂移;前端用它拼布局
    # 缓存 key,值不变则缓存 key 稳定。
    mode = "auto"
    qc_enabled = bool(getattr(config_module, QC_FLAG))

    node_ids, real_edges = _real_topology(compiled_graph)

    # ---- 边(先收集,reachable BFS 也要用) ----
    edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str, str]] = set()
    dup_counter: dict[tuple[str, str, str], int] = {}
    for src, dst, is_conditional in sorted(real_edges, key=lambda t: (t[0], t[1], t[2])):
        kind = "conditional" if is_conditional else "normal"
        key = (src, dst, kind)
        if key in seen_pairs:
            # 理论上不应出现;防御:同 (from,to,kind) 重复时加序号后缀
            n = dup_counter.get(key, 1) + 1
            dup_counter[key] = n
            edge_id = f"{src}->{dst}#{n}"
        else:
            seen_pairs.add(key)
            edge_id = f"{src}->{dst}"
        edges.append({
            "id": edge_id,
            "from": src,
            "to": dst,
            "kind": kind,
            "label": _edge_label(src, dst),
        })

    # ---- reachable:从 START 沿全部边 BFS(条件边计入 —— 它可能被选中) ----
    adjacency: dict[str, list[str]] = {}
    for e in edges:
        adjacency.setdefault(e["from"], []).append(e["to"])
    reachable_ids: set[str] = set()
    queue = deque(["START"])
    reachable_ids.add("START")
    while queue:
        cur = queue.popleft()
        for nxt in adjacency.get(cur, ()):  # noqa: B007 — BFS 消费
            if nxt not in reachable_ids:
                reachable_ids.add(nxt)
                queue.append(nxt)

    # ---- 节点 ----
    nodes: list[dict[str, Any]] = []
    for raw_id in node_ids:
        nid = raw_id
        is_terminal = raw_id in ("START", "END")
        meta = _NODE_META.get(nid)
        if meta is None:
            # 编译图出现了注册表没有的节点:如实透出,靠 test (c) 防漂移
            meta = {"name": nid, "group": "misc", "description": ""}
        if is_terminal:
            kind = "terminal"
            node_modes: list[str] = []
        else:
            # auto-only 单流水线:所有任务节点都属于 auto 链
            kind = "task"
            node_modes = ["auto"]

        if nid in _ASSEMBLY_NODES:
            enabled = qc_enabled
        else:
            enabled = True

        node: dict[str, Any] = {
            "id": nid,
            "name": meta["name"],
            "group": meta["group"],
            "description": meta["description"],
            "kind": kind,
            "checkpoint": meta.get("checkpoint"),
            "enabled": enabled,
            "reachable": nid in reachable_ids,
            "modes": node_modes,
        }
        if meta.get("waiting_kind"):
            node["waiting_kind"] = meta["waiting_kind"]
        nodes.append(node)

    return {
        "workflow_id": WORKFLOW_ID,
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "flags": {QC_FLAG: qc_enabled},
        "nodes": nodes,
        "edges": edges,
    }
