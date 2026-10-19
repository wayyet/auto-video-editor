"""D7 / D10 回归单测:草稿缺失时的优雅降级 + qa_gate 重试回环不再死锁。

背景(2026-10-19 P8 实跑):
- **D7**:``node_05`` 早退时 ``state["draft_path"]`` 是 None,``node_07`` 直接
  ``Path(state["draft_path"])`` → ``TypeError`` 把整个 worker 带走。
  现在这些节点都要能降级跳过。
- **D10**:``plan_timeline_pro`` 的 fan-in barrier 里含 ``split_shots``,
  而 qa_gate 超时重试回到 ``group_clips``(split_shots 的**下游**),
  split_shots 不会重跑 → barrier 永远补不齐 → 图在 recommend_transition 处
  **静默 END**(``next=[]``),worker 还报 "completed"。
"""

from __future__ import annotations

from typing import Any

import pytest

from nodes.node_07_speed_fit import (
    get_snapshot2_path,
    route_after_speed_fit,
    speed_fit,
)
from nodes.node_08_add_subtitles import add_subtitles
from nodes.node_09_inject_fx import inject_fx
from nodes.node_10_inject_text_fx import inject_text_fx


def _state_without_draft(**extra: Any) -> dict[str, Any]:
    """draft_path 为 None 的 state(node_05 早退后的样子)。"""
    state: dict[str, Any] = {
        "session_id": "t1",
        "draft_path": None,
        "status_log": [],
        "error_log": [],
    }
    state.update(extra)
    return state


# ---------------------------------------------------------------------------
# D7:draft_path=None 时不抛异常
# ---------------------------------------------------------------------------
def test_speed_fit_skips_when_draft_missing() -> None:
    out = speed_fit(_state_without_draft())
    assert "node_07_speed_fit_skipped" in (out.get("status_log") or [])
    assert any("draft_path" in e for e in (out.get("error_log") or []))


def test_route_after_speed_fit_passes_through_without_draft() -> None:
    """没有草稿时不能抛 TypeError,应放行到 node_08 让它自己降级。"""
    assert route_after_speed_fit(_state_without_draft()) == "node_08_add_subtitles"


def test_get_snapshot2_path_returns_none_without_draft() -> None:
    assert get_snapshot2_path(_state_without_draft()) is None


def test_add_subtitles_skips_when_draft_missing() -> None:
    out = add_subtitles(_state_without_draft())
    assert "node_08_add_subtitles_skipped" in (out.get("status_log") or [])
    assert any("draft_path" in e for e in (out.get("error_log") or []))


def test_inject_text_fx_skips_when_draft_missing() -> None:
    out = inject_text_fx(_state_without_draft())
    assert "node_10_inject_text_fx_skipped" in (out.get("status_log") or [])
    assert any("draft_path" in e for e in (out.get("error_log") or []))


def test_inject_fx_skips_when_draft_missing() -> None:
    """node_09 原来抛 FileNotFoundError,现在降级跳过。"""
    out = inject_fx(_state_without_draft())
    assert "node_09_inject_fx_skipped" in (out.get("status_log") or [])
    assert any("draft_path" in e for e in (out.get("error_log") or []))


# ---------------------------------------------------------------------------
# D10:qa_gate 重试回环不能死锁
# ---------------------------------------------------------------------------
def test_plan_timeline_pro_fan_in_excludes_split_shots() -> None:
    """D10 回归:PTP 的 fan-in 不能含 split_shots。

    split_shots 在 qa_gate 重试点(group_clips)的**上游**,不会重跑;
    留在 barrier 里 → 第二轮补不齐 → 图静默 END。
    """
    from graph import build_graph

    graph = build_graph(thread_id="topo-probe", start_heartbeat_thread=False, run_preflight=False)
    edges = graph.get_graph().edges

    sources_of_ptp: set[str] = set()
    for edge in edges:
        if edge.target == "storyline_plan_timeline_pro":
            sources_of_ptp.add(edge.source)

    assert "storyline_split_shots" not in sources_of_ptp, (
        "split_shots 留在 PTP 的 fan-in 里会让 qa_gate 重试死锁(D10)"
    )
    # 重试点上的四个上游必须在,否则第一次就会提前触发 PTP
    for required in (
        "storyline_group_clips",
        "storyline_generate_script",
        "storyline_generate_voiceover",
        "storyline_select_bgm",
    ):
        assert required in sources_of_ptp


def test_qa_gate_retry_edge_still_points_at_group_clips() -> None:
    """重试边本身不能改(仍回到 group_clips)。"""
    from nodes.storyline.qa_gate import route_after_storyline_qa

    assert (
        route_after_storyline_qa({"status_log": ["storyline_qa_retry"]})
        == "storyline_group_clips"
    )
    assert (
        route_after_storyline_qa({"status_log": ["storyline_qa_done"]})
        == "storyline_render_video"
    )
