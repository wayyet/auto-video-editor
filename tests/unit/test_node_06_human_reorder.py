"""节点 6 关卡① 单测 — interrupt payload + 副作用挪后。

注:直接调用 ``human_reorder()`` 会触发 ``get_config()`` 异常(无 RunnableConfig 上下文),
故测试拆分为两部分:
- 单元测试覆盖 ``_build_interrupt_payload`` 与 ``_post_resume``(纯函数)
- ``interrupt()`` 行为通过集成测试 ``test_interrupt_resume.py`` 验证
"""

from __future__ import annotations

from typing import Any

from nodes.node_06_human_reorder import _build_interrupt_payload, _post_resume


def _state(**overrides: Any) -> dict:
    base = {
        "session_id": "t1",
        "draft_path": "C:/tmp/draft.json",
        "status_log": [],
        "error_log": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# interrupt payload 构造
# ---------------------------------------------------------------------------
def test_interrupt_payload_structure() -> None:
    payload = _build_interrupt_payload(_state())
    # Week 5:统一为 "①",旧值保留在 legacy_id
    assert payload["checkpoint"] == "①"
    assert payload["legacy_id"] == "checkpoint1_reorder"
    assert payload["step"] == 6
    assert payload["draft_path"] == "C:/tmp/draft.json"
    assert "instructions" in payload
    assert "手动调整" in payload["instructions"]


def test_interrupt_payload_uses_state_draft_path() -> None:
    state = _state(draft_path="/another/draft.json")
    payload = _build_interrupt_payload(state)
    assert payload["draft_path"] == "/another/draft.json"


# ---------------------------------------------------------------------------
# resume 后副作用挪后验证
# ---------------------------------------------------------------------------
def test_post_resume_sends_notification_and_appends_log() -> None:
    """resume 后:发出通知 + 追加 status_log + 设置 reorder_notified。"""
    calls: list[tuple] = []

    def fake_notifier(state: dict, checkpoint: str) -> None:
        calls.append((state.get("session_id"), checkpoint))

    state = _state()
    result = _post_resume(state, notifier=fake_notifier)

    assert len(calls) == 1
    assert calls[0] == ("t1", "checkpoint1")
    assert result["reorder_notified"] is True
    assert "checkpoint1_resumed" in result["status_log"]


def test_post_resume_preserves_existing_status_log() -> None:
    """resume 应保留原有 status_log(只追加,不覆盖)。"""
    state = _state(status_log=["node_05_done"])
    result = _post_resume(state, notifier=lambda s, c: None)
    assert result["status_log"][0] == "node_05_done"
    assert "checkpoint1_resumed" in result["status_log"]


def test_post_resume_idempotent_via_field() -> None:
    """若已 reorder_notified=True,通知仍会发(本节点不依赖此字段做幂等 — 由 Command(resume) 控制)。

    副作用挪后方案的核心:**重放发生在 resume 之前**(`interrupt()` 之前不会调 notify),
    resume 后的代码每次都跑,但 LangGraph 不会"二次 resume"同一节点,所以通知只发一次。
    """
    state = _state(reorder_notified=True)
    result = _post_resume(state, notifier=lambda s, c: None)
    assert result["reorder_notified"] is True


# ---------------------------------------------------------------------------
# 端到端 interrupt() 行为 — 由集成测试覆盖(test_interrupt_resume.py)
# ---------------------------------------------------------------------------
# 单元测试只覆盖纯函数部分;LangGraph 真实 interrupt() 触发场景需要
# 完整 graph + checkpointer,在 integration test 验证。