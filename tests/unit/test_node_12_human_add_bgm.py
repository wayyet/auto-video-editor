"""节点 12 关卡②单测 — 结构对齐节点 6。"""

from __future__ import annotations

from typing import Any

from nodes.node_12_human_add_bgm import _build_interrupt_payload, _post_resume


def _state(**overrides: Any) -> dict:
    base = {
        "session_id": "t1",
        "draft_path": "C:/tmp/draft.json",
        "status_log": [],
        "error_log": [],
    }
    base.update(overrides)
    return base


def test_interrupt_payload_structure() -> None:
    payload = _build_interrupt_payload(_state())
    # Week 5:统一为 "②",旧值保留在 legacy_id
    assert payload["checkpoint"] == "②"
    assert payload["legacy_id"] == "checkpoint2_add_bgm"
    assert payload["step"] == 12
    assert payload["draft_path"] == "C:/tmp/draft.json"
    assert "BGM" in payload["instructions"]


def test_post_resume_sends_notification_and_logs() -> None:
    """resume 后:发出通知 + 写 status_log + 设置 bgm_notified。"""
    calls: list[tuple] = []

    def fake_notifier(state: dict, checkpoint: str) -> None:
        calls.append((state.get("session_id"), checkpoint))

    state = _state()
    result = _post_resume(state, notifier=fake_notifier)

    assert len(calls) == 1
    assert calls[0] == ("t1", "checkpoint2")
    assert result["bgm_notified"] is True
    assert "checkpoint2_resumed" in result["status_log"]


def test_post_resume_preserves_existing_status_log() -> None:
    state = _state(status_log=["node_11_done"])
    result = _post_resume(state, notifier=lambda s, c: None)
    assert result["status_log"][0] == "node_11_done"
    assert "checkpoint2_resumed" in result["status_log"]