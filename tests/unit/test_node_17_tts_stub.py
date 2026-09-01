"""node_17_inject_english_tts_stub 单测 — Week 4 骨架(delta-only 返回)。"""

from __future__ import annotations

from nodes.node_17_inject_english_tts_stub import node_17_inject_english_tts_stub


def test_node_17_returns_none_and_log() -> None:
    """骨架节点:en_dub_audio_path=None,status_log 含 node_17_tts_stub_pass。"""
    state = {"status_log": ["prev"], "error_log": []}
    out = node_17_inject_english_tts_stub(state)
    assert out["en_dub_audio_path"] is None
    assert "node_17_tts_stub_pass" in out["status_log"]
    # Week 4:delta-only 返回;LangGraph 合并 reducer 会保留 "prev"
    # 单测层面只验证节点返回的 delta
    assert "prev" not in out["status_log"]  # 节点不返回完整列表


def test_node_17_preserves_state() -> None:
    """delta-only 返回 — 不应包含上游字段。"""
    state = {
        "status_log": ["a"],
        "error_log": ["err"],
        "draft_path": "/x/y.json",
    }
    out = node_17_inject_english_tts_stub(state)
    # LangGraph 在运行时合并 reducer,会保留上游字段;单测层只验证节点 delta
    assert "draft_path" not in out
    assert "error_log" not in out