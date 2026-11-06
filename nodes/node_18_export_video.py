"""node_18:export_final_video — 最终 MP4 导出(2026-11-07 阶段 6,可选)。

位置:``join_before_delivery`` 之后、``END`` 之前(交付 QA 与导出**分离**,
计划 §8 阶段 6"将导出动作与草稿发布、人工关卡、交付 QA 分离")。

行为:
- ``config.FINAL_EXPORT_ENABLED=False``(默认)→ 显式记
  ``final_export_status="skipped"`` 并上报 ``export.skipped`` 事件,不渲染;
- 开启时读**权威中文主草稿**(``state.draft_path``)→
  :func:`draft_ops.draft_exporter.run_export` 渲染 + 校验 →
  ``final_export_status``("succeeded"/"failed") + ``final_export_report`` +
  ``final_video_path``(复用 Week 5 字段);
- 失败**不抛异常**(运行照常 END):失败必须显式落状态 + 上报
  ``export.failed`` + 写 error_log —— 不把"草稿文件存在"当成"成片交付成功"
  (计划阶段 6 验收标准);创建台交付状态卡会把 MP4 导出标红。

测试策略:开关/缺草稿/失败/成功四条路径全部依赖注入,不碰真 ffmpeg;
真渲染由 test_draft_exporter 覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from state import WorkflowState


def _emit_export_event(event: str, *, detail: dict) -> None:
    """经 LangGraph custom stream 上报 export.* 观测事件(与 node_05 同款)。

    图运行时外调用 ``get_stream_writer()`` 会 RuntimeError —— 观测事件绝不影响
    主流程,任何异常吞掉即可。
    """
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if callable(writer):
            writer(
                {
                    "monitor_event": "progress",
                    "node_id": "export_final_video",
                    "detail": {"event": event, **detail},
                }
            )
    except Exception:  # noqa: BLE001 — 观测通道失败不阻断工作流
        pass


def _resolve_output_path(state: WorkflowState) -> Path:
    """MP4 输出路径:``config.FINAL_EXPORT_DIR`` 优先,缺省任务产物根
    ``outputs/<session_id>/export/final.mp4``(跟随 STORYLINE_OUTPUTS_ROOT 的
    env 覆盖;不写进剪映草稿库,不污染草稿目录)。"""
    import config

    if config.FINAL_EXPORT_DIR:
        return Path(config.FINAL_EXPORT_DIR) / "final.mp4"
    from config import storyline_outputs_root

    session = str(state.get("session_id") or "unknown")
    return storyline_outputs_root() / session / "export" / "final.mp4"


def export_final_video(state: WorkflowState) -> dict[str, Any]:
    """LangGraph 节点:按开关导出中文主草稿为 MP4(独立于发布/关卡/QA)。"""
    import config

    if not config.FINAL_EXPORT_ENABLED:
        _emit_export_event(
            "export.skipped",
            detail={"reason": "未启用(AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED=0)"},
        )
        return {
            "final_export_status": "skipped",
            "status_log": ["node_18_export_skipped:disabled"],
        }

    draft_path_raw = state.get("draft_path")
    draft_file = Path(draft_path_raw) if draft_path_raw else None
    if draft_file is None or not draft_file.is_file():
        # 无草稿可导出:显式失败(绝不静默通过 —— 计划阶段 6 验收标准)
        reason = f"中文主草稿缺失或不存在: {draft_path_raw!r}"
        _emit_export_event("export.failed", detail={"reason": reason})
        return {
            "final_export_status": "failed",
            "final_export_report": {"status": "failed", "issues": [reason]},
            "error_log": [f"[node_18] {reason}"],
            "status_log": ["node_18_export_failed:no_draft"],
        }

    try:
        draft = json.loads(draft_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        reason = f"读取中文主草稿失败: {exc}"
        _emit_export_event("export.failed", detail={"reason": reason})
        return {
            "final_export_status": "failed",
            "final_export_report": {"status": "failed", "issues": [reason]},
            "error_log": [f"[node_18] {reason}"],
            "status_log": ["node_18_export_failed:draft_unreadable"],
        }

    out_path = _resolve_output_path(state)
    try:
        from draft_ops.draft_exporter import DraftExportError, run_export

        report = run_export(draft, out_path)
    except DraftExportError as exc:
        report = {"status": "failed", "issues": [str(exc)]}
    except Exception as exc:  # noqa: BLE001 — 导出失败不炸工作流,显式记失败
        report = {"status": "failed", "issues": [repr(exc)]}

    if report.get("status") == "succeeded":
        probed = report.get("probed") or {}
        _emit_export_event(
            "export.succeeded",
            detail={
                "output_path": report.get("output_path"),
                "duration_seconds": probed.get("duration_seconds"),
                "sha256": report.get("sha256"),
                "fidelity_warnings": report.get("fidelity_warnings") or [],
            },
        )
        return {
            "final_export_status": "succeeded",
            "final_export_report": report,
            "final_video_path": str(out_path),
            "status_log": [f"node_18_export_done:{out_path}"],
        }

    reason = "; ".join(report.get("issues") or ["未知原因"])[:500]
    _emit_export_event("export.failed", detail={"reason": reason})
    return {
        "final_export_status": "failed",
        "final_export_report": report,
        "error_log": [f"[node_18] MP4 导出失败: {reason}"],
        "status_log": ["node_18_export_failed"],
    }


__all__ = ["export_final_video"]
