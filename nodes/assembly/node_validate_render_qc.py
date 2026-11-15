"""节点 4/6:``assembly_validate_render_qc``(第六期 P1-5 候选草稿 QC 版)。

两条路径:
- **候选 QC 路径**(P1-5,state 有 ``assembly_candidate_plan``):用与正式
  ``generate_draft`` 相同的 mapper/native 链把候选计划映射成**临时草稿**
  (outputs/<job>/assembly/qc_draft/,与剪映草稿库完全隔离),做三层校验:
  ① 结构/引用完整性(draft_integrity 现成检查);② video track ↔ 候选 clip
  逐一按值核对;③ 关联轨道(配音/BGM)与 clips 组映射闭合;再渲染 preview
  并跑 ``qc_preview``。issues 全部带定位(clip_id / segment_id / 时间区间),
  落 ``candidate_qc_report.json``。
- **旧路径兜底**(无候选,单元测试 / 无基线流):P1-5 之前的行为 —— 对
  ``timeline.json`` 直接 validate/render/qc 三连。

判定规则(plan §7.4):``pass`` / ``pass_with_warnings`` / ``escalated``。
全程不修改 ``draft_path``、剪映草稿库或原始 storyline_plan。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assembly_capabilities.draft_preview_adapter import (
    CandidateDraftError,
    write_candidate_draft,
    validate_candidate_draft,
)
from assembly_capabilities.repair_strategies import has_structural_error
from assembly_capabilities.qc_preview import qc_preview
from assembly_capabilities.render_preview import render_preview
from assembly_capabilities.result import ToolResult
from assembly_capabilities.timing import timed_assembly_node
from assembly_capabilities.run_context import RunContext
from assembly_capabilities.timeline_ops import validate_timeline

import config
from nodes.storyline._common import (
    _resolve_outputs_root,
    append_error,
    append_status_tag,
)
from state import WorkflowState


def _qc_status_from_issues(issues: list[dict[str, Any]]) -> str:
    blocking = [i for i in issues if isinstance(i, dict) and i.get("severity") == "error"]
    warnings = [i for i in issues if isinstance(i, dict) and i.get("severity") == "warning"]
    if blocking:
        return "escalated"
    if warnings:
        return "pass_with_warnings"
    return "pass"


def _qc_status_from_data(qc_data: dict[str, Any]) -> str:
    return _qc_status_from_issues(qc_data.get("issues") or [])


def route_after_assembly_qc(state: WorkflowState) -> str:
    """``graph.py`` 条件边的路由函数(plan §7.5;P1-6 按 §4.4 修订 fail-closed)。

    - status in {pass, pass_with_warnings} → ``assembly_write_report``(直接收尾)
    - status == escalated 且 retry >= MAX:
      - 存在结构性 error(``assembly_structural_error``,候选路径 typed issues)
        → ``assembly_failed`` → END(fail-closed:结构坏掉的候选绝不交给
        generate_draft / 关卡①,取代旧 ADR-3 软降级)
      - 否则(仅 warning / 预览级问题)→ ``assembly_write_report`` 带 warning
        继续
    - status == escalated 且 retry < MAX → ``assembly_repair_loop``(再试一次)
    """
    status = state.get("assembly_qc_status")
    retry = int(state.get("assembly_qc_retry_count") or 0)
    if status in ("pass", "pass_with_warnings"):
        return "assembly_write_report"
    if retry >= config.ASSEMBLY_QC_MAX_RETRY:
        if state.get("assembly_structural_error"):
            return "assembly_failed"
        return "assembly_write_report"
    return "assembly_repair_loop"


def _preview_issues_from_qc_data(qc_data: dict[str, Any]) -> list[dict[str, Any]]:
    """把 qc_preview 的 issues 转成候选 QC 的 typed issue(黑帧/静音/卡帧等)。"""
    code_map = {
        "black frames detected": "black_frame",
        "silence detected": "silence",
        "freeze frames detected": "freeze_frame",
    }
    out: list[dict[str, Any]] = []
    for issue in qc_data.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        message = str(issue.get("message") or "")
        code = next((c for key, c in code_map.items() if key in message.lower()), "preview_issue")
        row = {
            "severity": issue.get("severity"),
            "code": code,
            "check": "候选预览 QC",
            "message": message,
        }
        if issue.get("evidence"):
            row["evidence"] = str(issue["evidence"])[:2000]
        out.append(row)
    return out


def _candidate_qc_path(state: WorkflowState, out_dir: Path, ctx: RunContext) -> dict:
    """P1-5 主路径:候选计划 → 临时草稿 → 结构/一致性校验 → preview QC。"""
    candidate_plan = state.get("assembly_candidate_plan") or {}
    timeline_path = state.get("assembly_timeline_path")
    issues: list[dict[str, Any]] = []
    binding: list[dict[str, Any]] = []
    draft_info: dict[str, Any] = {}

    # ---- 1. 候选 → 临时草稿(与正式链同一 mapper/native)----
    try:
        draft_info = write_candidate_draft(candidate_plan, out_dir)
    except CandidateDraftError as exc:
        return {
            "assembly_qc_status": "escalated",
            "assembly_candidate_qc_report_path": None,
            "error_log": append_error(
                state, node_kind="assembly_validate_render_qc",
                error_code="CONTRACT_INVALID",
                message=f"candidate draft mapping failed: {exc}",
            ),
            "status_log": append_status_tag(state, "assembly_validate_render_qc_failed"),
        }

    # ---- 2. 结构 + 一致性校验(结构跑 native;音频映射跑 mapper 输出)----
    qc_checks = validate_candidate_draft(
        candidate_plan,
        draft_info["draft_content"],
        storyline_draft=draft_info.get("storyline_draft"),
    )
    issues.extend(qc_checks["issues"])
    binding = qc_checks["binding"]

    # ---- 3. timeline 投影校验(沿用 validate_timeline)----
    # 纪律(P1-6 排障修正):投影层的 ffprobe/素材探测问题(如素材文件缺失)
    # 属 preview_issue(**非结构性**)—— 候选自身的结构完整性由第 2 步
    # validate_candidate_draft(binding/引用闭合/契约硬门)负责并给结构性
    # signal;两者混淆会让"预置草稿 + 伪素材路径"的合法流被 fail-closed 误杀。
    validation_out = out_dir / "timeline_validation.json"
    validation_ok = True
    if timeline_path:
        validate_result: ToolResult = validate_timeline(
            {"timeline_path": str(timeline_path), "output_json": str(validation_out)},
            ctx,
        )
        if validate_result.text.startswith("[ERROR]"):
            validation_ok = False
            issues.append({
                "severity": "error",
                "code": "preview_issue",
                "check": "timeline 投影校验",
                "message": validate_result.text,
            })
        else:
            for issue in validate_result.data.get("issues") or []:
                if isinstance(issue, dict):
                    issues.append({
                        "severity": issue.get("severity"),
                        "code": "preview_issue",
                        "check": "timeline 投影校验",
                        "message": issue.get("message"),
                    })

    # ---- 4. preview 渲染 + qc_preview ----
    preview_out = out_dir / "preview.mp4"
    qc_out = out_dir / "preview_qc_report.json"
    render_ok = True
    if timeline_path:
        render_result: ToolResult = render_preview(
            {"timeline_path": str(timeline_path), "output_path": str(preview_out)},
            ctx,
        )
        if render_result.text.startswith("[ERROR]"):
            render_ok = False
            issues.append({
                "severity": "error",
                "code": "preview_issue",
                "check": "候选预览渲染",
                "message": render_result.text,
            })
        else:
            qc_result: ToolResult = qc_preview(
                {
                    "video_path": str(preview_out),
                    "timeline_path": str(timeline_path),
                    "output_json": str(qc_out),
                },
                ctx,
            )
            if qc_result.text.startswith("[ERROR]"):
                issues.append({
                    "severity": "error",
                    "code": "preview_issue",
                    "check": "候选预览 QC",
                    "message": qc_result.text,
                })
            else:
                issues.extend(_preview_issues_from_qc_data(qc_result.data or {}))

    qc_status = _qc_status_from_issues(issues)
    report_path = out_dir / "candidate_qc_report.json"
    report = {
        "candidate_qc": True,
        "status": qc_status,
        "candidate_plan_sha256": state.get("assembly_candidate_plan_sha256"),
        "draft_dir": draft_info.get("draft_dir"),
        "draft_content_path": draft_info.get("draft_content_path"),
        "binding": binding,
        "issues": issues,
        "timeline_validation_ok": validation_ok,
        "preview_render_ok": render_ok,
        "preview_qc_report": str(qc_out) if qc_out.is_file() else None,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    delta: dict[str, Any] = {
        "assembly_candidate_draft_dir": draft_info.get("draft_dir"),
        "assembly_candidate_qc_report_path": str(report_path),
        "assembly_timeline_validation_path": str(validation_out) if validation_out.is_file() else None,
        "assembly_preview_path": str(preview_out) if render_ok and preview_out.is_file() else None,
        "assembly_qc_report_path": str(qc_out) if qc_out.is_file() else None,
        "assembly_qc_status": qc_status,
        # P1-6 fail-closed 判据:结构性 error(broken_reference/track_desync 等)
        # 存在时,达重试上限 → 路由 assembly_failed → END
        "assembly_structural_error": has_structural_error(issues),
        "error_log": [],
        "status_log": append_status_tag(state, "assembly_validate_render_qc_done"),
    }
    if qc_status == "escalated":
        delta["error_log"] = append_error(
            state, node_kind="assembly_validate_render_qc",
            error_code="QC_ESCALATED",
            message=f"candidate QC escalated with {len([i for i in issues if i.get('severity') == 'error'])} blocking issues(见 candidate_qc_report.json)",
        )
    return delta


def _legacy_qc_path(state: WorkflowState, out_dir: Path, ctx: RunContext) -> dict:
    """旧路径:P1-5 之前的行为(对 timeline.json 直接三连)。"""
    timeline_path = state.get("assembly_timeline_path")
    validation_out = out_dir / "timeline_validation.json"
    preview_out = out_dir / "preview.mp4"
    qc_out = out_dir / "preview_qc_report.json"

    validate_result: ToolResult = validate_timeline(
        {"timeline_path": str(timeline_path), "output_json": str(validation_out)},
        ctx,
    )
    if validate_result.text.startswith("[ERROR]"):
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

    render_result: ToolResult = render_preview(
        {"timeline_path": str(timeline_path), "output_path": str(preview_out)},
        ctx,
    )
    if render_result.text.startswith("[ERROR]"):
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
        "assembly_structural_error": False,  # 旧路径只有预览级问题,保持软降级
        "error_log": err_log_patch,
        "status_log": append_status_tag(state, "assembly_validate_render_qc_done"),
    }


@timed_assembly_node("assembly_validate_render_qc")
def assembly_validate_render_qc_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not state.get("assembly_timeline_path"):
        # P0 修复:早退必须同时设 status 与重试计数,否则本节点不写
        # assembly_qc_retry_count → route_after_assembly_qc 永远看到同一个
        # retry 值 → repair_loop → 本节点死循环,自旋到 LangGraph 递归上限。
        # status 设 escalated(而非留空)保证走 repair → write_report 的软降级
        # 路径,不会被误判为达标。
        return {
            "assembly_qc_status": "escalated",
            "assembly_qc_retry_count": int(state.get("assembly_qc_retry_count") or 0) + 1,
            "error_log": append_error(
                state, node_kind="assembly_validate_render_qc",
                error_code="CONTRACT_INVALID",
                message="missing assembly_timeline_path (run assembly_build_timeline first)",
            ),
            "status_log": append_status_tag(state, "assembly_validate_render_qc_failed"),
        }

    ctx = RunContext(session_kind="pipeline")
    if state.get("assembly_candidate_plan"):
        return _candidate_qc_path(state, out_dir, ctx)
    return _legacy_qc_path(state, out_dir, ctx)
