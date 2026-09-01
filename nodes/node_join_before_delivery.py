"""join_before_delivery — 中文主线 + 英文分支汇合节点(Week 4 新增,对照计划 §4.6)。

职责:轻量 QA 闸门,**不**做深度校验。失败时仅记录到 ``join_qa_issues`` 与
``error_log``,流程仍走到 END(便于联调观察,深度闸门留 Week 5)。

汇合条件:两条分支(中文尾段 → node_15、英文分支 → node_17)的输出都到本节点时
自动触发(LangGraph fan-in)。
"""

from __future__ import annotations

from state import WorkflowState


def join_before_delivery(state: WorkflowState) -> dict:
    """LangGraph 节点(汇合点):做轻量 QA 闸门。

    只返回变更字段(``join_qa_issues`` + 累积 ``error_log`` + ``status_log``),
    避免覆盖上游未变字段。
    """
    issues: list[str] = []

    covers = state.get("covers") or []
    if len(covers) < 3:
        issues.append(f"封面缺失:需3种比例,实际 {len(covers)} 种")
    else:
        for c in covers:
            if c.get("en_path") is None:
                issues.append(f"封面英文版缺失:ratio={c.get('ratio', 'unknown')}")

    if not state.get("subtitle_srt_path"):
        issues.append("英文字幕文件缺失")

    new_errors: list[str] = [f"[join] {it}" for it in issues]
    return {
        "join_qa_issues": issues,
        "error_log": new_errors,
        "status_log": ["join_before_delivery_done"],
    }