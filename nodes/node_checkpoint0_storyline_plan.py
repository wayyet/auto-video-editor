"""关卡⓪:等人工在 OpenStoryline 网页里完成分镜/文案/BGM/时间线规划。

设计(对照 plan §4.2):
- ``interrupt()`` 之前**不**做副作用(避免重放时重复)。
- resume 后第一个 task 才写 ``status_log``。
- 真正的产物读取放在 :func:`nodes.node_04_import_and_plan.import_video_and_plan_shots`,
  该函数验证过的 interrupt/resume 幂等模式(Week 3 起的 ``tests/integration/
  test_interrupt_resume.py`` 已覆盖)。

payload 字段 ``checkpoint`` 固定为 ``"⓪"``,与其他关卡 ``①/②/③`` 对齐
(Week 5 计划 §1.1)。
"""
from __future__ import annotations

from langgraph.types import interrupt

from state import WorkflowState


def _build_interrupt_payload(state: WorkflowState) -> dict:
    return {
        "checkpoint": "⓪",
        "legacy_id": "checkpoint0_storyline_plan",
        "step": 4,
        "openstoryline_web_url": state.get("openstoryline_web_url"),
        "instructions": (
            "请在 OpenStoryline 网页里上传素材并与 Agent 对话完成"
            "分镜/文案/BGM/时间线规划,完成后回复继续"
        ),
    }


def _post_resume(state: WorkflowState) -> dict:
    log = list(state.get("status_log") or []) + ["checkpoint0_resumed"]
    return {**state, "status_log": log}


def checkpoint0_wait_storyline_plan(state: WorkflowState) -> dict:
    """LangGraph 节点:关卡⓪ — 仅做 interrupt + resume 后写 status_log。"""
    interrupt(_build_interrupt_payload(state))
    return _post_resume(state)


__all__ = ["checkpoint0_wait_storyline_plan"]