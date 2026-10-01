"""关卡⓪:等人工在 OpenStoryline 网页里完成分镜/文案/BGM/时间线规划。

设计(对照 plan §4.2):
- ``interrupt()`` 之前**不**做副作用(避免重放时重复)。
- resume 后第一个 task 才写 ``status_log``。
- 真正的产物读取放在 :func:`nodes.node_04b_get_storyboard_plan.get_storyboard_plan`,
  该函数验证过的 interrupt/resume 幂等模式(Week 3 起的 ``tests/integration/
  test_interrupt_resume.py`` 已覆盖)。
- resume 之后依次经过 :mod:`nodes.node_04a_import_video`(等网页【📥 导入视频】
  按钮)与 :mod:`nodes.node_04b_get_storyboard_plan`(读盘)。

payload 字段 ``checkpoint`` 固定为 ``"⓪"``,与其他关卡 ``①/②/③`` 对齐
(Week 5 计划 §1.1)。
"""
from __future__ import annotations

from langgraph.types import interrupt

from nodes._checkpoint import build_interrupt_payload, post_resume
from state import WorkflowState


def _build_interrupt_payload(state: WorkflowState) -> dict:
    """薄封装:关卡 ⓪ 的差异参数(收敛样板见 ``nodes/_checkpoint.py``)。"""
    return build_interrupt_payload(
        checkpoint="⓪",
        legacy_id="checkpoint0_storyline_plan",
        step=4,
        extra_payload={"openstoryline_web_url": state.get("openstoryline_web_url")},
        instructions=(
            "请在 OpenStoryline 网页里上传素材并与 Agent 对话完成"
            "分镜/文案/BGM/时间线规划,完成后回复继续"
        ),
    )


def _post_resume(state: WorkflowState) -> dict:
    """薄封装:关卡 ⓪ 不发通知(无 notifier / 无 notified_field)。"""
    return post_resume(state, log_tag="checkpoint0_resumed")


def checkpoint0_wait_storyline_plan(state: WorkflowState) -> dict:
    """LangGraph 节点:关卡⓪ — 仅做 interrupt + resume 后写 status_log。"""
    interrupt(_build_interrupt_payload(state))
    return _post_resume(state)


__all__ = ["checkpoint0_wait_storyline_plan"]