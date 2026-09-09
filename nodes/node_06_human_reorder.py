"""节点 6:human_reorder — 关卡①(对应原文档 4.1 节)。

设计:副作用挪后方案。
- ``interrupt()`` 之前**不**调用 ``send_notification``(避免重放时重复发送)
- resume 后第一个 task 才发通知
- 状态字段 ``reorder_notified`` 仍保留(Week 2 测试兼容),但本节点不依赖它做幂等

调用栈(Week 3 集成图):
    节点 5 (generate_draft) → 节点 6 (interrupt) ⏸
    → 用户在剪映客户端手动调整 → Command(resume=True)
    → 节点 6 继续执行(发通知 + 写 status_log) → 节点 7

Week 5 改动(对齐第 5 周计划 §1.1):
- interrupt payload ``checkpoint`` 字段统一为 ``"①"``,便于
  ``resume_all_pending`` 自动匹配多个 interrupt。旧值
  ``"checkpoint1_reorder"`` 保留在 ``legacy_id`` 字段,保证向后兼容。
- 通知文案带 ``"checkpoint1"`` 标记,与 status_log ``checkpoint1_resumed`` 对齐。

测试策略:
- ``_build_interrupt_payload`` / ``_post_resume`` 抽成纯函数,单测直接覆盖
- ``human_reorder`` 仅做编排,集成测试通过完整 graph + SqliteSaver 覆盖
"""

from __future__ import annotations

from langgraph.types import interrupt

from state import WorkflowState


def _send_notification(state: WorkflowState, checkpoint: str) -> None:
    """Week 3 占位通知:实际接入企业微信 webhook 留 Week 4。

    本函数被刻意放在 ``interrupt()`` **之后**,避免重放时重复发送。
    单测可通过 monkeypatch 此函数计数。
    """
    # 当前仅写入 state.log;Week 4 替换为真实通知渠道。
    print(f"[notify] {checkpoint} thread={state.get('session_id')} draft={state.get('draft_path')}")


def _build_interrupt_payload(state: WorkflowState) -> dict:
    """构造 interrupt payload — 纯函数,便于单测。

    Week 5:``checkpoint`` 字段统一为 ``"①"``;保留 ``legacy_id`` 兼容旧版。
    """
    return {
        "checkpoint": "①",
        "legacy_id": "checkpoint1_reorder",
        "step": 6,
        "draft_path": state.get("draft_path"),
        "instructions": "请在剪映客户端手动调整分镜顺序,完成后确认继续",
    }


def _post_resume(state: WorkflowState, notifier=_send_notification) -> dict:
    """resume 后逻辑 — 纯函数,便于单测。"""
    notifier(state, "checkpoint1")
    log = list(state.get("status_log", []) or []) + ["checkpoint1_resumed"]
    return {**state, "reorder_notified": True, "status_log": log}


def human_reorder(state: WorkflowState) -> dict:
    """关卡①:挂起等待用户确认,resume 后发通知并通过。"""
    interrupt(_build_interrupt_payload(state))
    return _post_resume(state)