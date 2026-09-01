"""节点 12:human_add_bgm — 关卡②(对应原文档 4.7 节)。

结构与 ``node_06_human_reorder`` 同构,仅替换通知文案与操作范围。
用户被提示在剪映客户端手动添加 BGM,完成后通过 ``Command(resume=True)`` 唤醒。

测试策略:与 node_06 一致 — 纯函数单测覆盖 payload + post_resume,
真实 interrupt() 行为由集成测试覆盖。
"""

from __future__ import annotations

from langgraph.types import interrupt

from state import WorkflowState


def _send_notification(state: WorkflowState, checkpoint: str) -> None:
    """Week 3 占位通知 — 实际接入企业微信 webhook 留 Week 4。"""
    print(f"[notify] {checkpoint} thread={state.get('session_id')} draft={state.get('draft_path')}")


def _build_interrupt_payload(state: WorkflowState) -> dict:
    return {
        "checkpoint": "checkpoint2_add_bgm",
        "draft_path": state.get("draft_path"),
        "instructions": "请从剪映 VIP 音乐库选取 BGM 并拖入音轨,完成后确认继续",
    }


def _post_resume(state: WorkflowState, notifier=_send_notification) -> dict:
    notifier(state, "checkpoint2")
    log = list(state.get("status_log", []) or []) + ["checkpoint2_resumed"]
    return {**state, "bgm_notified": True, "status_log": log}


def human_add_bgm(state: WorkflowState) -> dict:
    """关卡②:挂起等待用户确认,resume 后发通知并通过。"""
    interrupt(_build_interrupt_payload(state))
    return _post_resume(state)