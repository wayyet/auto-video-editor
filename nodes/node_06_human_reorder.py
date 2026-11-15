"""节点 6:human_reorder — 关卡①(对应原文档 4.1 节)。

设计:副作用挪后方案。
- ``interrupt()`` 之前**不**调用 ``send_notification``(避免重放时重复发送)
- resume 后第一个 task 才发通知
- 幂等不依赖任何"已通知"状态位(由 LangGraph resume 语义保证;
  2026-11-12 阶段5批1 移除无人消费的 ``reorder_notified`` 审计位)

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
- ``human_reorder`` 仅做编排,集成测试通过完整 graph + ``InMemorySaver`` 覆盖
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.types import interrupt

from nodes._checkpoint import build_interrupt_payload, post_resume
from state import WorkflowState


def _send_notification(state: WorkflowState, checkpoint: str) -> None:
    """关卡① 人工通知(Week 3 占位 + 阶段五扩展)。

    阶段五(plan §7.6 / §11 验收项 "关卡① 通知文案正确带上报告路径"):
    ``assembly_report_path`` 是 assembly 6 节点的最终产物路径;关卡① 通知里
    必须带上,便于人工在剪映里调分镜时同步看 assembly QC 报告与 preview.mp4。

    本函数被刻意放在 ``interrupt()`` **之后**,避免重放时重复发送。
    单测可通过 monkeypatch 此函数计数。
    """
    assembly_report = state.get("assembly_report_path") or "(无报告)"
    # 当前仅写入 state.log;Week 4 替换为真实通知渠道。
    print(
        f"[notify] {checkpoint} thread={state.get('session_id')} "
        f"draft={state.get('draft_path')} assembly_report={assembly_report}"
    )


def _qc_issue_messages(state: WorkflowState, limit: int = 5) -> list[str]:
    """读取候选/预览 QC 报告的 error+warning 摘要(P1-7 关卡① 挂起即可见)。

    优先 candidate_qc_report(候选计划级,typed),回落 preview_qc_report;
    读不到/损坏一律返回 [](payload 展示字段,绝不抛异常)。
    """
    for path in (
        state.get("assembly_candidate_qc_report_path"),
        state.get("assembly_qc_report_path"),
    ):
        if not path:
            continue
        try:
            p = Path(str(path))
            if not p.is_file():
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 展示字段,损坏静默跳过
            continue
        if not isinstance(data, dict):
            continue
        msgs = [
            str(i.get("message") or "")
            for i in (data.get("issues") or [])
            if isinstance(i, dict) and i.get("severity") in ("error", "warning")
        ]
        msgs = [m for m in msgs if m]
        if msgs:
            return msgs[:limit]
    return []


def _build_interrupt_payload(state: WorkflowState) -> dict:
    """构造 interrupt payload — 纯函数,便于单测。

    Week 5:``checkpoint`` 字段统一为 ``"①"``;保留 ``legacy_id`` 兼容旧版。
    样板部分见 ``nodes/_checkpoint.build_interrupt_payload``;本函数只声明
    关卡① 特有的参数。

    P1-7:挂起**前**即可取得 assembly 报告/预览路径与 QC 状态、关键警告
    (此前只有 draft_path,报告路径只在 resume 后的通知文案里出现)。
    契约三键(checkpoint/legacy_id/step)由 build_interrupt_payload 回写保护,
    extra_payload 不允许覆盖。
    """
    return build_interrupt_payload(
        checkpoint="①",
        legacy_id="checkpoint1_reorder",
        step=6,
        extra_payload={
            "draft_path": state.get("draft_path"),
            "assembly_report_path": state.get("assembly_report_path"),
            "assembly_preview_path": state.get("assembly_preview_path"),
            "assembly_qc_status": state.get("assembly_qc_status"),
            "assembly_qc_warnings": _qc_issue_messages(state),
        },
        instructions=(
            "请在剪映客户端手动调整分镜顺序,完成后确认继续"
            "(assembly QC 降级时如需拒绝继续,可输入恢复值 reject 终止流程)"
        ),
    )


def _post_resume(
    state: WorkflowState,
    resume_value: Any = None,
    notifier=_send_notification,
) -> dict:
    """resume 后逻辑 — 纯函数,便于单测(notifier 保持可注入,单测直接传假函数)。

    P1-7:assembly QC 降级(escalated)时,人工可用恢复值 ``reject`` 显式
    拒绝降级结果 → fail-closed 终止运行(而不是把有质量问题的草稿继续剪下去)。
    非 escalated 状态下 reject 无效(照常继续),避免误伤正常任务。
    """
    if (
        state.get("assembly_qc_status") == "escalated"
        and str(resume_value or "").strip().lower() == "reject"
    ):
        raise RuntimeError(
            "[node_06] 关卡① 人工拒绝 assembly 降级结果(assembly_qc_status="
            "escalated,恢复值 reject),工作流终止;请修复素材或检查 QC 报告后重试,"
            "应急可关闭 ASSEMBLY_QC_GATE_ENABLED"
        )
    return post_resume(
        state,
        log_tag="checkpoint1_resumed",
        notifier=notifier,
    )


def human_reorder(state: WorkflowState) -> dict:
    """关卡①:挂起等待用户确认,resume 后发通知并通过(reject 显式拒绝)。"""
    resume_value = interrupt(_build_interrupt_payload(state))
    return _post_resume(state, resume_value=resume_value)