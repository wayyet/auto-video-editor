"""StateGraph 装配(2026-09 迁移解耦版,Week 5 拓扑)。

完整图拓扑(2026-09):
  START
    → clean_cache
    → launch_openstoryline      (本地 uvicorn + httpx 健康检查,无 MCP)
    → open_preview
    → checkpoint0_storyline_plan ⏸ interrupt("⓪")   (2026-09 新增;等人工在 OpenStoryline Web 完成规划)
    → import_and_plan           (读 openstoryline/outputs/<sid>/plan_timeline_pro/*.json)
    → generate_draft
    → node_06_human_reorder            ⏸ interrupt("①")
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
import os
import uuid
from pathlib import Path
from typing import Optional

from config import POSTGRES_URI, make_checkpointer, resolve_draft_dir
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
# 节点 4 之后路由(Week 2 已有,Week 3 保留;2026-09 关卡⓪ 后改为 storyline_plan or shot_plan)
# ---------------------------------------------------------------------------
def _route_after_import(state: WorkflowState) -> str:
    """关卡⓪ 后,产物应已写入;有 ``storyline_plan`` 或 ``shot_plan``(回退)就继续。"""
    if state.get("storyline_plan") or state.get("shot_plan"):
        return "generate_draft"
    return "__end__"


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
    """构造 StateGraph 拓扑(不 compile,便于单测替换 checkpointer)。"""
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

    # ---- Week 4 节点 + Week 5 拆分(7 个)----
    g.add_node("fork_draft_for_english_branch", fork_draft_for_english_branch)
    g.add_node("node_14_make_covers", node_14_make_covers)
    g.add_node("node_15_localize_covers_en", node_15_localize_covers_en)
    g.add_node("node_16a_translate_and_check", node_16a_translate_and_check)
    g.add_node("node_checkpoint3_layout_review", node_checkpoint3_layout_review)
    g.add_node("node_17_inject_english_tts_stub", node_17_inject_english_tts_stub)
    g.add_node("join_before_delivery", join_before_delivery)

    # ---- 边 ----
    # 节点 clean_cache 保留在图中(供 FireRed-OpenStoryline Web UI 的
    # "清理缓存" 按钮通过 POST /api/system/clean-cache 端点显式触发),
    # 但不再从 START 自动跑 — 避免每次 graph.invoke 都无谓删除
    # CACHE_PATHS_TO_CLEAN 中的剪映/OpenStoryline 临时目录。
    g.add_edge(START, "launch_openstoryline")
    g.add_edge("launch_openstoryline", "open_preview")
    # 关卡⓪ 串在 open_preview 与 import_and_plan 之间(2026-09 新增)
    g.add_edge("open_preview", "checkpoint0_storyline_plan")
    g.add_edge("checkpoint0_storyline_plan", "import_and_plan")
    g.add_conditional_edges(
        "import_and_plan",
        _route_after_import,
        {"generate_draft": "generate_draft", END: END},
    )
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

    return g


# ---------------------------------------------------------------------------
# Week 5 辅助:生成/读取 heartbeat_id(节点需要时通过 state.get 读)
# ---------------------------------------------------------------------------
def new_heartbeat_id() -> str:
    """生成一个新的 heartbeat_id(Week 5 计划 §4.1)。"""
    return uuid.uuid4().hex