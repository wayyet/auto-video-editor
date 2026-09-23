"""节点 4/6:``assembly_validate_render_qc``(plan §7.4)。

对应工具:``validate_timeline`` + ``render_preview`` + ``qc_preview`` 三连。

判定规则(plan §7.4):
- ``status = "pass"`` 若 ``qc_data.blocking_issues`` 为空
- ``status = "pass_with_warnings"`` 若只有 ``warning_issues``
- ``status = "escalated"`` 若有 ``blocking_issues``
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assembly_capabilities.qc_preview import qc_preview
from assembly_capabilities.render_preview import render_preview
from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext
from assembly_capabilities.timeline_ops import validate_timeline

import config
from nodes.storyline._common import (
    _resolve_outputs_root,
    append_error,
    append_status_tag,
)
from state import WorkflowState


def _qc_status_from_data(qc_data: dict[str, Any]) -> str:
    issues = qc_data.get("issues") or []
    blocking = [i for i in issues if isinstance(i, dict) and i.get("severity") == "error"]
    warnings = [i for i in issues if isinstance(i, dict) and i.get("severity") == "warning"]
    if blocking:
        return "escalated"
    if warnings:
        return "pass_with_warnings"
    return "pass"


def route_after_assembly_qc(state: WorkflowState) -> str:
    """``graph.py`` 条件边的路由函数(plan §7.5)。

    - status in {pass, pass_with_warnings} → ``assembly_write_report``(直接收尾)
    - status == escalated 且 retry >= MAX → ``assembly_write_report``(软降级,
      不阻断流水线,plan ADR-3)
    - status == escalated 且 retry < MAX → ``assembly_repair_loop``(再试一次)
    """
    status = state.get("assembly_qc_status")
    retry = int(state.get("assembly_qc_retry_count") or 0)
    if status in ("pass", "pass_with_warnings"):
        return "assembly_write_report"
    if retry >= config.ASSEMBLY_QC_MAX_RETRY:
        return "assembly_write_report"
    return "assembly_repair_loop"


def assembly_validate_render_qc_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"
    out_dir.mkdir(parents=True, exist_ok=True)

    timeline_path = state.get("assembly_timeline_path")
    if not timeline_path:
        return {
            "error_log": append_error(
                state, node_kind="assembly_validate_render_qc",
                error_code="CONTRACT_INVALID",
                message="missing assembly_timeline_path (run assembly_build_timeline first)",
            ),
            "status_log": append_status_tag(state, "assembly_validate_render_qc_failed"),
        }

    ctx = RunContext(session_kind="pipeline")
    validation_out = out_dir / "timeline_validation.json"
    preview_out = out_dir / "preview.mp4"
    qc_out = out_dir / "preview_qc_report.json"

    # ---- 1. validate_timeline ----
    validate_result: ToolResult = validate_timeline(
        {"timeline_path": str(timeline_path), "output_json": str(validation_out)},
        ctx,
    )
    if validate_result.text.startswith("[ERROR]"):
        # timeline 校验失败 → 不进入渲染,直接 escalated
        return {
            "assembly_timeline_validation_path": str(validation_out),
            "assembly_qc_status": "escalated",
            "error_log": append_error(
                state, node_kind="assembly_validate_render_qc",
                error_code="TOOL_EXECUTION_FAILED",
                message=validate_result.text,
            ),
            "status_log": append_status_tag(state, "assembly_validate_render_qc_done"),
        }

    # ---- 2. render_preview ----
    render_result: ToolResult = render_preview(
        {"timeline_path": str(timeline_path), "output_path": str(preview_out)},
        ctx,
    )
    if render_result.text.startswith("[ERROR]"):
        # 渲染失败 → 不进入 QC,直接 escalated
        return {
            "assembly_timeline_validation_path": str(validation_out),
            "assembly_preview_path": None,
            "assembly_qc_status": "escalated",
            "assembly_qc_report_path": None,
            "error_log": append_error(
                state, node_kind="assembly_validate_render_qc",
                error_code="TOOL_EXECUTION_FAILED",
                message=render_result.text,
            ),
            "status_log": append_status_tag(state, "assembly_validate_render_qc_done"),
        }

    # ---- 3. qc_preview ----
    qc_result: ToolResult = qc_preview(
        {
            "video_path": str(preview_out),
            "timeline_path": str(timeline_path),
            "output_json": str(qc_out),
        },
        ctx,
    )
    qc_data = qc_result.data or {}
    qc_status = _qc_status_from_data(qc_data)

    err_log_patch: list[str] = []
    if qc_result.text.startswith("[ERROR]") and qc_status == "escalated":
        err_log_patch = append_error(
            state, node_kind="assembly_validate_render_qc",
            error_code="TOOL_EXECUTION_FAILED",
            message=qc_result.text,
        )

    return {
        "assembly_timeline_validation_path": str(validation_out),
        "assembly_preview_path": str(preview_out),
        "assembly_qc_report_path": str(qc_out) if qc_out.is_file() else None,
        "assembly_qc_status": qc_status,
        "error_log": err_log_patch,
        "status_log": append_status_tag(state, "assembly_validate_render_qc_done"),
    }
