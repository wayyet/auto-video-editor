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

    副作用挪后方案的核心:**重放发生在 resume 之前**(``interrupt()`` 之前不会调 notify),
    resume 后的代码每次都跑,但 LangGraph 不会"二次 resume"同一节点,所以通知只发一次。
    """
    state = _state(reorder_notified=True)
    result = _post_resume(state, notifier=lambda s, c: None)
    assert "checkpoint1_resumed" in result["status_log"]


# ---------------------------------------------------------------------------
# P1-7:挂起前 payload 带 assembly 报告/预览/QC;reject 显式拒绝
# ---------------------------------------------------------------------------
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
    """旧任务/QC 关:assembly 字段缺省 → payload 带空值,不崩。"""
    payload = _build_interrupt_payload(_state())
    assert payload["assembly_report_path"] is None
    assert payload["assembly_preview_path"] is None
    assert payload["assembly_qc_status"] is None
    assert payload["assembly_qc_warnings"] == []


def test_post_resume_reject_terminates_when_escalated() -> None:
    """QC 降级(escalated)时恢复值 reject → fail-closed 终止。"""
    import pytest

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
    import pytest

    with pytest.raises(RuntimeError, match="reject"):
        _post_resume(state, resume_value="Reject ", notifier=lambda s, c: None)


# ---------------------------------------------------------------------------
# 端到端 interrupt() 行为 — 由集成测试覆盖(test_interrupt_resume.py)
# ---------------------------------------------------------------------------
# 单元测试只覆盖纯函数部分;LangGraph 真实 interrupt() 触发场景需要
# 完整 graph + checkpointer,在 integration test 验证。