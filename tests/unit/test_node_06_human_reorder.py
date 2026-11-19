"""节点 6 关卡① 单测 — 条件挂起控制分支 + interrupt payload + 副作用挪后。

2026-11-20 条件挂起改造:``human_reorder()`` 按 QC 状态分支(仅 escalated
挂起人工复核;pass / pass_with_warnings / QC 关闭自动通过;旧 checkpoint
保留旧的无条件挂起行为)。控制分支测试通过 **monkeypatch
``nodes.node_06_human_reorder.interrupt``** 直接驱动 ``human_reorder()``
(桩替代后无需 RunnableConfig 上下文);真实 LangGraph interrupt 语义仍由
集成测试 ``test_interrupt_resume.py`` / ``test_assembly_qc_graph.py`` 覆盖。

用例编号对应审查修订版执行计划 §4 阶段3 的 U1-U12 + 旧任务兼容 L1-L4。
"""

from __future__ import annotations

from typing import Any

import pytest

import nodes.node_06_human_reorder as n06
from nodes.node_06_human_reorder import (
    _build_interrupt_payload,
    _mark_auto_pass,
    _post_resume,
    human_reorder,
)


def _state(**overrides: Any) -> dict:
    base = {
        "session_id": "t1",
        "draft_path": "C:/tmp/draft.json",
        "status_log": [],
        "error_log": [],
    }
    base.update(overrides)
    return base


def _forbid_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """自动通过用例:任何 interrupt 调用都视为失败。"""
    def _boom(_payload: dict) -> None:
        raise AssertionError("非降级状态不应调用 interrupt()")
    monkeypatch.setattr(n06, "interrupt", _boom)


# ---------------------------------------------------------------------------
# interrupt payload 构造(纯函数)
# ---------------------------------------------------------------------------
def test_interrupt_payload_structure() -> None:
    payload = _build_interrupt_payload(_state())
    # Week 5:统一为 "①",旧值保留在 legacy_id
    assert payload["checkpoint"] == "①"
    assert payload["legacy_id"] == "checkpoint1_reorder"
    assert payload["step"] == 6
    assert payload["draft_path"] == "C:/tmp/draft.json"
    assert "instructions" in payload
    # 2026-11-20 条件挂起:指引改为 QC 降级复核语义(reject 选项保留)
    assert "QC" in payload["instructions"]
    assert "reject" in payload["instructions"]
    assert "手动调整" not in payload["instructions"]


def test_interrupt_payload_uses_state_draft_path() -> None:
    state = _state(draft_path="/another/draft.json")
    payload = _build_interrupt_payload(state)
    assert payload["draft_path"] == "/another/draft.json"


def test_interrupt_payload_carries_assembly_artifacts(tmp_path) -> None:
    """P1-7:挂起前即可取得报告/预览路径与 QC 状态(不再只在 resume 后出现)。"""
    import json

    qc_path = tmp_path / "candidate_qc_report.json"
    qc_path.write_text(
        json.dumps(
            {
                "status": "pass_with_warnings",
                "issues": [
                    {"severity": "warning", "code": "preview_issue", "message": "黑帧警告 A"},
                    {"severity": "error", "code": "track_desync", "message": "轨道错位 B"},
                    {"severity": "warning", "message": "静音 C"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    state = _state(
        assembly_report_path=str(tmp_path / "report.md"),
        assembly_preview_path=str(tmp_path / "preview.mp4"),
        assembly_qc_status="pass_with_warnings",
        assembly_candidate_qc_report_path=str(qc_path),
    )
    payload = _build_interrupt_payload(state)
    # 契约三键不动
    assert payload["checkpoint"] == "①"
    assert payload["legacy_id"] == "checkpoint1_reorder"
    assert payload["step"] == 6
    # P1-7 新增字段
    assert payload["assembly_report_path"] == str(tmp_path / "report.md")
    assert payload["assembly_preview_path"] == str(tmp_path / "preview.mp4")
    assert payload["assembly_qc_status"] == "pass_with_warnings"
    assert payload["assembly_qc_warnings"] == ["黑帧警告 A", "轨道错位 B", "静音 C"]
    # instructions 提及 reject 选项
    assert "reject" in payload["instructions"]


def test_interrupt_payload_assembly_fields_optional() -> None:
    """旧任务/QC 关:assembly 字段缺省 → payload 带空值,不崩(U11)。"""
    payload = _build_interrupt_payload(_state())
    assert payload["assembly_report_path"] is None
    assert payload["assembly_preview_path"] is None
    assert payload["assembly_qc_status"] is None
    assert payload["assembly_qc_warnings"] == []


# ---------------------------------------------------------------------------
# 自动通过审计收尾(纯函数)
# ---------------------------------------------------------------------------
def test_mark_auto_pass_tag_normal() -> None:
    """QC 正常/警告自动通过 → checkpoint1_auto_passed。"""
    out = _mark_auto_pass(_state(), qc_status="pass")
    assert out == {"status_log": ["checkpoint1_auto_passed"]}
    out = _mark_auto_pass(_state(), qc_status="pass_with_warnings")
    assert out["status_log"] == ["checkpoint1_auto_passed"]


def test_mark_auto_pass_tag_qc_disabled() -> None:
    """QC 关闭自动通过 → 独立 qc_disabled 标签(原因可区分,U8)。"""
    out = _mark_auto_pass(_state(), qc_status=None)
    assert out == {"status_log": ["checkpoint1_auto_passed_qc_disabled"]}


# ---------------------------------------------------------------------------
# resume 后副作用挪后验证(纯函数)
# ---------------------------------------------------------------------------
def test_post_resume_sends_notification_and_appends_log() -> None:
    """resume 后:发出通知 + 追加 status_log(reorder_notified 审计位已移除)。"""
    calls: list[tuple] = []

    def fake_notifier(state: dict, checkpoint: str) -> None:
        calls.append((state.get("session_id"), checkpoint))

    state = _state()
    result = _post_resume(state, notifier=fake_notifier)

    assert len(calls) == 1
    assert calls[0] == ("t1", "checkpoint1")
    assert "checkpoint1_resumed" in result["status_log"]


def test_post_resume_preserves_existing_status_log() -> None:
    """resume 应保留原有 status_log(只追加,不覆盖)。"""
    state = _state(status_log=["node_05_done"])
    result = _post_resume(state, notifier=lambda s, c: None)
    assert result["status_log"][0] == "node_05_done"
    assert "checkpoint1_resumed" in result["status_log"]


def test_post_resume_idempotent_via_field() -> None:
    """旧 checkpoint 兼容:state 里残留已移除的 reorder_notified 键时,resume 收尾
    照常执行且不崩(幂等由 Command(resume) 控制,与该键无关;键随 ``{**state}``
    透传无害保留)。
    """
    state = _state(reorder_notified=True)
    result = _post_resume(state, notifier=lambda s, c: None)
    assert "checkpoint1_resumed" in result["status_log"]


def test_post_resume_reject_terminates_when_escalated() -> None:
    """QC 降级(escalated)时恢复值 reject → fail-closed 终止。"""
    state = _state(assembly_qc_status="escalated")
    with pytest.raises(RuntimeError, match="reject"):
        _post_resume(state, resume_value="reject", notifier=lambda s, c: None)


def test_post_resume_reject_ignored_when_not_escalated() -> None:
    """非降级状态 reject 无效(照常继续),避免误伤正常任务。"""
    state = _state(assembly_qc_status="pass")
    result = _post_resume(state, resume_value="reject", notifier=lambda s, c: None)
    assert "checkpoint1_resumed" in result["status_log"]


def test_post_resume_reject_none_and_other_values_safe() -> None:
    """None / 空串 / 其他恢复值(ok)不触发误终止;reject 匹配大小写不敏感。"""
    state = _state(assembly_qc_status="escalated")
    ok = _post_resume(state, resume_value=None, notifier=lambda s, c: None)
    assert "checkpoint1_resumed" in ok["status_log"]
    ok2 = _post_resume(state, resume_value="ok", notifier=lambda s, c: None)
    assert "checkpoint1_resumed" in ok2["status_log"]
    # 大小写/空白变体视为显式 reject(escalated 下终止)
    with pytest.raises(RuntimeError, match="reject"):
        _post_resume(state, resume_value="Reject ", notifier=lambda s, c: None)


# ---------------------------------------------------------------------------
# human_reorder 控制分支(monkeypatch interrupt,真实控制流)
# ---------------------------------------------------------------------------
def test_u1_pass_auto_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """U1:QC pass → 不调用 interrupt,自动通过。"""
    _forbid_interrupt(monkeypatch)
    state = _state(assembly_qc_gate_enabled=True, assembly_qc_status="pass")
    result = human_reorder(state)
    assert result["status_log"] == ["checkpoint1_auto_passed"]
    assert "checkpoint1_resumed" not in result["status_log"]


def test_u2_pass_with_warnings_auto_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """U2:QC pass_with_warnings → 不调用 interrupt,自动通过。"""
    _forbid_interrupt(monkeypatch)
    state = _state(
        assembly_qc_gate_enabled=True,
        assembly_qc_status="pass_with_warnings",
        assembly_report_path="C:/tmp/report.md",
    )
    result = human_reorder(state)
    assert result["status_log"] == ["checkpoint1_auto_passed"]
    # 警告不丢失:自动通过不改写任何 QC 字段(报告仍在 state/磁盘)
    assert "assembly_report_path" not in result


def test_u3_escalated_interrupts_with_full_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U3:非结构性 escalated → 调用 interrupt,payload 三键与 QC 产物字段完整。"""
    captured: list[dict] = []
    monkeypatch.setattr(n06, "interrupt", lambda payload: captured.append(payload) or True)
    state = _state(
        assembly_qc_gate_enabled=True,
        assembly_qc_status="escalated",
        assembly_report_path="C:/tmp/report.md",
        assembly_preview_path="C:/tmp/preview.mp4",
    )
    result = human_reorder(state)
    assert len(captured) == 1
    payload = captured[0]
    assert payload["checkpoint"] == "①"
    assert payload["legacy_id"] == "checkpoint1_reorder"
    assert payload["step"] == 6
    assert payload["assembly_qc_status"] == "escalated"
    assert payload["assembly_report_path"] == "C:/tmp/report.md"
    # resume=True → 人工确认收尾
    assert "checkpoint1_resumed" in result["status_log"]


def test_u4_escalated_resume_true_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """U4:escalated + 恢复值 true → 通过并写入 checkpoint1_resumed(通知发出)。"""
    notified: list[str] = []
    monkeypatch.setattr(n06, "_send_notification", lambda s, c: notified.append(c))
    monkeypatch.setattr(n06, "interrupt", lambda payload: True)
    state = _state(assembly_qc_gate_enabled=True, assembly_qc_status="escalated")
    result = human_reorder(state)
    assert "checkpoint1_resumed" in result["status_log"]
    assert notified == ["checkpoint1"]


def test_u5_escalated_resume_reject_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U5:escalated + reject → RuntimeError,不进入 node_07。"""
    monkeypatch.setattr(n06, "interrupt", lambda payload: "reject")
    state = _state(assembly_qc_gate_enabled=True, assembly_qc_status="escalated")
    with pytest.raises(RuntimeError, match="reject"):
        human_reorder(state)


def test_u6_reject_irrelevant_when_auto_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """U6:非降级状态 reject 无从生效 —— pass 状态根本不挂起,按自动通过处理。"""
    _forbid_interrupt(monkeypatch)
    state = _state(assembly_qc_gate_enabled=True, assembly_qc_status="pass")
    result = human_reorder(state)
    assert result["status_log"] == ["checkpoint1_auto_passed"]


def test_u7_structural_error_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """U7:结构性错误 → fail-closed,不允许人工 true 放行(保护旧 checkpoint)。"""
    _forbid_interrupt(monkeypatch)
    for extra in (
        {"assembly_qc_gate_enabled": True, "assembly_qc_status": "escalated"},
        {"assembly_qc_gate_enabled": None, "assembly_qc_status": "escalated"},
        {"assembly_qc_gate_enabled": True, "assembly_qc_status": "pass"},
    ):
        state = _state(assembly_structural_error=True, **extra)
        with pytest.raises(RuntimeError, match="结构性"):
            human_reorder(state)


def test_u8_qc_disabled_missing_status_auto_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U8:QC 关闭(标记 False)且状态缺失 → 自动通过,记录关闭原因标签。"""
    _forbid_interrupt(monkeypatch)
    state = _state(assembly_qc_gate_enabled=False, assembly_qc_status=None)
    result = human_reorder(state)
    assert result["status_log"] == ["checkpoint1_auto_passed_qc_disabled"]


def test_u9_qc_enabled_missing_status_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U9:QC 启用但状态缺失 → fail-closed,不得隐式通过。"""
    _forbid_interrupt(monkeypatch)
    state = _state(assembly_qc_gate_enabled=True, assembly_qc_status=None)
    with pytest.raises(RuntimeError, match="assembly_qc_status"):
        human_reorder(state)


def test_u10_unknown_status_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """U10:未知 QC 状态字符串 → fail-closed(QC 开/关两态均不静默通过)。"""
    _forbid_interrupt(monkeypatch)
    for marker in (True, False):
        state = _state(assembly_qc_gate_enabled=marker, assembly_qc_status="weird")
        with pytest.raises(RuntimeError, match="未知 assembly_qc_status"):
            human_reorder(state)


def test_u12_auto_pass_no_notification_no_resumed_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U12:自动通过不发人工通知、不写 checkpoint1_resumed,写独立自动通过标记。"""
    _forbid_interrupt(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(n06, "_send_notification", lambda s, c: notified.append(c))
    state = _state(assembly_qc_gate_enabled=True, assembly_qc_status="pass")
    result = human_reorder(state)
    assert notified == []
    assert result["status_log"] == ["checkpoint1_auto_passed"]
    assert "checkpoint1_resumed" not in result["status_log"]


# ---------------------------------------------------------------------------
# 旧 checkpoint 兼容(assembly_qc_gate_enabled 缺失 = None → 旧版无条件挂起)
# ---------------------------------------------------------------------------
def test_l1_legacy_always_interrupts(monkeypatch: pytest.MonkeyPatch) -> None:
    """L1:旧 checkpoint(无标记)任何 QC 状态都无条件挂起 = 旧版行为。"""
    captured: list[dict] = []
    monkeypatch.setattr(n06, "interrupt", lambda payload: captured.append(payload) or True)
    for status in ("pass", "pass_with_warnings", "escalated", None):
        state = _state(assembly_qc_status=status)
        result = human_reorder(state)
        assert "checkpoint1_resumed" in result["status_log"]
    assert len(captured) == 4


def test_l2_legacy_escalated_reject_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2:旧 checkpoint + escalated + reject → 终止(reject 语义不变)。"""
    monkeypatch.setattr(n06, "interrupt", lambda payload: "reject")
    state = _state(assembly_qc_status="escalated")
    with pytest.raises(RuntimeError, match="reject"):
        human_reorder(state)


def test_l3_legacy_pass_consumes_pending_resume_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L3:旧 checkpoint(QC 正常)恢复 → interrupt() 返回已存恢复值 → 正常放行。

    存量任务(console-011419d9ceec 等)在新代码下恢复的兼容路径:挂起期间
    用户点确认的 resume 值经 interrupt() 原样消费,不依赖"跳过 interrupt"。
    """
    monkeypatch.setattr(n06, "interrupt", lambda payload: True)
    state = _state(assembly_qc_status="pass_with_warnings")
    result = human_reorder(state)
    assert "checkpoint1_resumed" in result["status_log"]


def test_l4_structural_check_precedes_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """L4:结构性错误检查在旧任务分支之前(旧 checkpoint 也不允许放行)。"""
    _forbid_interrupt(monkeypatch)
    state = _state(assembly_structural_error=True, assembly_qc_status="pass")
    with pytest.raises(RuntimeError, match="结构性"):
        human_reorder(state)
