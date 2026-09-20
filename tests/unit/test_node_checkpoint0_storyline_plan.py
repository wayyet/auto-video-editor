"""``node_checkpoint0_storyline_plan`` 单测(plan §4.2)。

覆盖:
1. interrupt payload 字段对齐关卡``⓪/①/②/③`` 约定;
2. resume 后只追加 status_log;
3. payload 含 ``openstoryline_web_url`` 与 ``instructions``。
"""
from __future__ import annotations

from unittest.mock import patch

from nodes.node_checkpoint0_storyline_plan import (
    _build_interrupt_payload,
    _post_resume,
    checkpoint0_wait_storyline_plan,
)


def test_interrupt_payload_has_checkpoint_zero() -> None:
    state = {
        "openstoryline_web_url": "http://127.0.0.1:7860",
        "status_log": [],
        "error_log": [],
    }
    payload = _build_interrupt_payload(state)
    assert payload["checkpoint"] == "⓪"
    assert payload["legacy_id"] == "checkpoint0_storyline_plan"
    assert payload["step"] == 4
    assert payload["openstoryline_web_url"] == "http://127.0.0.1:7860"
    assert "请在 OpenStoryline 网页里" in payload["instructions"]


def test_post_resume_appends_status_log() -> None:
    state = {"status_log": ["prev"]}
    out = _post_resume(state)
    assert out["status_log"] == ["prev", "checkpoint0_resumed"]


def test_checkpoint0_calls_interrupt_and_resume() -> None:
    """用 patch 替换 langgraph.types.interrupt,验证调用与 post_resume。"""
    state = {
        "openstoryline_web_url": "http://127.0.0.1:7860",
        "status_log": [],
        "error_log": [],
    }

    with patch(
        "nodes.node_checkpoint0_storyline_plan.interrupt"
    ) as mock_interrupt:
        out = checkpoint0_wait_storyline_plan(state)

    # interrupt 被调用一次,payload 含 checkpoint="⓪"
    mock_interrupt.assert_called_once()
    (payload,), _ = mock_interrupt.call_args
    assert payload["checkpoint"] == "⓪"

    # resume 后 status_log 追加
    assert out["status_log"] == ["checkpoint0_resumed"]


def test_checkpoint0_idempotent_under_replay() -> None:
    """重放:state 已有 status_log 时,_post_resume 只追加、不重写其他字段。"""
    state = {
        "openstoryline_web_url": "http://127.0.0.1:7860",
        "status_log": ["before"],
        "some_other_field": "untouched",
    }
    with patch("nodes.node_checkpoint0_storyline_plan.interrupt"):
        out = checkpoint0_wait_storyline_plan(state)
    assert out["status_log"] == ["before", "checkpoint0_resumed"]
    assert out["some_other_field"] == "untouched"
    assert out["openstoryline_web_url"] == "http://127.0.0.1:7860"