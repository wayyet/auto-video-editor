"""``nodes/_checkpoint.py`` 收敛后的公共关卡样板单测(R4)。

关卡 ⓪/①/② 的 payload 与 resume 行为本身由各自的测试文件覆盖
(``test_node_checkpoint0_storyline_plan`` / ``test_node_06_human_reorder`` /
``test_node_12_human_add_bgm``),本文件只锁**共享 helper 自身**的契约 ——
即以后有人再动 ``_checkpoint.py`` 时,这些行为不会在三个节点之间悄悄漂移。
"""

from __future__ import annotations

from nodes._checkpoint import build_interrupt_payload, post_resume


# ---------------------------------------------------------------------------
# build_interrupt_payload
# ---------------------------------------------------------------------------
def test_payload_contains_contract_fields() -> None:
    payload = build_interrupt_payload(
        checkpoint="①",
        legacy_id="checkpoint1_reorder",
        step=6,
        instructions="做点什么",
    )
    # checkpoint / legacy_id / step 三件套是 resume_all_pending 与人工脚本的契约
    assert payload["checkpoint"] == "①"
    assert payload["legacy_id"] == "checkpoint1_reorder"
    assert payload["step"] == 6
    assert payload["instructions"] == "做点什么"


def test_extra_payload_is_merged_not_dropped() -> None:
    """关卡特有字段(⓪ 的 openstoryline_web_url / ①② 的 draft_path)不能被吞掉。"""
    payload = build_interrupt_payload(
        checkpoint="⓪",
        legacy_id="checkpoint0_storyline_plan",
        step=4,
        extra_payload={"openstoryline_web_url": "http://127.0.0.1:7860"},
        instructions="x",
    )
    assert payload["openstoryline_web_url"] == "http://127.0.0.1:7860"
    # extra 不得覆盖三个契约键
    payload2 = build_interrupt_payload(
        checkpoint="①",
        legacy_id="l",
        step=6,
        extra_payload={"checkpoint": "被覆盖", "draft_path": "d"},
        instructions="x",
    )
    assert payload2["checkpoint"] == "①", "extra_payload 不允许覆盖契约键 checkpoint"
    assert payload2["draft_path"] == "d"


def test_extra_payload_optional() -> None:
    """不传 extra_payload 时不应凭空多出键(``None`` 与 ``{}`` 等价)。"""
    a = build_interrupt_payload(checkpoint="②", legacy_id="l", step=12, instructions="x")
    b = build_interrupt_payload(
        checkpoint="②", legacy_id="l", step=12, extra_payload={}, instructions="x"
    )
    assert a == b
    assert set(a) == {"checkpoint", "legacy_id", "step", "instructions"}


# ---------------------------------------------------------------------------
# post_resume
# ---------------------------------------------------------------------------
def test_post_resume_appends_without_clobbering() -> None:
    state = {"status_log": ["before"], "other": "keep"}
    out = post_resume(state, log_tag="checkpoint0_resumed")
    assert out["status_log"] == ["before", "checkpoint0_resumed"]
    assert out["other"] == "keep"
    # 原 state 不被就地改写(纯函数)
    assert state["status_log"] == ["before"]


def test_post_resume_handles_missing_status_log() -> None:
    """state 没有 status_log 键(None / 缺失)都要能跑。"""
    assert post_resume({}, log_tag="c_resumed")["status_log"] == ["c_resumed"]
    assert post_resume({"status_log": None}, log_tag="c_resumed")["status_log"] == [
        "c_resumed"
    ]


def test_post_resume_notifier_tag_derived_from_log_tag() -> None:
    """通知函数收到的第二参是 log_tag 去掉 _resumed("checkpoint1_resumed"→"checkpoint1")。"""
    calls: list[tuple[str, str]] = []
    out = post_resume(
        {"status_log": []},
        log_tag="checkpoint2_resumed",
        notifier=lambda s, tag: calls.append((s.get("session_id", "?"), tag)),
        notified_field="bgm_notified",
    )
    assert calls == [("?", "checkpoint2")]
    assert out["bgm_notified"] is True


def test_post_resume_without_notifier_writes_no_flag() -> None:
    """关卡 ⓪ 形态:不传 notifier / notified_field 时不得凭空加键。"""
    out = post_resume({"status_log": []}, log_tag="checkpoint0_resumed")
    assert "reorder_notified" not in out
    assert "bgm_notified" not in out
