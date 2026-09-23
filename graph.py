"""StateGraph 装配(2026-09 迁移解耦版,Week 5 拓扑 + Phase 4 auto-mode + 阶段 7 解耦 + Phase 5 assembly QC 通道)。

完整图拓扑(2026-09 + Phase 4 + Phase 5):
  START
    → clean_cache
    → launch_openstoryline      (human-mode:本地 uvicorn + httpx 健康检查;
                                  auto-mode:noop,plan §5 阶段 7)
    → open_preview
    → _route_mode(plan §2.1 双模分支):
        ├─ human(默认):checkpoint0_storyline_plan ⏸ interrupt("⓪")
        │                → import_and_plan  (读 vendored 产物)
        └─ auto:         storyline_load_media(19 节点确定性图入口)
    → generate_draft    (两条 mode 汇合点)
    → [Phase 5: ASSEMBLY_QC_GATE_ENABLED=True 时插入 6 节点 QC 通道(plan §七)]
        assembly_discover_and_probe
        → assembly_asr_and_visual_observe
        → assembly_build_timeline
        → assembly_validate_render_qc      🔁 条件边(route_after_assembly_qc):
            ├─→ assembly_repair_loop       (未达标且 retry < MAX)
            │     ↻ assembly_validate_render_qc
            └─→ assembly_write_report      (达标 / retry 达上限 → 软降级)
        → node_06_human_reorder            ⏸ interrupt("①")
      [ASSEMBLY_QC_GATE_ENABLED=False 时:generate_draft 直接到 node_06_human_reorder]
    → node_07_speed_fit                🔁 条件边(route_after_speed_fit):
      ├─→ node_08_add_subtitles  (达标,产出 snapshot2)
      ├─→ node_07_speed_fit      (未达标,retry_counts < MAX_RETRY)
      └─→ escalate_guardrail_failure → END  (重试超限)
    → bridge_snapshot2                 (写 snapshot2_path)
    → node_08_add_subtitles            (Week 4 同步写 asr_segments_zh 到 state)
    → node_09_inject_fx
    → node_10_inject_text_fx
    → node_11_inject_sticker
    → node_12_human_add_bgm            ⏸ interrupt("②")
    → node_13_adjust_volume
    ├─→ node_14_make_covers            (中文主线尾段)
    │     → node_15_localize_covers_en
    │           └─────────────┐
    │                         ▼
    └─→ [parallel fork_draft → node_16a_translate_and_check
                                ├─→ node_checkpoint3_layout_review ⏸ interrupt("③")(条件触发)
                                └─→ node_17_inject_english_tts_stub
                                                                 │
                                                  join_before_delivery
                                                                 ▼
                                                                END

2026-09 关键改动(对照 plan §4/§5):
- OpenStoryline 改为本地 uvicorn 子进程 + 读盘(node_02 不再有 MCP 链路,
  node_04 读 ``openstoryline/outputs/<sid>/plan_timeline_pro/``)。
- 新增关卡⓪ ``checkpoint0_storyline_plan``:在 ``open_preview`` 与
  ``import_and_plan`` 之间挂起,等人工在 OpenStoryline 网页完成规划后 resume,
  再走读盘路径。
- 关卡统一为 ``⓪/①/②/③``,``_route_after_import`` 改为
  ``storyline_plan or shot_plan`` → ``generate_draft``。

Phase 4 auto-mode(plan §2.1 / §5 阶段 0~5):
- ``_route_mode`` 条件边在 ``open_preview`` 之后做双模分支;
  ``STORYLINE_MODE=auto`` 时走新增 19 节点确定性图,产出
  ``storyline_timeline_plan`` 直接喂 ``generate_draft`` mapper。
- 默认 ``human`` 路径**完全不动**,172 unit / 关卡⓪ interrupt/resume
  不回归。

阶段 7 解耦(plan §5 阶段 7 / §7.3 验收):
- ``launch_openstoryline`` 节点在 ``auto`` 模式下**禁用 vendored Web UI**
  启动(返回 ``openstoryline_ready=True`` + ``node_02_auto_skipped`` 状态),
  减少无谓 uvicorn 子进程开销,呼应"auto-mode 不依赖 vendored Web UI"。
- ``_mcp_passthrough.py`` 已删除,19 节点全部走 ``storyline_capabilities/``
  本地能力层;``asr_runner.py`` 仍走 vendored venv 子进程调 funasr(主 venv
  不能装 torch),这是 ADR-001 阻塞项,**不**在阶段 7 删除范围内。
- human-mode 路径(checkpoint0_storyline_plan + node_04_import_and_plan)
  **保留**并加 ``[Phase 7 deprecation]`` 注释,不删 — 避免破坏老
  checkpoint 恢复 + 保留 auto-mode 失败时的回退入口(plan §6.2 选项 A)。

Week 5 关键改动(保留):
- ``node_16_translate_subtitles`` 拆为 ``node_16a_translate_and_check``(翻译 +
  layout 检测,无 interrupt) + ``node_checkpoint3_layout_review``(只做 interrupt,
  无副作用,天然幂等);条件边 ``route_after_translate`` 据 ``layout_issues_detected``
  二选一走向。
- 关卡①/② payload ``checkpoint`` 字段统一为 ``"①"`` / ``"②"``(Week 5 计划 §1.1),
  旧值保留在 ``legacy_id`` 字段。
- ``node_17_inject_english_tts_stub`` 写空 wav 占位 + ``en_audio_path`` 字段。

汇合:node_15 与 node_17 通过 ``add_edge([...], "join_before_delivery")`` 单次调用汇入,
LangGraph fan-in 自动等齐两条分支后才触发 join(避免多次独立 ``add_edge`` 造成的
重复触发,见验证报告 §5.1)。

关卡①/②/③ 使用 ``langgraph.types.interrupt``,需要 checkpointer(Week 3 用
SqliteSaver 做持久化,覆盖多日挂起恢复;Week 5 切到 PostgresSaver)。
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import uuid
from pathlib import Path
from typing import Optional

# Phase 4:用于在测试中临时覆盖 config.STORYLINE_MODE(不污染全局 env)
_STORYLINE_MODE_OVERRIDE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "STORYLINE_MODE_OVERRIDE", default=None
)

from config import (
    POSTGRES_URI,
    STORYLINE_MODE,
    make_checkpointer,
    resolve_draft_dir,
)
from monitoring.heartbeat_writer import start_heartbeat

# pre-flight 依赖只在用户显式打开时才 import,避免 84 条现有测试多走依赖链
PreflightConfig = None  # type: ignore[assignment]
PreflightError = None   # type: ignore[assignment]
from nodes.node_01_clean_cache import clean_cache
from nodes.node_02_launch_openstoryline import launch_openstoryline_service
from nodes.node_03_open_preview import open_preview
from nodes.node_04_import_and_plan import import_video_and_plan_shots
from nodes.node_05_generate_draft import generate_initial_jianying_draft
from nodes.node_06_human_reorder import human_reorder
from nodes.node_07_speed_fit import get_snapshot2_path, route_after_speed_fit, speed_fit
from nodes.node_08_add_subtitles import add_subtitles
from nodes.node_09_inject_fx import inject_fx
from nodes.node_10_inject_text_fx import inject_text_fx
from nodes.node_11_inject_sticker import inject_sticker
from nodes.node_12_human_add_bgm import human_add_bgm
from nodes.node_13_adjust_volume import adjust_volume
from nodes.node_14_make_covers import node_14_make_covers
from nodes.node_15_localize_covers_en import node_15_localize_covers_en
from nodes.node_16a_translate_and_check import node_16a_translate_and_check
from nodes.node_checkpoint0_storyline_plan import checkpoint0_wait_storyline_plan
from nodes.node_checkpoint3_layout_review import node_checkpoint3_layout_review
from nodes.node_17_inject_english_tts_stub import node_17_inject_english_tts_stub
from nodes.node_fork_english_branch import fork_draft_for_english_branch
from nodes.node_join_before_delivery import join_before_delivery
from state import WorkflowState

# ---------------------------------------------------------------------------
# Phase 5:assembly QC 通道节点(plan §七 / ADR-1~5 / §十 阶段四)
# ---------------------------------------------------------------------------
# 6 个新节点:discover → asr/observe → build_timeline → validate/render/qc
# → repair_loop (条件) → write_report。所有节点都注册到图中,但当
# ``ASSEMBLY_QC_GATE_ENABLED=false`` 时不接边,等价于改动前状态。
from nodes.assembly import (
    assembly_asr_and_visual_observe_node,
    assembly_build_timeline_node,
    assembly_discover_and_probe_node,
    assembly_repair_loop_node,
    assembly_validate_render_qc_node,
    assembly_write_report_node,
    route_after_assembly_qc,
)

# ---------------------------------------------------------------------------
# Phase 4:剧情线模式路由(plan_v4 §2.1 / §5 阶段 0)
# ---------------------------------------------------------------------------
# 双模分支点 — ``open_preview`` 之后立刻二选一:
# - ``human``:保持现有 17 节点 + 关卡⓪(**完全不动**,172 unit / 中断恢复不回归)
# - ``auto``:走新增 19 节点确定性图(plan_v4 §2.2)
# 19 节点的壳子全部 ``add_node`` 加入图,但只在 ``auto`` 模式下连通。
from nodes.storyline import (
    qa_gate as _qa_gate_mod,
    join_storyline as _join_storyline_mod,
)
from nodes.storyline.node_load_media import storyline_load_media_node
from nodes.storyline.node_search_media import storyline_search_media_node
from nodes.storyline.node_search_web_topic import storyline_search_web_topic_node
from nodes.storyline.node_split_shots import storyline_split_shots_node
from nodes.storyline.node_local_asr import storyline_local_asr_node
from nodes.storyline.node_speech_rough_cut import storyline_speech_rough_cut_node
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
from nodes.storyline.node_render_video import storyline_render_video_node


# ---------------------------------------------------------------------------
# 节点 4 之后路由(Week 2 已有,Week 3 保留;2026-09 关卡⓪ 后改为 storyline_plan or shot_plan)
# ---------------------------------------------------------------------------
def _route_after_import(state: WorkflowState) -> str:
    """关卡⓪ 后,产物应已写入;有 ``storyline_plan`` 或 ``shot_plan``(回退)就继续。"""
    if state.get("storyline_plan") or state.get("shot_plan"):
        return "generate_draft"
    return "__end__"


# ---------------------------------------------------------------------------
# Phase 4:auto-mode 19 节点图的条件路由(plan_v4 §5 阶段 0)
# ---------------------------------------------------------------------------
def _route_mode(state: WorkflowState) -> str:
    """open_preview 之后的双模分支:返回 ``"checkpoint0_storyline_plan"`` 或
    ``"storyline_load_media"``,由 ``config.STORYLINE_MODE`` 控制。

    默认 ``"human"`` 完全保留现有路径;``"auto"`` 走新 19 节点确定性图。

    注意:build_graph 可显式接受 ``storyline_mode_override`` 参数强制覆盖,
    便于测试在同一个进程内跑两种 mode。
    """
    mode = _STORYLINE_MODE_OVERRIDE.get() or STORYLINE_MODE
    if mode == "auto":
        return "storyline_load_media"
    return "checkpoint0_storyline_plan"


def _route_after_storyline_join(state: WorkflowState) -> str:
    """storyline_join 之后的条件路由(对齐 ``_route_after_import`` 的语义)。

    - 有 ``storyline_plan``(join_storyline 已反序列化到 state)→ ``generate_draft``
    - 否则 → ``END``(空 plan 与 qa_gate 失败强制 continue 的语义一致)
    """
    if state.get("storyline_plan") or state.get("shot_plan"):
        return "generate_draft"
    return "__end__"


def _storyline_route_after_load_media(state: WorkflowState) -> str:
    """storyline_load_media 之后的条件路由(plan §2.2 第 96/97 行)。

    阶段 0 默认走 ``split_shots`` 主干,``search_media`` 旁路仅当 ``Pexels Key``
    已配(读 ``STORYLINE_PEXELS_API_KEY``);不在 stub 模式启用。
    """
    if os.environ.get("STORYLINE_PEXELS_API_KEY", "").strip():
        return "storyline_search_media"
    return "storyline_split_shots"


def _storyline_route_after_split_shots(state: WorkflowState) -> str:
    """storyline_split_shots 之后的条件路由(plan §2.2 第 99/100 行)。

    阶段 0 默认走 ``understand_clips`` 主干;``local_asr`` 旁路仅当
    ``media_artifact`` 里识别到至少一个 media 有 audio stream(读 ``has_audio``)。
    阶段 1 才完整检测。
    """
    media_artifact = state.get("storyline_media_artifact")
    if media_artifact:
        # 阶段 0 stub 模式不真正解析文件,保守直接走主干
        return "storyline_understand_clips"
    return "storyline_understand_clips"


# ---------------------------------------------------------------------------
# Week 5 条件边:步骤 16a 之后是否触发关卡③
# ---------------------------------------------------------------------------
def route_after_translate(state: WorkflowState) -> str:
    """node_16a_translate_and_check 之后的条件路由。

    - ``layout_issues_detected`` 为真 → 关卡③(``node_checkpoint3_layout_review``)
    - 否则 → ``node_17_inject_english_tts_stub``
    """
    if state.get("layout_issues_detected"):
        return "node_checkpoint3_layout_review"
    return "node_17_inject_english_tts_stub"


# ---------------------------------------------------------------------------
# 升级节点:节点 7 重试超限后的终止点
# ---------------------------------------------------------------------------
def escalate_guardrail_failure(state: WorkflowState) -> dict:
    """节点 7 连续 3 次重试仍未达 35s 时的兜底:写 error_log + 终止。"""
    errors = list(state.get("error_log", []) or []) + [
        "[node_07] 护栏节点连续 3 次重试仍未将总时长压到 35s 以内,流程升级",
    ]
    log = list(state.get("status_log", []) or []) + ["guardrail_failed"]
    return {**state, "error_log": errors, "status_log": log}


# ---------------------------------------------------------------------------
# 节点 7 之后桥接 snapshot2_path:把快照②路径写入 state(条件边不返回 state)
# ---------------------------------------------------------------------------
def bridge_snapshot2(state: WorkflowState) -> dict:
    """条件边不能改 state —— 把 snapshot2 路径读出写到 state,再交给节点 8。"""
    snap = get_snapshot2_path(state)
    return {**state, "snapshot2_path": snap} if snap else state


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
    1. ``AsyncPostgresSaver.from_conn_string(POSTGRES_URI)`` — 内部起连接池
    2. ``await saver.setup()`` — 首次使用建表(checkpoints / checkpoint_blobs /
       checkpoint_writes),幂等
    3. yield saver 供 build_graph 使用
    4. context manager 退出时 saver 自行清理

    Raises:
        RuntimeError: 未安装 ``langgraph-checkpoint-postgres``。
        ValueError: ``POSTGRES_URI`` 未配置或格式错误。
    """
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as e:
        raise RuntimeError(
            "Postgres checkpointer 需要 langgraph-checkpoint-postgres 包,"
            "请先 pip install langgraph-checkpoint-postgres psycopg[binary,pool]"
        ) from e

    uri = os.environ.get("POSTGRES_URI", POSTGRES_URI)
    if not uri:
        raise ValueError("POSTGRES_URI 环境变量未设置")

    # AsyncPostgresSaver.from_conn_string 返回一个 AsyncContextManager(可作 async with)
    # 但我们要让外层直接 yield saver,需要在内部 __aenter__ 拿到实例
    saver_cm = AsyncPostgresSaver.from_conn_string(uri)
    async with saver_cm as saver:
        # 首次使用建表(幂等);Week 5 计划 §3.3 "破坏性变更应急" 文档明示
        await saver.setup()
        yield saver


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
        checkpointer: 已构造的 checkpointer,None 时按 config 选 SqliteSaver/InMemorySaver。
        thread_id: sqlite 模式下决定落盘文件名。
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
        PreflightError: pre-flight 任一步骤失败时抛出。
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

    if checkpointer is None:
        checkpointer = make_checkpointer(thread_id=thread_id)

    if start_heartbeat_thread:
        try:
            start_heartbeat()
        except Exception:  # noqa: BLE001 — 心跳失败不应阻塞图编译
            pass

    g = _build_state_graph()
    return g.compile(checkpointer=checkpointer)


def _build_state_graph():
    """构造 StateGraph 拓扑(不 compile,便于单测替换 checkpointer)。

    Phase 4(plan_v4 §2.1 / §2.2 / §5 阶段 0)新增:
    - ``_route_mode`` 条件边在 ``open_preview`` 之后做双模分支。
    - ``human`` mode:走原 17 节点路径(关卡⓪ + import_and_plan),**完全不动**。
    - ``auto`` mode:走新 19 节点确定性图,19 节点 / qa_gate / join_storyline 全
      部 ``add_node`` 注册,内部拓扑按 plan §2.2 连边;``auto`` 模式从
      ``open_preview`` 直达 ``storyline_load_media``,绕过关卡⓪ 与
      ``import_and_plan``。
    """
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(WorkflowState)

    # ---- 节点 1-5 (Week 2 已有,节点 5 用 wrapper 注入 draft_dir) ----
    from functools import partial

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

    g.add_node("clean_cache", clean_cache)
    g.add_node("launch_openstoryline", launch_openstoryline_service)
    g.add_node("open_preview", open_preview)
    # 关卡⓪:等人工在 OpenStoryline 网页里完成分镜/文案/BGM/时间线规划(2026-09 迁移后新增)
    g.add_node("checkpoint0_storyline_plan", checkpoint0_wait_storyline_plan)
    g.add_node("import_and_plan", import_video_and_plan_shots)
    g.add_node("generate_draft", generate_draft_wrapped)

    # ---- 关卡① + 护栏 + 关卡② (Week 3) ----
    g.add_node("node_06_human_reorder", human_reorder)
    g.add_node("node_07_speed_fit", speed_fit)
    g.add_node("escalate_guardrail_failure", escalate_guardrail_failure)
    g.add_node("bridge_snapshot2", bridge_snapshot2)
    g.add_node("node_08_add_subtitles", add_subtitles)
    g.add_node("node_09_inject_fx", inject_fx)
    g.add_node("node_10_inject_text_fx", inject_text_fx)
    g.add_node("node_11_inject_sticker", inject_sticker)
    g.add_node("node_12_human_add_bgm", human_add_bgm)
    g.add_node("node_13_adjust_volume", adjust_volume)

    # ---- Phase 5:assembly QC 通道(plan §七 / §十 阶段四)----
    # ADR-3:默认 ASSEMBLY_QC_GATE_ENABLED=True → 6 节点全部接进图;
    # 设为 False 时只 add_node 不接边,等价于改动前状态,human-mode 关卡①
    # 不会突然被打破。
    g.add_node("assembly_discover_and_probe", assembly_discover_and_probe_node)
    g.add_node("assembly_asr_and_visual_observe", assembly_asr_and_visual_observe_node)
    g.add_node("assembly_build_timeline", assembly_build_timeline_node)
    g.add_node("assembly_validate_render_qc", assembly_validate_render_qc_node)
    g.add_node("assembly_repair_loop", assembly_repair_loop_node)
    g.add_node("assembly_write_report", assembly_write_report_node)

    # ---- Week 4 节点 + Week 5 拆分(7 个)----
    g.add_node("fork_draft_for_english_branch", fork_draft_for_english_branch)
    g.add_node("node_14_make_covers", node_14_make_covers)
    g.add_node("node_15_localize_covers_en", node_15_localize_covers_en)
    g.add_node("node_16a_translate_and_check", node_16a_translate_and_check)
    g.add_node("node_checkpoint3_layout_review", node_checkpoint3_layout_review)
    g.add_node("node_17_inject_english_tts_stub", node_17_inject_english_tts_stub)
    g.add_node("join_before_delivery", join_before_delivery)

    # ---- Phase 4:auto-mode 19 节点 + qa_gate + join_storyline(plan_v4 §2.2 / §2.3)----
    # 所有 storyline 节点在两种 mode 下都注册成图节点(便于检查点兼容);
    # 但仅在 ``auto`` 模式下通过 _route_mode 条件边连通,``human`` 模式下完全
    # 不进入这些节点,172 unit / 关卡⓪ interrupt/resume 不回归。
    g.add_node("storyline_load_media", storyline_load_media_node)
    g.add_node("storyline_search_media", storyline_search_media_node)
    g.add_node("storyline_search_web_topic", storyline_search_web_topic_node)
    g.add_node("storyline_split_shots", storyline_split_shots_node)
    g.add_node("storyline_local_asr", storyline_local_asr_node)
    g.add_node("storyline_speech_rough_cut", storyline_speech_rough_cut_node)
    g.add_node("storyline_generate_ai_transition",
               storyline_generate_ai_transition_node)
    g.add_node("storyline_understand_clips", storyline_understand_clips_node)
    g.add_node("storyline_filter_clips", storyline_filter_clips_node)
    g.add_node("storyline_group_clips", storyline_group_clips_node)
    g.add_node("storyline_generate_script", storyline_generate_script_node)
    g.add_node("storyline_script_template_recommendation",
               storyline_script_template_recommendation_node)
    g.add_node("storyline_generate_voiceover", storyline_generate_voiceover_node)
    g.add_node("storyline_select_bgm", storyline_select_bgm_node)
    g.add_node("storyline_recommend_transition", storyline_recommend_transition_node)
    g.add_node("storyline_recommend_text", storyline_recommend_text_node)
    g.add_node("storyline_plan_timeline_pro", storyline_plan_timeline_pro_node)
    g.add_node("storyline_plan_timeline_ai_transition",
               storyline_plan_timeline_ai_transition_node)
    g.add_node("storyline_render_video", storyline_render_video_node)
    g.add_node("storyline_qa_gate", _qa_gate_mod.storyline_qa_gate_node)
    g.add_node("storyline_join", _join_storyline_mod.storyline_join_node)

    # ---- 边 ----
    # 节点 clean_cache 保留在图中(供 FireRed-OpenStoryline Web UI 的
    # "清理缓存" 按钮通过 POST /api/system/clean-cache 端点显式触发),
    # 但不再从 START 自动跑 — 避免每次 graph.invoke 都无谓删除
    # CACHE_PATHS_TO_CLEAN 中的剪映/OpenStoryline 临时目录。
    g.add_edge(START, "launch_openstoryline")
    g.add_edge("launch_openstoryline", "open_preview")

    # Phase 4:open_preview 之后做双模分支(plan_v4 §2.1 / §5 阶段 0)
    g.add_conditional_edges(
        "open_preview",
        _route_mode,
        {
            "checkpoint0_storyline_plan": "checkpoint0_storyline_plan",
            "storyline_load_media": "storyline_load_media",
        },
    )

    # human-mode 路径(关卡⓪ + node_04)
    # [Phase 7 deprecation] plan §5 阶段 7:human-mode 路径仅作
    # auto-mode 失败时的回退入口 + 老 checkpoint 恢复兜底保留,不删。
    # 新功能请优先走 auto-mode(19 节点确定性图)。
    g.add_edge("checkpoint0_storyline_plan", "import_and_plan")
    g.add_conditional_edges(
        "import_and_plan",
        _route_after_import,
        {"generate_draft": "generate_draft", END: END},
    )

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
        g.add_edge("assembly_discover_and_probe", "assembly_asr_and_visual_observe")
        g.add_edge("assembly_asr_and_visual_observe", "assembly_build_timeline")
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

    # 节点 7 条件边:达标走桥接 → 节点 8;未达标自循环或升级
    g.add_conditional_edges(
        "node_07_speed_fit",
        route_after_speed_fit,
        {
            "node_08_add_subtitles": "bridge_snapshot2",
            "node_07_speed_fit": "node_07_speed_fit",
            "escalate_guardrail_failure": "escalate_guardrail_failure",
        },
    )
    g.add_edge("escalate_guardrail_failure", END)
    g.add_edge("bridge_snapshot2", "node_08_add_subtitles")

    # 8 → 9 → 10 → 11 → 12 → 13
    g.add_edge("node_08_add_subtitles", "node_09_inject_fx")
    g.add_edge("node_09_inject_fx", "node_10_inject_text_fx")
    g.add_edge("node_10_inject_text_fx", "node_11_inject_sticker")
    g.add_edge("node_11_inject_sticker", "node_12_human_add_bgm")
    g.add_edge("node_12_human_add_bgm", "node_13_adjust_volume")

    # ---- Week 4 双分支 + 汇合 ----
    # 中文主线尾段:13 → 14 → 15 → join
    g.add_edge("node_13_adjust_volume", "node_14_make_covers")
    g.add_edge("node_14_make_covers", "node_15_localize_covers_en")

    # Week 5 英文分支:bridge_snapshot2 → fork → 16a → [checkpoint3 ⏸ | 17] → join
    g.add_edge("bridge_snapshot2", "fork_draft_for_english_branch")
    g.add_edge("fork_draft_for_english_branch", "node_16a_translate_and_check")
    g.add_conditional_edges(
        "node_16a_translate_and_check",
        route_after_translate,
        {
            "node_checkpoint3_layout_review": "node_checkpoint3_layout_review",
            "node_17_inject_english_tts_stub": "node_17_inject_english_tts_stub",
        },
    )
    g.add_edge("node_checkpoint3_layout_review", "node_17_inject_english_tts_stub")

    # 汇合:中文主线(node_15)与英文分支(node_17)都到达才触发 join。
    # 用列表语法,fan-in 自动等齐,join 只跑一次(对照验证报告 §5.1)。
    g.add_edge(
        ["node_15_localize_covers_en", "node_17_inject_english_tts_stub"],
        "join_before_delivery",
    )

    # join → END
    g.add_edge("join_before_delivery", END)

    # ---- Phase 4:auto-mode 19 节点图边(plan_v4 §2.2 / §5 阶段 0)----
    # 起点:``_route_mode`` 条件边已把 open_preview 指向 ``storyline_load_media``。
    # 阶段 0 拓扑(简版;plan §2.2 完整版待阶段 5 完善):

    # LM 后两条路:
    #   A) Pexels Key 已配 → search_media → LM(条件边,可选循环)
    #   B) 主干 → split_shots
    # 阶段 0 默认走 B(stub 模式无条件),search_media 暂作旁路,**不** 串回 LM
    # (避免单测复杂度;阶段 1+ 完善)。
    g.add_conditional_edges(
        "storyline_load_media",
        _storyline_route_after_load_media,
        {
            "storyline_search_media": "storyline_search_media",
            "storyline_split_shots": "storyline_split_shots",
        },
    )
    g.add_edge("storyline_search_media", "storyline_split_shots")

    # split_shots 出 2 个分支(并行 fan-out):
    #   A) understand_clips 主路径
    #   B) local_asr 条件(有语音)→ speech_rough_cut(目前阶段 0 为旁支)
    g.add_conditional_edges(
        "storyline_split_shots",
        _storyline_route_after_split_shots,
        {
            "storyline_understand_clips": "storyline_understand_clips",
            "storyline_local_asr": "storyline_local_asr",
        },
    )
    g.add_edge("storyline_local_asr", "storyline_speech_rough_cut")

    # understand_clips → filter_clips → group_clips(主线)
    g.add_edge("storyline_understand_clips", "storyline_filter_clips")
    g.add_edge("storyline_filter_clips", "storyline_group_clips")

    # group_clips → 两路:
    #   A) generate_script(文案)
    #   B) recommend_transition(转场) — 等 select_bgm 完成后才走
    #   C) plan_timeline_pro:在下面 fan-in 列表形式统一加入
    g.add_edge("storyline_group_clips", "storyline_generate_script")
    g.add_edge("storyline_group_clips", "storyline_recommend_transition")

    # generate_script 之后并行 fan-out(plan §2.2 §3):
    #   A) script_template_recommendation
    #   B) generate_voiceover
    #   C) select_bgm
    #   D) recommend_text
    g.add_edge("storyline_generate_script", "storyline_script_template_recommendation")
    g.add_edge("storyline_generate_script", "storyline_generate_voiceover")
    g.add_edge("storyline_generate_script", "storyline_select_bgm")
    g.add_edge("storyline_generate_script", "storyline_recommend_text")

    # voiceover 完成后 join select_bgm(便于配音时长对齐)
    g.add_edge("storyline_generate_voiceover", "storyline_select_bgm")

    # select_bgm 完成后 join recommend_transition(plan §2.2:SB→RTR)
    g.add_edge("storyline_select_bgm", "storyline_recommend_transition")

    # 所有 PTP 上游都到 plan_timeline_pro(plan §2.2):
    #   SS, GC, GS, GV, SB 都到 PTP(fan-in)。用 list 形式语义(对照
    #   ``join_before_delivery`` 注释),LangGraph 0.2 才会等所有 sources 完成才
    #   触发目标一次;独立 ``add_edge`` 是「任一 source 完成就触发」,会导致
    #   PTP 在 SS 完成时就跑,GS/GV/SB 还没产出 — 与 plan §2.2 拓扑不符。
    g.add_edge(
        [
            "storyline_split_shots",
            "storyline_group_clips",
            "storyline_generate_script",
            "storyline_generate_voiceover",
            "storyline_select_bgm",
        ],
        "storyline_plan_timeline_pro",
    )

    # qa_gate 也是 fan-in:PTP + RTR + RT(plan §2.2)。同样改 list 形式。
    g.add_edge(
        [
            "storyline_plan_timeline_pro",
            "storyline_recommend_transition",
            "storyline_recommend_text",
        ],
        "storyline_qa_gate",
    )

    # qa_gate 条件边:retry→ group_clips;通过 / 失败→ render_video(条件)→ join
    g.add_conditional_edges(
        "storyline_qa_gate",
        _qa_gate_mod.route_after_storyline_qa,
        {
            "storyline_group_clips": "storyline_group_clips",
            "storyline_render_video": "storyline_render_video",
        },
    )

    # render_video 旁支:默认 noop → join;不接入下游(plan §2.2 旁支 + ADR-006)
    g.add_edge("storyline_render_video", "storyline_join")

    # join → generate_draft(条件边;与 _route_after_import 同语义)
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