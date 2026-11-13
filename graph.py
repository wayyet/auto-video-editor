"""StateGraph 装配(auto-only 单流水线拓扑;2026-11-04 单流水线架构收敛 阶段3)。

完整图拓扑(2026-11-04 起,human 模式与 OpenStoryline Web 前置已删除):
  START
    → storyline_load_media          (19 节点确定性图入口,自动导入素材)
    → generate_draft
    → [ASSEMBLY_QC_GATE_ENABLED=True 时插入 6 节点 QC 通道]
        assembly_discover_and_probe
        → assembly_visual_observe
        → assembly_build_timeline
        → assembly_validate_render_qc      🔁 条件边(route_after_assembly_qc):
            ├─→ assembly_repair_loop       (未达标且 retry < MAX)
            │     ↻ assembly_validate_render_qc
            └─→ assembly_write_report      (达标 / retry 达上限 → 软降级)
        → node_06_human_reorder            ⏸ interrupt("①")
      [ASSEMBLY_QC_GATE_ENABLED=False 时:generate_draft 直接到 node_06_human_reorder]
    → node_07_speed_fit                🔁 条件边(route_after_speed_fit):
      ├─→ node_08_add_subtitles  (达标;节点本体已产出 snapshot2 并写 state)
      ├─→ node_07_speed_fit      (未达标,retry_counts < MAX_RETRY)
      └─→ escalate_guardrail_failure → END  (重试超限)
    → node_08_add_subtitles            (字幕主干兼容节点;B1 ASR 移除后不再转写语音)
    ├─→ [中文线] node_10_inject_text_fx
    │              → node_14_make_covers
    │              → node_15_localize_covers_en ──────────────┐
    └─→ [英文线] fork_draft_for_english_branch                  │
                   → node_16a_translate_and_check  (无源字幕时正常 skip)
                   → node_08b_add_subtitles_en     (英文字幕写入英文分支草稿)
                   → node_10b_inject_text_fx_en    (英文花字/字幕动画,先于人工校对)
                   → 🔁 条件边(route_after_translate):          │
                       ├─→ node_checkpoint3_layout_review ⏸ interrupt("③")(条件触发)
                       │         └─→ node_17_inject_english_tts │
                       └─→ node_17_inject_english_tts ───────────┤
                                                                ▼
    ⇒ node_09_inject_fx              (汇合屏障:node_15 与 node_17 都完成才触发)
    → node_09b_inject_opening_hook   (开场钩子:前 3 秒免费开场特效 + 大字标题,第四期阶段 3)
    → node_11_inject_sticker         (尾链 09/09b/11/12/13 同时作用于中英两份草稿)
    → node_12_human_add_bgm          ⏸ interrupt("②")
    → node_13_adjust_volume
    → join_before_delivery           (交付 QA 闸门 + required-feature gate,单前驱)
    → 🔁 条件边(route_after_join_qa,阶段 6):
      ├─→ export_final_video         (gate 通过;可选 MP4 导出,默认关闭记 skipped)
      ├─→ delivery_qa_repair         (gate 失败未修复:重跑幂等修复节点 → 回 join 复检)
      └─→ delivery_qa_failed         (复检仍失败:raise → run.failed,不静默 END)
    → END

2026-11-04 单流水线架构收敛(阶段3,见《单流水线架构设计与分阶段执行计划
评审修订版》§6 阶段3 / R-03 / R-10 / R-11):
- ``STORYLINE_MODE`` 固定为 ``auto``(config.py import 时校验,传其他值直接报错);
  human 模式 5 节点(checkpoint0_storyline_plan / import_video / get_storyboard_plan /
  launch_openstoryline / open_preview)与 ``nodes/storyline/fusion.py`` 融合层整体删除。
- 图入口重接:``START → storyline_load_media``,不再经过 OpenStoryline Web
  探测 / 打开预览(旧 Web 生命周期由删除前的 start_production.ps1 管)。
- ``clean_cache`` 节点删除(R-10):唯一触发入口 ``POST /api/system/clean-cache``
  已随旧聊天运行时(阶段2)删除,缓存清理能力如需保留另做独立 CLI 设计。
- 保留:关卡①②③(interrupt + resume,``agent_gate resume`` / 直接 CLI
  ``scripts/resume_workflow.py`` 放行)、assembly QC 六节点、speed-fit
  guardrail、中英双语线、列表 fan-in 汇合、交付前 QA。

2026-10 调序(保留,见《auto-video-editor节点调序与全量工作流设计执行计划》):
- 英文线补 ``node_08b_add_subtitles_en`` / ``node_10b_inject_text_fx_en``;
- ``node_09/11/12/13`` 全部推迟到 ``node_17`` 之后(列表型 fan-in 保证先后);
- K3=B:``node_10b`` 在关卡③ **之前**;K2=B:尾链四节点内部同时处理中英草稿;
- ``node_08 → fork`` 显式依赖:16a 读 node_08 的 asr_segments_zh
  (2026-11-08 B1 ASR 移除后恒为空 → 16a 正常 skip);
- ②与③ 永不同时挂起(②在尾链,③在英文线)。

2026-10 接入原"不可达节点"(保留,见《不可达节点接入LangGraph设计与执行计划》):
- ``storyline_search_web_topic``:2026-11-14 依赖约简阶段5 起改为
  ``storyline_group_clips`` **并行 fan-out** 触发(与模板推荐同时),经
  ``[模板推荐, 检索]→generate_script`` 双源屏障汇入;节点每轮必跑 —— 检索开关
  关闭或 ``build_web_topic_query`` 构造不出非空检索词时节点内部软跳过(不发
  网络请求)。不再是 load_media 后的死胡同旁支,也不再是条件路由分支。
- ``storyline_generate_ai_transition`` 单源边挂 ``recommend_transition`` 之后
  (2026-11-14 依赖约简阶段1;历史:2026-11-12 边治理阶段3 曾为屏障 [GC, RTR]);
  ``plan_timeline_ai_transition`` 屏障 [PTP, GAT] 后接管 qa_gate 的屏障位
  (``STORYLINE_ENABLE_AI_TRANSITION``,默认开,**建图时读取**,改开关要重启进程)。
- ⚠️ 屏障规则(D10):**每轮并非必跑**的节点绝不能放进任何 fan-in 屏障 ——
  qa 重试回路不会重跑它们的上游,屏障永远凑不齐,图会静默 END。
  (历史:web_topic 曾是条件触发节点受此约束;2026-11-14 依赖约简阶段5 起它
  每轮必跑、内部软跳过,已可进入 ``[STR, WT]→GS`` 屏障。)
  AI 转场两节点每轮必跑(建图时按开关决定接不接),可以进屏障。
  (``storyline_local_asr`` / ``storyline_speech_rough_cut`` 曾是条件旁支,
  2026-11-08 B1 ASR 移除已整体删除,``split_shots`` 改为静态直连 ``understand_clips``。)
- ⚠️ qa 重试路由靠 ``status_log[-1] == "storyline_qa_retry"`` 判断:重试回路内的
  节点**不得产生每轮都不同的状态标签**。

Week 5 关键改动(保留):
- ``node_16_translate_subtitles`` 拆为 ``node_16a_translate_and_check`` +
  ``node_checkpoint3_layout_review``;条件边 ``route_after_translate`` 据
  ``layout_issues_detected`` 二选一走向。
- 关卡①/②/③ payload ``checkpoint`` 字段统一为 ``"①"/"②"/"③"``(旧值保留在
  ``legacy_id`` 字段,``agent_gate resume`` / ``scripts/resume_workflow.py`` 按它匹配)。

汇合:node_15 与 node_17 通过 ``add_edge([...], "node_09_inject_fx")`` 单次调用汇入,
LangGraph fan-in 自动等齐两条分支后才触发尾链首节点;``join_before_delivery`` 只做交付前 QA。

关卡①/②/③ 使用 ``langgraph.types.interrupt``,需要 checkpointer。
PostgreSQL 统一改造后(checkpointer 统一为 PostgreSQL,方案 §4.1 / §4.2):
- 生产:``get_checkpointer()`` → ``AsyncPostgresSaver``(唯一持久化后端);
- 单测:``make_checkpointer("memory")`` → ``InMemorySaver``;
- ``build_graph()`` **不再**自动兜底创建 saver —— 未注入就明确报错,
  避免"悄悄退化成无持久化"导致挂起任务查不到(方案 §8 风险表)。
"""

from __future__ import annotations

import contextlib
import uuid
from pathlib import Path
from typing import Optional

from config import (
    NODE_07_MAX_RETRY,
    resolve_draft_dir,
)
from monitoring.heartbeat_writer import start_heartbeat

# pre-flight 依赖只在用户显式打开时才 import,避免现有测试多走依赖链
PreflightConfig = None  # type: ignore[assignment]
PreflightError = None   # type: ignore[assignment]
from nodes.node_05_generate_draft import generate_initial_jianying_draft
from nodes.node_06_human_reorder import human_reorder
from nodes.node_07_speed_fit import route_after_speed_fit, speed_fit
from nodes.node_08_add_subtitles import add_subtitles
from nodes.node_08b_add_subtitles_en import add_subtitles_en
from nodes.node_09_inject_fx import inject_fx
from nodes.node_09b_inject_opening_hook import inject_opening_hook
from nodes.node_10_inject_text_fx import inject_text_fx, inject_text_fx_en
from nodes.node_11_inject_sticker import inject_sticker
from nodes.node_12_human_add_bgm import human_add_bgm
from nodes.node_13_adjust_volume import adjust_volume
from nodes.node_14_make_covers import node_14_make_covers
from nodes.node_15_localize_covers_en import node_15_localize_covers_en
from nodes.node_16a_translate_and_check import node_16a_translate_and_check
from nodes.node_checkpoint3_layout_review import node_checkpoint3_layout_review
from nodes.node_17_inject_english_tts import node_17_inject_english_tts
from nodes.node_18_export_video import export_final_video
from nodes.node_delivery_qa_repair import delivery_qa_failed, delivery_qa_repair
from nodes.node_fork_english_branch import fork_draft_for_english_branch
from nodes.node_join_before_delivery import join_before_delivery
from state import WorkflowState

# ---------------------------------------------------------------------------
# Phase 5:assembly QC 通道节点(plan §七 / ADR-1~5 / §十 阶段四)
# ---------------------------------------------------------------------------
# 6 个新节点:discover → visual_observe → build_timeline → validate/render/qc
# → repair_loop (条件) → write_report。所有节点都注册到图中,但当
# ``ASSEMBLY_QC_GATE_ENABLED=false`` 时不接边,等价于改动前状态。
from nodes.assembly import (
    assembly_visual_observe_node,
    assembly_build_timeline_node,
    assembly_discover_and_probe_node,
    assembly_repair_loop_node,
    assembly_validate_render_qc_node,
    assembly_write_report_node,
    route_after_assembly_qc,
)

# ---------------------------------------------------------------------------
# auto 链 qa_gate / join_storyline(plan_v4 §2.2)
# ---------------------------------------------------------------------------
from nodes.storyline import (
    qa_gate as _qa_gate_mod,
    join_storyline as _join_storyline_mod,
)
from nodes.storyline.node_load_media import storyline_load_media_node
from nodes.storyline.node_search_web_topic import storyline_search_web_topic_node
from nodes.storyline.node_split_shots import storyline_split_shots_node
from nodes.storyline.node_generate_ai_transition import (
    storyline_generate_ai_transition_node,
)
from nodes.storyline.node_understand_clips import storyline_understand_clips_node
from nodes.storyline.node_filter_clips import storyline_filter_clips_node
from nodes.storyline.node_group_clips import storyline_group_clips_node
from nodes.storyline.node_generate_script import storyline_generate_script_node
from nodes.storyline.node_script_template_recommendation import (
    storyline_script_template_recommendation_node,
)
from nodes.storyline.node_generate_voiceover import (
    storyline_generate_voiceover_node,
)
from nodes.storyline.node_select_bgm import storyline_select_bgm_node
from nodes.storyline.node_recommend_transition import (
    storyline_recommend_transition_node,
)
from nodes.storyline.node_recommend_text import storyline_recommend_text_node
from nodes.storyline.node_plan_timeline_pro import (
    storyline_plan_timeline_pro_node,
)
from nodes.storyline.node_plan_timeline_ai_transition import (
    storyline_plan_timeline_ai_transition_node,
)
from nodes.storyline._common import target_duration_ms
from storyline_capabilities.ai_transition_client import is_ai_transition_enabled


# ---------------------------------------------------------------------------
# storyline_join 之后的条件路由(auto-only;Week 5 起 generator 的兜底语义保留)
# ---------------------------------------------------------------------------
def _route_after_storyline_join(state: WorkflowState) -> str:
    """storyline_join 之后的条件路由。

    - 有 ``storyline_plan``(join_storyline 已反序列化到 state)→ ``generate_draft``
    - 否则 → ``END``(空 plan 与 qa_gate 失败强制 continue 的语义一致)
    - ``shot_plan`` 是 node_05 generate_draft 的旧 checkpoint / 测试兜底输入,
      保留读取兼容(human 路径删除后正常运行不再产生该键)。
    """
    if state.get("storyline_plan") or state.get("shot_plan"):
        return "generate_draft"
    return "__end__"


# ---------------------------------------------------------------------------
# (2026-11-14 依赖约简阶段5)``_storyline_route_after_template_rec`` 条件路由
# 已删除:web_topic 节点改为每轮必跑(开关关闭/检索词为空时节点内部软跳过,
# 只落 skipped 标签、不发网络请求),与模板推荐并行 fan-out 后经
# ``[STR, WT]→GS`` 双源屏障汇入文案。历史:2026-11-12 noop 治理阶段1 曾把
# 检索条件路由挂到模板推荐出口(更早挂 group_clips)—— 条件路由改为并行屏障
# 后「路由放行 ⇒ 节点能构造检索词」的一致性约束由节点内部的同一 builder 自检。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Week 5 条件边:步骤 10b 之后是否触发关卡③
# ---------------------------------------------------------------------------
def route_after_translate(state: WorkflowState) -> str:
    """node_10b_inject_text_fx_en 之后的条件路由(K3=B:花字先于人工校对)。

    - ``layout_issues_detected`` 为真 → 关卡③(``node_checkpoint3_layout_review``)
    - 否则 → ``node_17_inject_english_tts``

    挂载点 2026-10 从 08b 后移到 10b 后:花字(描边/入场动画)先落地,人工在关卡③
    打开剪映看到的就是带花字的最终效果,而不是"改完还要再等一步才有花字"。
    """
    if state.get("layout_issues_detected"):
        return "node_checkpoint3_layout_review"
    return "node_17_inject_english_tts"


# ---------------------------------------------------------------------------
# 阶段 6 条件边(效果缺失修复):交付 QA required-feature gate 阻断路由
# ---------------------------------------------------------------------------
def route_after_join_qa(state: WorkflowState) -> str:
    """join_before_delivery 之后的条件路由(计划 §8 阶段 6 任务 4)。

    - gate 未失败(required 类别全 passed/not_applicable)→ 可选导出;
    - gate 失败且尚未修复过 → ``delivery_qa_repair``(重跑幂等修复节点,
      回边到 join 复检);
    - gate 失败且修复过仍失败 → ``delivery_qa_failed``(raise → run.failed,
      不允许静默 END)。

    判据读 ``join_qa_report.gate_failed``(join 节点写,见
    :func:`draft_ops.draft_integrity.build_qa_categories`),与
    ``delivery_qa_retry_count``(repair 唯一写入者)。
    """
    report = state.get("join_qa_report") or {}
    if not report.get("gate_failed"):
        return "export_final_video"
    if not state.get("delivery_qa_retry_count"):
        return "delivery_qa_repair"
    return "delivery_qa_failed"


# ---------------------------------------------------------------------------
# 升级节点:节点 7 重试超限后的终止点
# ---------------------------------------------------------------------------
def escalate_guardrail_failure(state: WorkflowState) -> dict:
    """节点 7 连续 NODE_07_MAX_RETRY 次重试仍未达标时的兜底:写 error_log + 终止。

    P1(决定 1 选 B):目标时长读 ``state.storyline_targets.target_duration_ms``
    (创建台可调,缺省 35s),文案随目标动态化,不再写死 "35s"。
    """
    target_s = target_duration_ms(state) / 1000
    errors = list(state.get("error_log", []) or []) + [
        f"[node_07] 护栏节点连续 {NODE_07_MAX_RETRY} 次重试仍未将总时长压到 {target_s:g}s 以内,流程升级",
    ]
    log = list(state.get("status_log", []) or []) + ["guardrail_failed"]
    return {**state, "error_log": errors, "status_log": log}


# ---------------------------------------------------------------------------
# (2026-11-14 依赖约简阶段4)bridge_snapshot2 桥接节点已删除:快照②生产移入
# speed_fit 节点本体(放行路径直写 state.snapshot2_path),路由函数只返回标签,
# 条件边 mapping 直连 node_08_add_subtitles。历史:条件边不能返回 state,曾靠
# 本节点调 get_snapshot2_path 从磁盘重定位快照并写 state。
# ---------------------------------------------------------------------------
# Week 5:Postgres checkpointer asynccontextmanager
# ---------------------------------------------------------------------------
import contextlib


@contextlib.asynccontextmanager
async def get_checkpointer():
    """Week 5 计划 §4.6:Postgres checkpointer 的异步 context manager。

    用法(生产 main 入口):
        async with get_checkpointer() as saver:
            graph = build_graph(checkpointer=saver)
            await graph.ainvoke(state, config)

    关键步骤:
    1. ``AsyncPostgresSaver.from_conn_string(uri)`` — uri 来自统一 DSN 解析层
    2. ``await saver.setup()`` — 首次使用建表(checkpoints / checkpoint_blobs /
       checkpoint_writes),幂等
    3. yield saver 供 build_graph 使用
    4. context manager 退出时 saver 自行清理

    Raises:
        RuntimeError: 未安装 ``langgraph-checkpoint-postgres``;或
            ``APP_ENV=production`` 且设置了 ``AGENT_GATE_POSTGRES_URI``
            (统一解析层 B-T4 生产防护)。
    """
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as e:
        raise RuntimeError(
            "Postgres checkpointer 需要 langgraph-checkpoint-postgres 包,"
            "请先 pip install langgraph-checkpoint-postgres psycopg[binary,pool]"
        ) from e

    # B-T2(环境变量传播链路优化):改用统一 DSN 解析层,与 agent_gate worker
    # 共用同一套优先级与空值回退 —— 两条 LangGraph 路径不再可能因解析规则
    # 不同而连到不同数据库(执行设计 §4.3 的 P0;原 ``os.environ.get`` 自有
    # 解析与空值检查一并删除)。
    from db.postgres_config import resolve_postgres_target

    uri = resolve_postgres_target().uri

    # ⚠️ 必须先设 JSONB 的 dumps:本机库编码是 SQL_ASCII,psycopg3 默认
    # ``ensure_ascii=True`` 会把中文写成 ``\uXXXX``,PG 无法把它翻译成 SQL_ASCII
    # ⇒ ``UntranslatableCharacter``。这是**进程级全局**设置,LangGraph saver
    # 内部用的 ``Jsonb(...)`` 也吃这一套(方案 §3.2 A5)。
    from db.connection import configure_json_dumps

    configure_json_dumps()

    # AsyncPostgresSaver.from_conn_string 返回一个 AsyncContextManager(可作 async with)
    # 但我们要让外层直接 yield saver,需要在内部 __aenter__ 拿到实例
    saver_cm = AsyncPostgresSaver.from_conn_string(uri)
    async with saver_cm as saver:
        # 首次使用建表(幂等);Week 5 计划 §3.3 "破坏性变更应急" 文档明示
        await saver.setup()
        yield saver


# ---------------------------------------------------------------------------
# thread_id 归一(方案 §3.2 A2 / §3.1 M2)
# ---------------------------------------------------------------------------
def normalize_thread_id(thread_id: object) -> str:
    """把 thread_id 归一成 ``str``;**拒绝 bytes**。

    为什么必须拒绝 bytes
    ------------------
    ``langgraph.checkpoint.postgres`` 的 saver 把
    ``config["configurable"]["thread_id"]`` **原样透传**给 psycopg3
    (``langgraph/checkpoint/postgres/__init__.py`` 的 ``put`` / ``aput``)。
    psycopg3 把 Python ``bytes`` 写进 ``TEXT`` 列时,会存成 ``\\x<hex>`` 字面量。

    结果:同一个逻辑线程被分裂成**两套 thread_id** ——
    文本版(``video-wiring-001``)与 hex 版(``\\x766964656f...``)。
    查 ``WHERE thread_id = 'xxx'`` 会漏掉续跑的那一半,挂起任务查不到、续跑断链。

    本机实测(2026-10-29):``video-wiring-001`` 40 条(step -1→38)+
    ``\\x766964656f2d776172696e672d303031`` 22 条(step 39→60),正好接续。

    Raises:
        TypeError: thread_id 不是 ``str``(尤其是 bytes)。
        ValueError: thread_id 为空 / 只有空白。
    """
    if isinstance(thread_id, (bytes, bytearray, memoryview)):
        raise TypeError(
            "thread_id 必须是 str,不能是 bytes —— psycopg3 会把 bytes 写进 TEXT 列成 "
            f"\\x<hex> 字面量,导致同一逻辑线程分裂成两套 thread_id"
            f"(got {type(thread_id).__name__})"
        )
    if not isinstance(thread_id, str):
        raise TypeError(
            f"thread_id 必须是 str(got {type(thread_id).__name__})"
        )
    value = thread_id.strip()
    if not value:
        raise ValueError("thread_id 不能为空")
    return value


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------
def build_graph(
    checkpointer=None,
    *,
    thread_id: str = "default",
    start_heartbeat_thread: bool = True,
    run_preflight: bool = False,
    preflight_config: Optional["PreflightConfig"] = None,
):
    """装配 17 节点 + 1 升级节点 + 1 桥接节点的完整图。

    Args:
        checkpointer: 已构造的 checkpointer。**必填** —— 为 ``None`` 时直接抛
            ``RuntimeError``,不再自动兜底(方案 §4.2)。
            生产用 ``get_checkpointer()``(AsyncPostgresSaver),
            单测用 ``make_checkpointer("memory")``(InMemorySaver)。
        thread_id: 工作流线程 ID;必须是 ``str``(见 ``normalize_thread_id``)。
        start_heartbeat_thread: 是否同时启动心跳写线程(默认 True)。
            单测可传 False 避免 IO。
        run_preflight: 是否在 compile 前执行 pre-flight 自检(默认 False,
            保持现有 84 条测试零改动)。主入口 ``scripts/run_workflow.py``
            应设为 True。
        preflight_config: 传入自定义 ``PreflightConfig``;None 时使用
            ``PreflightConfig.from_env()``。仅 ``run_preflight=True`` 时生效。

    Returns:
        CompiledStateGraph 实例。

    Raises:
        RuntimeError: 未注入 checkpointer。
        PreflightError: pre-flight 任一步骤失败时抛出。
        TypeError / ValueError: thread_id 不是合法的 str。
    """
    if run_preflight:
        # 延迟 import,确保 84 条现有测试不会因 preflight 模块失败而崩
        from runtime.preflight import (
            PreflightConfig as _PC,
            PreflightError as _PE,
            run_preflight as _run_preflight,
        )

        # 让模块级 hint 跟着实际导入同步,便于类型检查
        global PreflightConfig, PreflightError
        PreflightConfig = _PC  # type: ignore[assignment]
        PreflightError = _PE   # type: ignore[assignment]

        cfg = preflight_config or _PC.from_env()
        result = _run_preflight(cfg)
        if not result.ok:
            failed = result.first_failure()
            assert failed is not None  # ok=False 时 first_failure 必非空
            raise _PE(
                f"pre-flight 在 step={failed.step!r} 失败: {failed.error}"
            )

    # thread_id 归一(方案 §3.2 A2):必须是 str,否则 saver 会写出 \\x<hex> 版。
    thread_id = normalize_thread_id(thread_id)

    if checkpointer is None:
        raise RuntimeError(
            "build_graph() 必须显式注入 checkpointer"
            "(生产用 graph.get_checkpointer(),单测用 make_checkpointer('memory'))"
            "—— 不再自动兜底,避免悄悄退化成无持久化。"
        )

    if start_heartbeat_thread:
        try:
            start_heartbeat()
        except Exception:  # noqa: BLE001 — 心跳失败不应阻塞图编译
            pass

    g = _build_state_graph()
    return g.compile(checkpointer=checkpointer)


def _build_state_graph():
    """构造 StateGraph 拓扑(不 compile,便于单测替换 checkpointer)。

    auto-only(2026-11-04 阶段3):``START`` 直达 ``storyline_load_media``,
    human 模式 5 节点与 fusion 融合层已删除(见模块 docstring)。
    """
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(WorkflowState)

    # ---- 节点 5(用 wrapper 注入 draft_dir)----
    def generate_draft_wrapped(state):
        """节点 5 wrapper — Week 3 测试支持。

        行为:
        - 若 state["draft_path"] 指向已存在的 draft_content.json(测试 fixture 已预置),
          跳过实际写入,直接返回现有 state(保持原有 draft_path / encryption_status / strategy)
        - 否则按默认 draft_dir 生成草稿(生产路径)
        """
        existing = state.get("draft_path")
        if existing and Path(existing).exists():
            # 测试 fixture 已预置 draft — 不重写,直接通过
            return state
        draft_dir = resolve_draft_dir(state)
        return generate_initial_jianying_draft(state, draft_dir)

    g.add_node("generate_draft", generate_draft_wrapped)

    # ---- 关卡① + 护栏 + 关卡② (Week 3) ----
    g.add_node("node_06_human_reorder", human_reorder)
    g.add_node("node_07_speed_fit", speed_fit)
    g.add_node("escalate_guardrail_failure", escalate_guardrail_failure)
    g.add_node("node_08_add_subtitles", add_subtitles)
    g.add_node("node_09_inject_fx", inject_fx)
    g.add_node("node_09b_inject_opening_hook", inject_opening_hook)
    g.add_node("node_10_inject_text_fx", inject_text_fx)
    g.add_node("node_10b_inject_text_fx_en", inject_text_fx_en)
    g.add_node("node_11_inject_sticker", inject_sticker)
    g.add_node("node_12_human_add_bgm", human_add_bgm)
    g.add_node("node_13_adjust_volume", adjust_volume)

    # ---- Phase 5:assembly QC 通道(plan §七 / §十 阶段四)----
    # ADR-3:默认 ASSEMBLY_QC_GATE_ENABLED=True → 6 节点全部接进图;
    # 设为 False 时只 add_node 不接边,等价于改动前状态,human-mode 关卡①
    # 不会突然被打破。
    g.add_node("assembly_discover_and_probe", assembly_discover_and_probe_node)
    g.add_node("assembly_visual_observe", assembly_visual_observe_node)
    g.add_node("assembly_build_timeline", assembly_build_timeline_node)
    g.add_node("assembly_validate_render_qc", assembly_validate_render_qc_node)
    g.add_node("assembly_repair_loop", assembly_repair_loop_node)
    g.add_node("assembly_write_report", assembly_write_report_node)

    # ---- Week 4 节点 + Week 5 拆分(7 个)----
    g.add_node("fork_draft_for_english_branch", fork_draft_for_english_branch)
    g.add_node("node_14_make_covers", node_14_make_covers)
    g.add_node("node_15_localize_covers_en", node_15_localize_covers_en)
    g.add_node("node_16a_translate_and_check", node_16a_translate_and_check)
    g.add_node("node_08b_add_subtitles_en", add_subtitles_en)
    g.add_node("node_checkpoint3_layout_review", node_checkpoint3_layout_review)
    g.add_node("node_17_inject_english_tts", node_17_inject_english_tts)
    g.add_node("join_before_delivery", join_before_delivery)
    # 2026-11-07 阶段 6:可选最终 MP4 导出(交付 QA 与导出分离;开关关闭时
    # 节点显式记 skipped,不静默)。
    g.add_node("export_final_video", export_final_video)
    # 2026-11-09 阶段 6(效果缺失修复):required-feature gate 阻断路由两节点
    g.add_node("delivery_qa_repair", delivery_qa_repair)
    g.add_node("delivery_qa_failed", delivery_qa_failed)

    # ---- auto 链 17 节点 + qa_gate + join_storyline(plan_v4 §2.2 / §2.3)----
    # (2026-11-10:storyline_search_media「检索素材」节点已整体移除;
    #  2026-11-12 边治理阶段5:storyline_render_video 节点已整体移除)
    g.add_node("storyline_load_media", storyline_load_media_node)
    # 检索节点:2026-11-14 依赖约简阶段5 起每轮必跑(与模板推荐并行 fan-out,
    # 开关关/无检索词时节点内部软跳过);历史:2026-11-10 前置化阶段3 条件前置、
    # 2026-11-12 noop 治理阶段1 条件路由挂模板推荐出口 —— 均已随并行化删除
    g.add_node("storyline_search_web_topic", storyline_search_web_topic_node)
    g.add_node("storyline_split_shots", storyline_split_shots_node)
    # 2026-10 已接入:与 plan_timeline_pro 并排(同一道 4 源屏障;建图时按开关接边)
    g.add_node(
        "storyline_generate_ai_transition",
        storyline_generate_ai_transition_node,
    )
    g.add_node("storyline_understand_clips", storyline_understand_clips_node)
    g.add_node("storyline_filter_clips", storyline_filter_clips_node)
    g.add_node("storyline_group_clips", storyline_group_clips_node)
    g.add_node("storyline_generate_script", storyline_generate_script_node)
    g.add_node(
        "storyline_script_template_recommendation",
        storyline_script_template_recommendation_node,
    )
    g.add_node("storyline_generate_voiceover", storyline_generate_voiceover_node)
    g.add_node("storyline_select_bgm", storyline_select_bgm_node)
    g.add_node("storyline_recommend_transition", storyline_recommend_transition_node)
    g.add_node("storyline_recommend_text", storyline_recommend_text_node)
    g.add_node("storyline_plan_timeline_pro", storyline_plan_timeline_pro_node)
    # 2026-10 已接入:plan_timeline_pro + generate_ai_transition 之后、qa_gate 之前
    g.add_node(
        "storyline_plan_timeline_ai_transition",
        storyline_plan_timeline_ai_transition_node,
    )
    g.add_node("storyline_qa_gate", _qa_gate_mod.storyline_qa_gate_node)
    g.add_node("storyline_join", _join_storyline_mod.storyline_join_node)

    # ---- 边 ----
    # auto-only 入口(2026-11-04 阶段3):START 直达 auto 链首,
    # 不再经过 OpenStoryline Web 探测(launch_openstoryline)/ 打开预览(open_preview)。
    # 前置条件(素材路径 / 输出目录 / 配置)由入口层负责:
    # - 直接 CLI:scripts/run_workflow.py(--initial-state-json / 默认素材回退)
    # - Agent 路径:agent_gate worker(run_start / drive,safe_media_path 白名单)
    g.add_edge(START, "storyline_load_media")

    # ---- 阶段 7 拓扑收尾 + Phase 5 assembly QC 通道边(plan §八 / ADR-3)----
    # 默认 ``ASSEMBLY_QC_GATE_ENABLED=True``:``generate_draft`` 改为
    # 接 ``assembly_discover_and_probe``,``assembly_write_report`` 接回
    # ``node_06_human_reorder``。
    # ``ASSEMBLY_QC_GATE_ENABLED=False``(应急关闭):保留原 ``generate_draft
    # → node_06_human_reorder``,add_node 已注册但完全无连接 — 等价于
    # 改动前状态(plan §11 验收项"应急关闭生效")。
    #
    # 运行时读 ``config.ASSEMBLY_QC_GATE_ENABLED``,不走模块顶层 import 的本地
    # 绑定(单测用 ``monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", ...)``
    # 才能生效)。
    import config as _config
    if _config.ASSEMBLY_QC_GATE_ENABLED:
        # 重新指向:generate_draft 不再直连 node_06_human_reorder,
        # 先接 assembly 链首;6 节点按 plan §八 接边 + 条件路由。
        g.add_edge("generate_draft", "assembly_discover_and_probe")
        g.add_edge("assembly_discover_and_probe", "assembly_visual_observe")
        g.add_edge("assembly_visual_observe", "assembly_build_timeline")
        g.add_edge("assembly_build_timeline", "assembly_validate_render_qc")
        g.add_conditional_edges(
            "assembly_validate_render_qc",
            route_after_assembly_qc,
            {
                "assembly_repair_loop": "assembly_repair_loop",
                "assembly_write_report": "assembly_write_report",
            },
        )
        g.add_edge("assembly_repair_loop", "assembly_validate_render_qc")
        g.add_edge("assembly_write_report", "node_06_human_reorder")
    else:
        # 应急关闭:原状,add_node 注册的 6 个 assembly 节点不接边,不参与执行。
        g.add_edge("generate_draft", "node_06_human_reorder")

    g.add_edge("node_06_human_reorder", "node_07_speed_fit")

    # 节点 7 条件边:达标直连节点 8(2026-11-14 依赖约简阶段4:快照②由 speed_fit
    # 节点本体在放行路径写入 state.snapshot2_path,bridge_snapshot2 桥接节点已删,
    # 路由函数纯路由无文件副作用);未达标自循环或升级
    g.add_conditional_edges(
        "node_07_speed_fit",
        route_after_speed_fit,
        {
            "node_08_add_subtitles": "node_08_add_subtitles",
            "node_07_speed_fit": "node_07_speed_fit",
            "escalate_guardrail_failure": "escalate_guardrail_failure",
        },
    )
    g.add_edge("escalate_guardrail_failure", END)

    # ---- 2026-10 调序:中文线 08 → 10 → 14 → 15;英文线 fork → 16a → 08b → 10b → [③] → 17;
    #      两线汇合后才跑尾链 09 → 11 → 12(②) → 13 → join_before_delivery(QA)→ END ----
    # 中文线
    g.add_edge("node_08_add_subtitles", "node_10_inject_text_fx")
    g.add_edge("node_10_inject_text_fx", "node_14_make_covers")
    g.add_edge("node_14_make_covers", "node_15_localize_covers_en")

    # 英文线:B1 ASR 移除后 16a 无源字幕即正常 skip;08 → fork 的图边保留(拓扑不变)
    g.add_edge("node_08_add_subtitles", "fork_draft_for_english_branch")
    g.add_edge("fork_draft_for_english_branch", "node_16a_translate_and_check")
    g.add_edge("node_16a_translate_and_check", "node_08b_add_subtitles_en")
    # K3=B:花字(10b)先于人工校对(③)。人工在剪映里看到的就是带花字的最终效果。
    g.add_edge("node_08b_add_subtitles_en", "node_10b_inject_text_fx_en")
    g.add_conditional_edges(
        "node_10b_inject_text_fx_en",
        route_after_translate,
        {
            "node_checkpoint3_layout_review": "node_checkpoint3_layout_review",
            "node_17_inject_english_tts": "node_17_inject_english_tts",
        },
    )
    g.add_edge("node_checkpoint3_layout_review", "node_17_inject_english_tts")

    # 汇合 + 尾链起点:列表语法 → 中文线(15)与英文线(17)都到才触发 node_09,只触发一次
    g.add_edge(
        ["node_15_localize_covers_en", "node_17_inject_english_tts"],
        "node_09_inject_fx",
    )
    # 尾链(K2=B:节点内部同时处理中文主草稿与英文分支草稿)
    # 09b(开场钩子)插在 09 与 11 之间:必须在 node_08 之后(标题不随字幕轨同步
    # 被删)、node_11 之前(node_11 跳过 hook-title- 前缀文字,标题不被贴贴纸)
    g.add_edge("node_09_inject_fx", "node_09b_inject_opening_hook")
    g.add_edge("node_09b_inject_opening_hook", "node_11_inject_sticker")
    g.add_edge("node_11_inject_sticker", "node_12_human_add_bgm")
    g.add_edge("node_12_human_add_bgm", "node_13_adjust_volume")
    # 交付前 QA 闸门:单前驱,不再承担 fan-in
    g.add_edge("node_13_adjust_volume", "join_before_delivery")

    # join → 条件路由(阶段 6:required-feature gate)→ 可选导出 / 定向修复 / 失败终止
    # gate 未失败 → export;失败未修复 → repair(回边 join 复检);复检仍失败 →
    # delivery_qa_failed(raise → run.failed)。导出与发布/关卡/QA 分离(计划 §8 阶段 6);
    # FINAL_EXPORT_ENABLED=False 时节点显式记 final_export_status="skipped" 并上报
    # export.skipped,不渲染。
    g.add_conditional_edges(
        "join_before_delivery",
        route_after_join_qa,
        {
            "export_final_video": "export_final_video",
            "delivery_qa_repair": "delivery_qa_repair",
            "delivery_qa_failed": "delivery_qa_failed",
        },
    )
    g.add_edge("delivery_qa_repair", "join_before_delivery")
    g.add_edge("delivery_qa_failed", END)
    g.add_edge("export_final_video", END)

    # ---- auto 链 17 节点图边(plan_v4 §2.2 / §5 阶段 0)----
    # 起点:START 已直连 storyline_load_media(2026-11-04 阶段3)。

    # LM 后主干 → split_shots(2026-11-10 前置化阶段3:web_topic 并行 fan-out 条件边
    # 整体拆除,与 B1 移除后 split_shots 主边同一先例 —— load_media 后不再有任何旁支;
    # 检索条件路由移到 group_clips 之后,见下面 group_clips 三路注释)。
    g.add_edge("storyline_load_media", "storyline_split_shots")

    # 2026-11-08 B1 ASR 移除:原条件路由(_storyline_route_after_split_shots,含
    # local_asr 旁支)删除,改为确定性主边 —— 无论素材有无音轨都进入视觉理解主链。
    g.add_edge("storyline_split_shots", "storyline_understand_clips")

    # understand_clips → filter_clips → group_clips(主线)
    g.add_edge("storyline_understand_clips", "storyline_filter_clips")
    g.add_edge("storyline_filter_clips", "storyline_group_clips")

    # group_clips 出路(2026-11-14 依赖约简阶段5 起:A/B 两路并行 fan-out):
    #   A) storyline_script_template_recommendation(主链前置节点)
    #   A')storyline_search_web_topic —— 与模板推荐**并行**触发(第5期计划 §9:
    #      检索构造 query 只读 targets/overall/groups[].theme,不读模板产物,
    #      两能力间无数据依赖)。节点每轮必跑:开关关闭或检索词为空时内部
    #      软跳过(只落 skipped 标签,不发网络请求),因此可以进入必需屏障
    #      而不踩 D10(历史:2026-11-10 前置化阶段3 曾以条件路由表达"检索
    #      可选性";2026-11-12 noop 治理阶段1 条件路由改挂模板推荐出口 ——
    #      两者都已随并行化删除,检索词仍由 ``build_web_topic_query`` 自动构造)。
    #   A'')generate_script —— 双源屏障 [STR, WT],等模板推荐与检索节点都完成。
    #      (历史:条件路由时代曾担心"两条独立单源边让 GS 每轮跑两次 LLM" ——
    #      屏障 list 形式等齐所有来源才触发一次,不存在该竞态。)
    #   B) recommend_transition:2026-11-14 依赖约简阶段1 起为 SB→RTR 单源边
    #      (历史:2026-11-12 阶段1 曾改 [GC, SB] 双源屏障,修 GC→RTR 独立单源边
    #      在 BGM 缺席时提前触发的病;现 SB→RTR 唯一入边,病根随唯一入边消除,
    #      GC 源由 SB 祖先链传递保证)。
    #   C) plan_timeline_pro:不再直接依赖 GC —— 2026-11-14 约简后 PTP 屏障
    #      只含 [RTR, TXT],GC 由 RTR 祖先链传递保证。
    g.add_edge("storyline_group_clips", "storyline_script_template_recommendation")
    g.add_edge("storyline_group_clips", "storyline_search_web_topic")
    # [STR, WT] 双源屏障 → 文案:模板推荐与检索分支每轮都会到达,屏障必凑齐;
    # QA 重试轮两者都在回路内(qa → GC → A/A'),不会静默 END。
    g.add_edge(
        [
            "storyline_script_template_recommendation",
            "storyline_search_web_topic",
        ],
        "storyline_generate_script",
    )

    # generate_script 之后并行 fan-out(plan §2.2 §3;2026-11-12 起模板推荐已前置,
    # 从这里移除,改为上方 group_clips → 模板推荐前置):
    #   A) generate_voiceover
    #   B) recommend_text
    # select_bgm 不再单源直挂(边治理阶段1):原 GS→SB / GV→SB 两条独立单源边是
    # 「任一来源完成即触发」,GS 完成时 SB 提前跑一次、GV 完成后再跑一次
    # (黄金轨迹实测每轮 2 次),两次写同一 storyline_bgm_selection 互相覆盖。
    g.add_edge("storyline_generate_script", "storyline_generate_voiceover")
    g.add_edge("storyline_generate_script", "storyline_recommend_text")

    # select_bgm 单源边(2026-11-14 依赖约简阶段1,第5期计划 §4.1:传递性约简):
    # GV 已经由 GS→GV 普通边等待 GS,等 GV 即传递保证文案 Artifact 已写出,
    # 原 [GS, GV] 双源屏障中的 GS 源属重复约束,删除。保留 GV 源是必要的:
    # 2026-11-13 边审计任务B 起 SB 真实消费配音 —— 聚合 storyline_voiceover_artifact
    # 各段 duration 传给 select_bgm 做配乐时长覆盖偏好
    # (node_select_bgm._read_voiceover_total_ms);配音缺失/失败按 0 容缺省,
    # 不阻断 BGM。单源边不会重现 2026-11-12 屏障化之前的重复触发病 ——
    # 那个病根是同一目标存在多条独立单源边("任一来源完成即触发"),
    # 约简后 SB 只剩这一条入边,每轮恰触发一次。
    g.add_edge("storyline_generate_voiceover", "storyline_select_bgm")

    # recommend_transition 单源边(2026-11-14 依赖约简阶段1,第5期计划 §4.2):
    # SB 的祖先链已包含 GC(FC→GC→STR→GS→GV→SB),等 SB 即传递保证
    # storyline_groups_artifact 与 storyline_bgm_selection(RTR 的全部真实输入,
    # node_recommend_transition.py)就绪,原 [GC, SB] 双源屏障的 GC 源属重复
    # 约束,删除。历史上(2026-11-12 阶段1 之前)GC→RTR / SB→RTR 两条独立
    # 单源边曾导致 RTR 每轮 3 次 —— 现病根已随"目标唯一入边"消除。
    g.add_edge("storyline_select_bgm", "storyline_recommend_transition")

    # 所有 PTP 上游都到 plan_timeline_pro(plan §2.2):
    # 2026-11-14 依赖约简阶段1(第5期计划 §4.3):六源屏障缩为 [RTR, TXT] 两源。
    #   - RTR 一条链保证 groups、script、voiceover、BGM selection、transition
    #     plan 以及上游 media 链就绪(传递链 RTR←SB←GV←GS←STR←GC←FC←UC←SS←LM,
    #     RTR 自己还读取 BGM selection 与 transition plan);
    #   - TXT 单独保证 text_style_plan 就绪(TXT 是 GS 之后的独立 fan-out
    #     分支,RTR 链不含它 —— 这是不能只等 RTR 的唯一理由);
    #   - 不把 web topic 加进屏障:检索是条件节点,某些轮次不跑(D10 ——
    #     屏障永远凑不齐 → 图静默 END);PTP 也不直接读取检索产物。
    # 2026-10-20 修 D10:**SS(split_shots)被移出这个 barrier**(历史教训,
    #   保留作防回归记录):qa_gate 超时重试会回到 storyline_group_clips 重跑,
    #   SS 在重试点**上游**、不会重跑。barrier 渠道在首次触发后被清空,第二轮
    #   只有回路内节点会重新写入,SS 的名字永远补不回来 → PTP 再也不触发 →
    #   图在 recommend_transition 处**静默 END**(next=[]),worker 还报
    #   "completed"。SS 在 SS→understand_clips→filter_clips→GC 这条链上,
    #   GC 完成即蕴含 SS 完成。同理适用于一切"重试点上游"节点。
    #   (历史:2026-11-12 边治理阶段2 曾扩为 6 源 [GC,GS,GV,SB,RTR,TXT];
    #   2026-11-14 约简 —— 被删 4 源都在 RTR 祖先链上,重复等待不增加正确性。)
    g.add_edge(
        [
            "storyline_recommend_transition",
            "storyline_recommend_text",
        ],
        "storyline_plan_timeline_pro",
    )

    # 2026-10 AI 转场两节点(设计文档 §3.2:PTP/GAT → PTAT → qa 闸门)。
    #   - generate_ai_transition(GAT):单源边 RTR(2026-11-14 依赖约简阶段1,
    #     第5期计划 §4.4;历史:2026-11-12 边治理阶段3 曾为屏障 [GC, RTR])。
    #     GAT 只读 storyline_groups_artifact 与 storyline_transition_plan
    #     (作 hints 参考,node_generate_ai_transition.py),不读文案/配音/BGM
    #     —— RTR 完成即传递保证 groups(GC→…→SB→RTR 链)与 transition_plan
    #     (RTR 自产)就绪,GC 源属重复约束。单源边无重复触发风险:GAT 只有
    #     这一条入边。
    #   - plan_timeline_ai_transition(PTAT):屏障 [PTP, GAT],紧跟其后 ——
    #     两份输入来自互相独立的分支,不能约成单源。
    #   - qa_gate 只等最终时间线(阶段2 精简):AI 开时即 PTAT,qa 校验的就是
    #     含 AI 转场的最终时间线。
    # 开关在**建图时**读取(与 ASSEMBLY_QC_GATE_ENABLED 同类):
    #   关 → 不接这两个节点,拓扑与接入前完全一致;开 → 两个节点**每轮必跑**,
    #   所以放进屏障不会踩 D10(它们不是"有时跑有时不跑"的条件分支)。
    #   默认姿态(阶段3 固化):接线开关默认开 + 双门禁 fail-closed
    #   (STORYLINE_AI_TRANSITION_PROVIDER 默认空 / APPROVED 默认 false)→
    #   默认生产路径 0 次 Provider 外呼、不产生占位转场。
    # 两个节点内部都包了 try/except,失败只写 error_log,仍然正常返回 → 屏障照常凑齐。
    _plan_for_qa = "storyline_plan_timeline_pro"
    if is_ai_transition_enabled():
        g.add_edge(
            "storyline_recommend_transition",
            "storyline_generate_ai_transition",
        )
        g.add_edge(
            ["storyline_plan_timeline_pro", "storyline_generate_ai_transition"],
            "storyline_plan_timeline_ai_transition",
        )
        _plan_for_qa = "storyline_plan_timeline_ai_transition"

    # qa_gate 只等最终时间线节点(2026-11-12 边治理阶段2,设计文档 §5.7):
    # QA 实际只读取 storyline_timeline_plan 并校验 Canonical Timeline(qa_gate.py),
    # 不直接读 transition_plan / text_style_plan;PTP 已在上方屏障等待 RTR/TXT,
    # QA 再等它们属重复依赖 —— 精简为单源(AI 开=PTAT,关=PTP)。
    g.add_edge(_plan_for_qa, "storyline_qa_gate")

    # qa_gate 条件边:retry→ group_clips;通过 / 失败→ join(边治理阶段5:
    # render_video 预览旁支已整体删除 —— 子图交付物是 Canonical Timeline,
    # 最终 MP4 仍由外层 node_18 export_final_video 负责,附件 §5.9)
    g.add_conditional_edges(
        "storyline_qa_gate",
        _qa_gate_mod.route_after_storyline_qa,
        {
            "storyline_group_clips": "storyline_group_clips",
            "storyline_join": "storyline_join",
        },
    )

    # join → generate_draft(条件边)
    g.add_conditional_edges(
        "storyline_join",
        _route_after_storyline_join,
        {"generate_draft": "generate_draft", END: END},
    )

    # 兼容默认 graph 入边(notebook / 旧脚本可能直接 invoke 但不在测试范围)
    return g


# ---------------------------------------------------------------------------
# Week 5 辅助:生成/读取 heartbeat_id(节点需要时通过 state.get 读)
# ---------------------------------------------------------------------------
def new_heartbeat_id() -> str:
    """生成一个新的 heartbeat_id(Week 5 计划 §4.1)。"""
    return uuid.uuid4().hex