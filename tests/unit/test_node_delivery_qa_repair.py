"""delivery_qa_repair / delivery_qa_failed / route_after_join_qa 单测(阶段 6)。

覆盖计划 §8 阶段 6 任务 4 的阻断路由语义:
- gate 通过 → export;
- gate 失败未修复 → repair(只重跑可修复类别,写 retry 计数);
- gate 失败已修复过 → delivery_qa_failed raise(终止为 run.failed,不静默 END)。
"""

from __future__ import annotations

import pytest

from graph import route_after_join_qa
from nodes.node_delivery_qa_repair import (
    DELIVERY_QA_MAX_REPAIRS,
    _blocked_categories,
    _build_dispatch,
    delivery_qa_failed,
    delivery_qa_repair,
)


def _state(gate_failed: bool, retry: int | None = None, categories: dict | None = None) -> dict:
    state: dict = {
        "join_qa_report": {
            "gate_failed": gate_failed,
            "required_features": {
                "subtitles_zh": True, "subtitles_en": True, "voiceover_zh": True,
                "voiceover_en": True, "bgm": False, "transitions": True,
                "text_animation": True, "stickers": False, "volume_fades": True,
            },
            "categories": categories or {},
        },
        "status_log": [],
    }
    if retry is not None:
        state["delivery_qa_retry_count"] = retry
    return state


def test_max_repairs_is_one() -> None:
    """修复上限 1 次:repair → join 复检一轮,再失败即终止。"""
    assert DELIVERY_QA_MAX_REPAIRS == 1


def test_route_gate_ok_exports() -> None:
    assert route_after_join_qa(_state(gate_failed=False)) == "export_final_video"


def test_route_gate_failed_routes_to_repair() -> None:
    assert route_after_join_qa(_state(gate_failed=True)) == "delivery_qa_repair"


def test_route_gate_failed_after_retry_terminates() -> None:
    assert route_after_join_qa(_state(gate_failed=True, retry=1)) == "delivery_qa_failed"


def test_blocked_categories_only_required_failed_or_degraded() -> None:
    state = _state(
        gate_failed=True,
        categories={
            "transitions": {"status": "failed", "required": True},
            "stickers": {"status": "failed", "required": False},  # 未 required 不算
            "voiceover_zh": {"status": "degraded", "required": True},
            "subtitles_zh": {"status": "not_applicable", "required": True},
        },
    )
    assert _blocked_categories(state) == ["voiceover_zh", "transitions"]


def test_dispatch_covers_all_repairable_categories() -> None:
    dispatch = _build_dispatch()
    assert set(dispatch.keys()) == {"transitions", "text_animation", "stickers", "volume_fades"}
    # 数据性缺失类别不可修复
    for key in ("subtitles_zh", "subtitles_en", "voiceover_zh", "voiceover_en", "bgm"):
        assert key not in dispatch


def test_repair_retries_only_repairable(monkeypatch) -> None:
    called: list[str] = []

    def _fake_action(state):
        called.append("ok")
        return {}

    monkeypatch.setattr(
        "nodes.node_delivery_qa_repair._build_dispatch",
        lambda: {"transitions": _fake_action, "stickers": _fake_action},
    )
    state = _state(
        gate_failed=True,
        categories={
            "transitions": {"status": "failed", "required": True},
            "stickers": {"status": "failed", "required": True},
            "bgm": {"status": "failed", "required": True},  # 不可修复
        },
    )
    state["join_qa_report"]["required_features"] = {
        "transitions": True, "stickers": True, "bgm": True,
    }
    out = delivery_qa_repair(state)
    assert called == ["ok", "ok"]
    assert out["delivery_qa_retry_count"] == DELIVERY_QA_MAX_REPAIRS
    tags = out["status_log"]
    assert "delivery_qa_repaired:transitions,stickers" in tags
    assert "delivery_qa_unrepairable:bgm" in tags


def test_repair_no_blocked_categories_still_sets_retry_count() -> None:
    """无阻断类别(防御路径,路由正常不会进)→ 不重跑,只写计数。"""
    out = delivery_qa_repair(_state(gate_failed=True, categories={}))
    assert out["delivery_qa_retry_count"] == DELIVERY_QA_MAX_REPAIRS
    assert out["status_log"] == ["delivery_qa_repair_done"]


def test_failed_node_raises_with_blocked_detail() -> None:
    state = _state(
        gate_failed=True,
        retry=1,
        categories={
            "transitions": {
                "status": "failed",
                "required": True,
                "issues": [{"check": "转场", "severity": "error", "detail": "转场缺失", "draft": "zh"}],
            },
        },
    )
    with pytest.raises(RuntimeError) as excinfo:
        delivery_qa_failed(state)
    assert "必需功能未通过" in str(excinfo.value)
    assert "transitions" in str(excinfo.value)


def test_repair_nodes_registered_in_graph() -> None:
    """图编译包含 repair/failed 节点与回边(阻断路由接线存在)。"""
    from config import make_checkpointer
    import graph as g

    compiled = g.build_graph(checkpointer=make_checkpointer("memory"))
    node_names = set(compiled.get_graph().nodes.keys())
    assert "delivery_qa_repair" in node_names
    assert "delivery_qa_failed" in node_names
    edges = {(e.source, e.target) for e in compiled.get_graph().edges}
    assert ("join_before_delivery", "delivery_qa_repair") in edges
    assert ("delivery_qa_repair", "join_before_delivery") in edges
    assert ("delivery_qa_failed", "__end__") in edges
