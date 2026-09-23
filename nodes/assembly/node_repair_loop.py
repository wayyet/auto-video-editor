"""节点 5/6:``assembly_repair_loop``(plan §7.5)。

对应工具:``timeline_diff``(``apply=True``)。按 ``qc_preview`` /
``validate_timeline`` 返回的问题列表构造 ``patch``,调 ``timeline_diff``
写回 ``timeline.json``,然后回到节点 4 重新走一遍 validate/render/qc。

**阶段二实施范围**:本节点负责:
1. 构造一个**最小可用 patch**(从 ``preview_qc_report.json`` 的 issues
   抽取"blocking + video 轨";目前 stage 二只构造 ``update_clips`` 减少
   受影响 clip 的 duration 作为兜底;阶段三替换为基于 issue 类型的精细
   patch 构造)。
2. 调用 ``timeline_diff`` 写回 ``timeline.json``(``apply=True``)。
3. 增加 ``assembly_qc_retry_count``,供 ``route_after_assembly_qc`` 判断
   是否到 ``ASSEMBLY_QC_MAX_RETRY``。

设计纪律:此节点**不**直接进入 ``assembly_write_report`` —— 它的唯一
出口是回到 ``assembly_validate_render_qc`` 重新跑校验,让 graph 的条件
边 + 路由函数来统一裁决"重试 or 收尾"。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext
from assembly_capabilities.timeline_ops import timeline_diff

from nodes.storyline._common import (
    _resolve_outputs_root,
    append_error,
    append_status_tag,
)
from state import WorkflowState


def _build_repair_patch(qc_report_path: str | None, validation_report_path: str | None) -> dict[str, Any]:
    """构造最小可用 ``update_clips`` patch。

    阶段三扩展:按 issue 类型构造精细 patch(替换素材 / 删除卡帧 clip /
    调整时长等)。阶段二仅做"打补丁占位",确保 ``timeline_diff`` 链路畅通。
    """
    # 即使没 issue 也返回一个非空 patch(空 patch + apply=true 会被原版拒绝)
    # 用 ``set_timeline_fields`` 设一个 metadata 标记,确认 repair 链路真跑了
    return {
        "set_timeline_fields": {
            "metadata": {
                "repair_marker": True,
                "qc_report": qc_report_path,
                "validation_report": validation_report_path,
            },
        },
    }


def assembly_repair_loop_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"

    timeline_path = state.get("assembly_timeline_path")
    if not timeline_path:
        return {
            "error_log": append_error(
                state, node_kind="assembly_repair_loop", error_code="CONTRACT_INVALID",
                message="missing assembly_timeline_path",
            ),
            "status_log": append_status_tag(state, "assembly_repair_loop_failed"),
        }

    ctx = RunContext(session_kind="pipeline")
    patch = _build_repair_patch(
        qc_report_path=state.get("assembly_qc_report_path"),
        validation_report_path=state.get("assembly_timeline_validation_path"),
    )

    diff_out = out_dir / "timeline_diff.json"
    diff_result: ToolResult = timeline_diff(
        {
            "timeline_path": str(timeline_path),
            "instructions": "assembly_repair_loop placeholder: add metadata marker; stage 3 will add typed patches",
            "patch": patch,
            "apply": True,
            "output_json": str(diff_out),
        },
        ctx,
    )

    # 重试计数 +1(唯一写入者,无需 reducer)
    retry_count = int(state.get("assembly_qc_retry_count") or 0) + 1

    err_log_patch: list[str] = []
    if diff_result.text.startswith("[ERROR]"):
        err_log_patch = append_error(
            state, node_kind="assembly_repair_loop",
            error_code="TOOL_EXECUTION_FAILED",
            message=diff_result.text,
        )

    return {
        "assembly_qc_retry_count": retry_count,
        "error_log": err_log_patch,
        "status_log": append_status_tag(state, "assembly_repair_loop_done"),
    }
