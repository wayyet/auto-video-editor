"""节点 6:human_reorder — 关卡①(对应原文档 4.1 节)。

2026-11-20 条件挂起改造(审查修订版执行计划 §3.1/§3.2):关卡① 不再每轮
挂起要求人工调序 —— **仅 assembly QC 降级(escalated)时挂起人工复核**;
QC 正常(pass / pass_with_warnings)与 QC 关闭(状态缺失)自动通过。

设计:副作用挪后方案(不变)。
- ``interrupt()`` 之前**不**调用 ``send_notification``(避免重放时重复发送)
- resume 后第一个 task 才发通知
- 幂等不依赖任何"已通知"状态位(由 LangGraph resume 语义保证;
  2026-11-12 阶段5批1 移除无人消费的 ``reorder_notified`` 审计位)

调用栈(2026-11-20 改造后):
    节点 5 (generate_draft) → 节点 6
    → QC 正常 / QC 关闭:自动通过(写 checkpoint1_auto_passed* 审计标签)→ 节点 7
    → QC 降级(escalated):interrupt ⏸ → 人工看报告后 Command(resume=True|reject)
      → 节点 6 继续执行(发通知 + 写 status_log)→ 节点 7

Week 5 改动(对齐第 5 周计划 §1.1,不变):
- interrupt payload ``checkpoint`` 字段统一为 ``"①"``,便于
  ``resume_all_pending`` 自动匹配多个 interrupt。旧值
  ``"checkpoint1_reorder"`` 保留在 ``legacy_id`` 字段,保证向后兼容。
- 通知文案带 ``"checkpoint1"`` 标记,与 status_log ``checkpoint1_resumed`` 对齐。

测试策略:
- ``_build_interrupt_payload`` / ``_post_resume`` / ``_mark_auto_pass`` 抽成
  纯函数,单测直接覆盖
- ``human_reorder`` 的控制分支单测经 monkeypatch ``interrupt`` 覆盖,
  集成测试通过完整 graph + ``InMemorySaver`` 覆盖
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
            "assembly QC 修复重试后仍未达标(escalated,非结构性降级);"
            "请打开 QC 报告与预览确认,可接受则放行(恢复值 true),"
            "需要终止则输入恢复值 reject"
        ),
    )


def _post_resume(
    state: WorkflowState,
    resume_value: Any = None,
    notifier=None,
) -> dict:
    """resume 后逻辑 — 纯函数,便于单测(notifier 保持可注入,单测直接传假函数;
    缺省时调用时解析模块级 ``_send_notification`` —— 2026-11-20 改为惰性绑定,
    使 ``monkeypatch(n06, "_send_notification")`` 对 ``human_reorder`` 路径生效)。

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
        notifier=notifier or _send_notification,
    )


def _mark_auto_pass(state: WorkflowState, *, qc_status: str | None) -> dict:
    """QC 正常 / QC 关闭 → 自动通过的审计收尾(2026-11-20 条件挂起改造)。

    - 不发人工通知、不写 ``checkpoint1_resumed``(那是人工确认专用标记,
      自动通过不得冒充"用户已人工确认");
    - 自动通过原因用独立 status_log 标签留痕,QC 关闭与 QC 正常可区分,
      便于运行轨迹排查。

    纯函数(不读盘、无副作用之外的输出);status_log 由 ``_append_unique``
    reducer 合并,直接返回增量即可。
    """
    tag = (
        "checkpoint1_auto_passed"
        if qc_status is not None
        else "checkpoint1_auto_passed_qc_disabled"
    )
    print(
        f"[notify] checkpoint1 auto-pass thread={state.get('session_id')} "
        f"qc_status={qc_status!r} tag={tag}"
    )
    return {"status_log": [tag]}


def human_reorder(state: WorkflowState) -> dict:
    """关卡①:仅 assembly QC 降级(escalated)时挂起人工复核,其余自动通过。

    判定矩阵(2026-11-20 审查修订版执行计划 §3.1/§3.2,按序判定):

    1. ``assembly_structural_error=True`` → fail-closed(结构性错误绝不允许
       人工放行;上游正常路由本就 assembly_failed → END,此处兜底保护旧
       checkpoint / 异常状态)。
    2. ``assembly_qc_gate_enabled`` 缺失(None)= 标记引入前的旧 checkpoint
       (storyline_load_media 2026-11-20 起写入该标记):完整保留旧版无条件
       挂起行为 —— resume 重放时 ``interrupt()`` 直接返回已存恢复值,存量
       任务兼容性不受影响。
    3. QC 状态 pass / pass_with_warnings → 自动通过(独立审计标签)。
    4. QC 状态 escalated → 挂起人工复核(放行 true / reject 终止)。
    5. 状态缺失:QC 关闭(标记 False)→ 自动通过(qc_disabled 审计);
       QC 启用(标记 True)→ fail-closed(异常状态,不得静默放行)。
    6. 未知状态值 → fail-closed。
    """
    if state.get("assembly_structural_error") is True:
        raise RuntimeError(
            "[node_06] 检测到 assembly 结构性 QC 错误(assembly_structural_error"
            "=True),禁止人工放行;请检查 QC 报告修复后重试"
        )

    qc_enabled = state.get("assembly_qc_gate_enabled")
    qc_status = state.get("assembly_qc_status")

    if qc_enabled is None:
        # 旧 checkpoint(标记引入前的运行):无条件挂起 = 旧版行为原样保留
        resume_value = interrupt(_build_interrupt_payload(state))
        return _post_resume(state, resume_value=resume_value)

    if qc_status in ("pass", "pass_with_warnings"):
        return _mark_auto_pass(state, qc_status=qc_status)
    if qc_status == "escalated":
        resume_value = interrupt(_build_interrupt_payload(state))
        return _post_resume(state, resume_value=resume_value)
    if qc_status is None:
        if qc_enabled is False:
            # QC 通道关闭:状态缺失属预期形态 → 自动通过
            return _mark_auto_pass(state, qc_status=None)
        raise RuntimeError(
            "[node_06] assembly QC 已启用但缺少 assembly_qc_status(异常状态,"
            "不自动放行);请检查 assembly 六节点是否完整执行"
        )
    raise RuntimeError(f"[node_06] 未知 assembly_qc_status: {qc_status!r},不自动放行")