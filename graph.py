"""StateGraph 装配(Week 4,对照附件 3.4 节 + 第 4 周计划 §5)。

完整图拓扑(Week 4):
  START
    → clean_cache
    → launch_openstoryline
    → open_preview
    → import_and_plan
    → generate_draft
    → node_06_human_reorder            ⏸ interrupt("checkpoint1")
    → node_07_speed_fit                🔁 条件边(route_after_speed_fit):
      ├─→ node_08_add_subtitles  (达标,产出 snapshot2)
      ├─→ node_07_speed_fit      (未达标,retry_counts < MAX_RETRY)
      └─→ escalate_guardrail_failure → END  (重试超限)
    → bridge_snapshot2                 (写 snapshot2_path)
    → node_08_add_subtitles            (Week 4 同步写 asr_segments_zh 到 state)
    → node_09_inject_fx
    → node_10_inject_text_fx
    → node_11_inject_sticker
    → node_12_human_add_bgm            ⏸ interrupt("checkpoint2")
    → node_13_adjust_volume
    ├─→ node_14_make_covers            (中文主线尾段)
    │     → node_15_localize_covers_en
    │           └─────────────┐
    │                         ▼
    └─→ [parallel fork_draft → node_16(关卡③ interrupt) → node_17]
                         ▼
                  join_before_delivery
                         ▼
                        END

LangGraph 自动 fan-in:node_15 与 node_17 都有出边指向 join_before_delivery,
等两分支都到达才触发 join。

关卡①/②/③ 使用 ``langgraph.types.interrupt``,需要 checkpointer(Week 3 用
SqliteSaver 做持久化,覆盖多日挂起恢复)。
"""

from __future__ import annotations

from pathlib import Path

from config import make_checkpointer, resolve_draft_dir
from monitoring.heartbeat_writer import start_heartbeat
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
from nodes.node_16_translate_subtitles import node_16_translate_subtitles
from nodes.node_17_inject_english_tts_stub import node_17_inject_english_tts_stub
from nodes.node_fork_english_branch import fork_draft_for_english_branch
from nodes.node_join_before_delivery import join_before_delivery
from state import WorkflowState


# ---------------------------------------------------------------------------
# 节点 4 之后路由(Week 2 已有,Week 3 保留)
# ---------------------------------------------------------------------------
def _route_after_import(state: WorkflowState) -> str:
    if state.get("openstoryline_ready") and state.get("shot_plan"):
        return "generate_draft"
    return "__end__"


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
# 图构建
# ---------------------------------------------------------------------------
def build_graph(checkpointer=None, *, thread_id: str = "default", start_heartbeat_thread: bool = True):
    """装配 13 节点 + 1 升级节点 + 1 桥接节点的完整图。

    Args:
        checkpointer: 已构造的 checkpointer,None 时按 config 选 SqliteSaver/InMemorySaver。
        thread_id: sqlite 模式下决定落盘文件名。
        start_heartbeat_thread: 是否同时启动心跳写线程(默认 True)。
            单测可传 False 避免 IO。

    Returns:
        CompiledStateGraph 实例。
    """
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

    # ---- Week 4 新增节点(6 个)----
    g.add_node("fork_draft_for_english_branch", fork_draft_for_english_branch)
    g.add_node("node_14_make_covers", node_14_make_covers)
    g.add_node("node_15_localize_covers_en", node_15_localize_covers_en)
    g.add_node("node_16_translate_subtitles", node_16_translate_subtitles)
    g.add_node("node_17_inject_english_tts_stub", node_17_inject_english_tts_stub)
    g.add_node("join_before_delivery", join_before_delivery)

    # ---- 边 ----
    g.add_edge(START, "clean_cache")
    g.add_edge("clean_cache", "launch_openstoryline")
    g.add_edge("launch_openstoryline", "open_preview")
    g.add_edge("open_preview", "import_and_plan")
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
    g.add_edge("node_15_localize_covers_en", "join_before_delivery")

    # 英文分支:bridge_snapshot2 → fork → 16(关卡③ interrupt)→ 17 → join
    g.add_edge("bridge_snapshot2", "fork_draft_for_english_branch")
    g.add_edge("fork_draft_for_english_branch", "node_16_translate_subtitles")
    g.add_edge("node_16_translate_subtitles", "node_17_inject_english_tts_stub")
    g.add_edge("node_17_inject_english_tts_stub", "join_before_delivery")

    # join → END(LangGraph 自动等两分支都到达)
    g.add_edge("join_before_delivery", END)

    return g