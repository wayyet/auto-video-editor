"""节点 6/6:``assembly_write_report``(plan §7.6)。

汇总前 5 步所有产物(素材清单、转写摘要、选段理由、质检结果、修复记录),
写成 Markdown 报告,路径写入 ``assembly_report_path``。

设计纪律:
- **绝不**抛异常 —— 任何步骤都包 try/except,失败就降级写空 section,
  保证关卡① 拿到的是"可读的、最坏情况是空报告"而不是流水线崩溃。
- 报告开头含一行提示:"本次已生成组装质检报告,建议对照剪映草稿一并查看"
  (plan §7.6 + ADR-3)。关卡① 的人工通知文案会引用此路径。
- 2026-11-15 第六期 P1-1 前移:本节点出口改为 ``generate_draft``(交棒草稿
  生成),不再是"草稿生成之后的收尾旁路";report.md 仍是关卡① 可见的
  审计产物。
- 2026-11-15 第六期 P1-7:本节点成为**最终编辑计划的正式提交者** —— 对
  候选计划做模型硬门 + 双重 hash 一致校验后写 ``final_plan.json`` 并提交
  ``assembly_final_plan*`` 三件套,由紧随其后的 ``generate_draft`` 消费。
  结构性错误被阻断时不提交(generate_draft 自动回退原始 storyline_plan);
  报告本身绝不因此抛异常。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assembly_capabilities.draft_patch import OPERATION_TYPES
from assembly_capabilities.timing import timed_assembly_node
from assembly_capabilities.influence_trace import (
    add_contribution,
    canonical_json_sha256,
    load_or_create_trace,
    save_trace,
)
from assembly_capabilities.repair_strategies import (
    STRUCTURAL_CODES,
    has_structural_error,
)
from assembly_capabilities.timing import summarize_timing
from nodes.storyline._common import (
    _resolve_outputs_root,
    append_status_tag,
)
from state import WorkflowState
from storyline.contract import CanonicalTimeline


def _safe_read_json(path: str | Path | None) -> Any:
    if not path:
        return None
    p = Path(str(path))
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _summarize_media(media_data: Any) -> str:
    """素材清单摘要(2026-11-15 P1-2 起 media.json 为 manifest dict,兼容旧 list)。"""
    if isinstance(media_data, dict):
        # manifest 形状:sources[] 带探测/分析状态与候选明细
        sources = [s for s in media_data.get("sources") or [] if isinstance(s, dict)]
        lines: list[str] = [
            f"- 素材数量:{len(sources)}"
            f"(manifest v{media_data.get('schema_version', '?')},"
            f" base_plan_sha256={media_data.get('base_plan_sha256') or 'n/a'})"
        ]
        for i, src in enumerate(sources[:5], start=1):
            lines.append(
                f"  - [{i}] `{src.get('source_path', '<unknown>')}`"
                f" — asset_id={src.get('asset_id')},"
                f" probe={src.get('probe_status')}, analysis={src.get('analysis_status')},"
                f" duration={src.get('duration_seconds')}s,"
                f" candidates={len(src.get('candidate_segments') or [])}"
                f"{', degraded: ' + str(src.get('degraded_reason')) if src.get('degraded_reason') else ''}"
            )
        if len(sources) > 5:
            lines.append(f"  - ...(其余 {len(sources) - 5} 条省略)")
        if media_data.get("degraded"):
            lines.append("- ⚠️ 存在探测/分析失败的素材(见 degraded_reason),相关候选不可用")
        return "\n".join(lines)
    if not isinstance(media_data, list):
        return "- (无素材清单)"

    media_reports = media_data
    lines = [f"- 素材数量:{len(media_reports)}"]
    for i, rep in enumerate(media_reports[:5], start=1):
        source = rep.get("source", "<unknown>")
        analysis = rep.get("analysis") or {}
        scenes = (analysis.get("scene_change_count") if isinstance(analysis, dict) else None)
        candidates = (analysis.get("candidate_segment_count") if isinstance(analysis, dict) else None)
        duration = (analysis.get("duration_seconds") if isinstance(analysis, dict) else None)
        lines.append(
            f"  - [{i}] `{source}` — duration={duration}s, scenes={scenes}, candidates={candidates}"
        )
    if len(media_reports) > 5:
        lines.append(f"  - ...(其余 {len(media_reports) - 5} 条省略)")
    return "\n".join(lines)


def _summarize_qc(qc_data: Any) -> str:
    if not isinstance(qc_data, dict):
        return "- (无 QC 报告)"
    status = qc_data.get("status", "unknown")
    issues = qc_data.get("issues") or []
    blocking = [i for i in issues if isinstance(i, dict) and i.get("severity") == "error"]
    warnings = [i for i in issues if isinstance(i, dict) and i.get("severity") == "warning"]
    lines = [
        f"- 整体状态:`{status}`",
        f"- 阻断项:{len(blocking)} 条 / 警告项:{len(warnings)} 条",
    ]
    for i in blocking[:10]:
        lines.append(f"  - [ERROR] {i.get('message', '')}")
    for i in warnings[:10]:
        lines.append(f"  - [WARN] {i.get('message', '')}")
    if len(blocking) > 10:
        lines.append(f"  - ...(其余 {len(blocking) - 10} 条阻断项省略)")
    if len(warnings) > 10:
        lines.append(f"  - ...(其余 {len(warnings) - 10} 条警告省略)")
    return "\n".join(lines)


def _summarize_validation(validation_data: Any) -> str:
    if not isinstance(validation_data, dict):
        return "- (无 validation 报告)"
    status = validation_data.get("status", "unknown")
    clip_count = validation_data.get("clip_count", 0)
    issues = validation_data.get("issues") or []
    errors = [i for i in issues if isinstance(i, dict) and i.get("severity") == "error"]
    warnings = [i for i in issues if isinstance(i, dict) and i.get("severity") == "warning"]
    lines = [
        f"- 整体状态:`{status}`",
        f"- clip 数量:{clip_count}",
        f"- error:{len(errors)} 条 / warning:{len(warnings)} 条",
    ]
    for i in errors[:5]:
        lines.append(f"  - [ERROR] {i.get('message', '')}")
    return "\n".join(lines)


def _summarize_timeline(timeline_path: str | None) -> str:
    if not timeline_path:
        return "- (timeline.json 未生成)"
    data = _safe_read_json(timeline_path)
    if not isinstance(data, dict):
        return f"- timeline 文件存在但无法解析:`{timeline_path}`"
    tracks = data.get("tracks") or []
    total_clips = sum(len(t.get("clips") or []) for t in tracks if isinstance(t, dict))
    sequence = data.get("sequence") or {}
    lines = [
        f"- 路径:`{timeline_path}`",
        f"- 轨道数:{len(tracks)}",
        f"- 总 clip 数:{total_clips}",
        f"- sequence.duration:{sequence.get('duration')}s, fps:{sequence.get('fps')}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# P1-7:候选计划摘要 / 最终计划提交 / 影响链收尾
# ---------------------------------------------------------------------------
def _repair_rounds(repair_history: Any) -> list[dict[str, Any]]:
    """repair_history.json → 轮次列表(兼容 list 与 {"rounds": list} 两形态)。"""
    if isinstance(repair_history, list):
        return [r for r in repair_history if isinstance(r, dict)]
    if isinstance(repair_history, dict):
        rounds = repair_history.get("rounds")
        if isinstance(rounds, list):
            return [r for r in rounds if isinstance(r, dict)]
    return []


def _summarize_candidate_and_repair(
    candidate_path: str | None,
    patch: Any,
    repair_history: Any,
) -> str:
    lines: list[str] = [f"- 候选计划:`{candidate_path or '(未生成)'}`"]
    if isinstance(patch, dict):
        ops = [o for o in patch.get("operations") or [] if isinstance(o, dict)]
        by_type: dict[str, int] = {}
        for o in ops:
            key = str(o.get("operation") or "unknown")
            by_type[key] = by_type.get(key, 0) + 1
        known = ", ".join(f"{t}={by_type[t]}" for t in sorted(by_type) if t in OPERATION_TYPES)
        unknown = ", ".join(f"{t}={n}" for t, n in sorted(by_type.items()) if t not in OPERATION_TYPES)
        lines.append(
            f"- patch 操作:{len(ops)} 项({known or '无'}"
            + (f";未知类型 {unknown}" if unknown else "")
            + ")"
        )
    else:
        lines.append("- patch 操作:(无 DraftPatch)")
    rounds = _repair_rounds(repair_history)
    if rounds:
        last = rounds[-1]
        lines.append(f"- 修复轮次:{len(rounds)}(最后一轮 round={last.get('round')})")
        if last.get("noop_reason"):
            lines.append(f"- 最后一轮未产生有效变化(noop):{last.get('noop_reason')}")
    else:
        lines.append("- 修复轮次:0(QC 首轮通过,无修复)")
    return "\n".join(lines)


def _summarize_llm_diagnostics(diag: Any) -> str:
    """P2-1 LLM 调用诊断摘要(stub 显式标注,请求状态与采纳状态分离)。"""
    if not isinstance(diag, dict):
        return "- LLM 调用诊断:(未产出)"
    stats = diag.get("selection_stats") or {}
    lines = [
        f"- 客户端:`{diag.get('llm_client_type') or '?'}`"
        f"(model={diag.get('model_name') or 'n/a'})",
        f"- 是否发起请求:{'是' if diag.get('llm_attempted') else '否'}"
        f",请求状态:`{diag.get('request_status') or '?'}`"
        f",响应有效:{'是' if diag.get('response_valid') else '否'}",
        f"- 选段采纳:{'是' if diag.get('selection_applied') else '否'}"
        f",兜底:{'是' if diag.get('fallback_used') else '否'}"
        f",索引统计:returned={stats.get('returned', 0)} invalid={stats.get('invalid', 0)}"
        f" duplicate={stats.get('duplicate', 0)} adopted={stats.get('adopted', 0)}",
        f"- 耗时:{diag.get('elapsed_seconds', 0)}s",
    ]
    if diag.get("failure_reason"):
        lines.append(f"- 失败原因:{diag.get('failure_reason')}")
    return "\n".join(lines)


def _add_contribution_once(trace: dict[str, Any], **kwargs: Any) -> None:
    """幂等追加 contribution(resume/重跑时 (node_id, artifact) 不重复)。"""
    existing = {
        (c.get("node_id"), c.get("artifact"))
        for c in trace.get("node_contributions") or []
        if isinstance(c, dict)
    }
    if (kwargs.get("node_id"), kwargs.get("artifact")) in existing:
        return
    add_contribution(trace, **kwargs)


def _submit_final_plan(state: WorkflowState, out_dir: Path) -> dict[str, Any]:
    """校验候选计划并提交最终计划(绝不抛异常;P1-7)。

    返回::

        {
          "final_plan": dict | None,
          "final_plan_path": str | None,
          "final_plan_sha256": str | None,
          "final_status": "submitted" | "blocked" | "not_available",
          "final_notes": [...],   # 提交/阻断原因,进报告与 status_log
        }

    阻断判据(任一命中即 blocked,不提交):
    1. ``assembly_structural_error`` 为 True(validate/repair 链的 fail-closed
       标记;路由层已把"结构性错误达上限"的运行送 END,这里是防御纵深);
    2. candidate QC issues 命中 ``repair_strategies.STRUCTURAL_CODES`` 的
       error 级问题;
    3. 候选计划模型校验失败;
    4. 候选计划 sha 与 state 记录漂移;
    5. 基线 storyline_plan 与 discover 记录的 base sha 漂移。
    """
    notes: list[str] = []
    candidate = state.get("assembly_candidate_plan")
    candidate_path = state.get("assembly_candidate_plan_path")
    if not isinstance(candidate, dict):
        loaded = _safe_read_json(candidate_path)
        if isinstance(loaded, dict):
            candidate = loaded
            notes.append("候选计划从磁盘产物读取(state 内联缺失)")
    if not isinstance(candidate, dict):
        return {
            "final_plan": None,
            "final_plan_path": None,
            "final_plan_sha256": None,
            "final_status": "not_available",
            "final_notes": notes + ["无候选计划(QC 关姿态或 build_timeline 未产出)"],
        }

    blocked_reason: str | None = None
    if bool(state.get("assembly_structural_error")):
        blocked_reason = "assembly_structural_error=True(validate/repair 链 fail-closed 标记)"
    if blocked_reason is None:
        cand_qc = _safe_read_json(state.get("assembly_candidate_qc_report_path"))
        issues = cand_qc.get("issues") if isinstance(cand_qc, dict) else None
        if isinstance(issues, list) and has_structural_error(issues):
            hit_codes = sorted(
                {
                    str(i.get("code"))
                    for i in issues
                    if isinstance(i, dict)
                    and i.get("severity") == "error"
                    and i.get("code") in STRUCTURAL_CODES
                }
            )
            blocked_reason = (
                "candidate QC issues 含结构性 error(fail-closed): "
                + ", ".join(hit_codes)
            )

    if blocked_reason is None:
        try:
            CanonicalTimeline.model_validate(candidate)
        except Exception as exc:  # noqa: BLE001 — 校验失败=阻断,不是崩溃
            blocked_reason = f"候选计划模型校验失败: {exc!r}"

    if blocked_reason is None:
        cand_sha_state = state.get("assembly_candidate_plan_sha256")
        actual_sha = canonical_json_sha256(candidate)
        if cand_sha_state and actual_sha != str(cand_sha_state):
            blocked_reason = (
                f"候选计划 sha 漂移: expected={cand_sha_state} actual={actual_sha}"
            )

    if blocked_reason is None:
        base_sha = state.get("assembly_base_plan_sha256")
        storyline_plan = state.get("storyline_plan")
        if base_sha and isinstance(storyline_plan, dict):
            actual_base = canonical_json_sha256(storyline_plan)
            if actual_base != str(base_sha):
                blocked_reason = (
                    "基线 storyline_plan 漂移: "
                    f"expected={base_sha} actual={actual_base}"
                )

    if blocked_reason is not None:
        return {
            "final_plan": None,
            "final_plan_path": None,
            "final_plan_sha256": None,
            "final_status": "blocked",
            "final_notes": notes + [blocked_reason],
        }

    final_path = out_dir / "final_plan.json"
    final_path.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    final_sha = canonical_json_sha256(candidate)
    notes.append(f"最终计划已提交: {final_path}")
    return {
        "final_plan": candidate,
        "final_plan_path": str(final_path),
        "final_plan_sha256": final_sha,
        "final_status": "submitted",
        "final_notes": notes,
    }


def _finalize_influence_trace(
    state: WorkflowState,
    out_dir: Path,
    *,
    final_sha: str | None,
) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    """补齐六节点 contribution 并固化 final_plan_sha256(绝不抛异常)。

    build_timeline 已写自己的 contribution;这里补 discover/observe/validate/
    repair/write_report 五条(repair 未执行时以 extra.executed=False 记录,
    保证六节点在 trace 中始终可审计),提交成功时固化 ``final_plan_sha256``。
    ``mapped_draft_sha256`` 由 generate_draft 消费后回写。
    """
    notes: list[str] = []
    try:
        base_sha = state.get("assembly_base_plan_sha256")
        trace_path = out_dir / "influence_trace.json"
        trace = load_or_create_trace(
            trace_path,
            str(base_sha)
            if base_sha
            else canonical_json_sha256({"base_plan_sha256": None}),
        )
        _add_contribution_once(
            trace,
            node_id="assembly_discover_and_probe",
            artifact="assembly/media.json",
            consumed_by=["assembly_visual_observe", "assembly_build_timeline"],
        )
        # build_timeline 的 contribution 本应已由其节点写入;这里兜底补全
        # (once-guard 会保留 build 已写的带 affected_operations 的完整版本)
        _add_contribution_once(
            trace,
            node_id="assembly_build_timeline",
            artifact="assembly/candidate_plan.json",
            consumed_by=["assembly_validate_render_qc", "generate_draft"],
        )
        _add_contribution_once(
            trace,
            node_id="assembly_visual_observe",
            artifact="assembly/visual_observation.json",
            consumed_by=["assembly_build_timeline"],
        )
        _add_contribution_once(
            trace,
            node_id="assembly_validate_render_qc",
            artifact="assembly/candidate_qc_report.json",
            consumed_by=["assembly_repair_loop", "assembly_write_report"],
        )
        _add_contribution_once(
            trace,
            node_id="assembly_repair_loop",
            artifact="assembly/repair_history.json",
            consumed_by=["assembly_write_report"],
            extra={
                "executed": bool(state.get("assembly_repair_history_path")),
                "note": "QC 首轮通过时无修复轮次,贡献以 executed=false 记录",
            }
            if not state.get("assembly_repair_history_path")
            else None,
        )
        _add_contribution_once(
            trace,
            node_id="assembly_write_report",
            artifact="assembly/final_plan.json",
            consumed_by=["generate_draft"],
        )
        if final_sha:
            trace["final_plan_sha256"] = final_sha
        # P2-3:四级 plan hash 链(原始 → 候选 → 修复轮 → 最终),审计与回滚
        # 对账的唯一口径;repair 轮 hash 已在 repair_history.json,这里聚合留痕。
        hash_chain: dict[str, Any] = {
            "base_plan_sha256": str(base_sha) if base_sha else None,
            "candidate_plan_sha256": state.get("assembly_candidate_plan_sha256"),
            "final_plan_sha256": trace.get("final_plan_sha256"),
            "repair_rounds": [],
        }
        history = _safe_read_json(state.get("assembly_repair_history_path"))
        rounds = history if isinstance(history, list) else (history.get("rounds") if isinstance(history, dict) else None)
        if isinstance(rounds, list):
            hash_chain["repair_rounds"] = [
                {
                    "round": r.get("round"),
                    "before": r.get("before_plan_sha256"),
                    "after": r.get("after_plan_sha256"),
                    "fingerprint": r.get("patch_fingerprint"),
                }
                for r in rounds
                if isinstance(r, dict)
            ]
        trace["hash_chain"] = hash_chain
        save_trace(trace_path, trace)
        return trace, str(trace_path), notes
    except Exception as exc:  # noqa: BLE001 — trace 是审计产物,失败不阻断提交
        notes.append(f"influence_trace 收尾失败(不阻断): {exc!r}")
        return None, None, notes


def _summarize_trace(trace: Any, final_status: str) -> str:
    if not isinstance(trace, dict):
        return f"- (influence_trace 未生成;最终计划状态:{final_status})"
    lines = ["- 节点贡献(artifact → consumed_by):"]
    for c in trace.get("node_contributions") or []:
        if not isinstance(c, dict):
            continue
        extra_note = ""
        extra = c.get("extra")
        if isinstance(extra, dict) and extra.get("executed") is False:
            extra_note = "(本轮未执行)"
        lines.append(
            f"  - `{c.get('node_id')}` → {c.get('artifact')}{extra_note}"
            f" → {', '.join(c.get('consumed_by') or [])}"
        )
    lines.append(f"- final_plan_sha256:{trace.get('final_plan_sha256') or '(未提交)'}")
    lines.append(
        "- mapped_draft_sha256:"
        f"{trace.get('mapped_draft_sha256') or '(待 generate_draft 消费后回写)'}"
    )
    return "\n".join(lines)


@timed_assembly_node("assembly_write_report")
def _emit_assembly_qc_summary(state: WorkflowState) -> None:
    """P2-4:经 LangGraph custom stream 上报 assembly QC 摘要事件。

    创建台交付卡(delivery_summary.mjs reduceDeliveryCard)按
    ``summary.event == "assembly.qc"`` 归并出 Assembly QC 行。观测通道,
    任何失败吞掉即可(图运行时外无 stream writer)。
    """
    try:
        candidate = state.get("assembly_candidate_plan")
        patch = state.get("assembly_draft_patch")
        history = _safe_read_json(state.get("assembly_repair_history_path"))
        rounds = history if isinstance(history, list) else (
            history.get("rounds") if isinstance(history, dict) else None
        )
        cand_qc = _safe_read_json(state.get("assembly_candidate_qc_report_path"))
        warning_count = sum(
            1
            for i in ((cand_qc or {}).get("issues") or [])
            if isinstance(i, dict) and i.get("severity") == "warning"
        )
        detail = {
            "event": "assembly.qc",
            "qc_status": state.get("assembly_qc_status"),
            "final_status": state.get("assembly_final_status")
            or (
                "submitted"
                if state.get("assembly_final_plan_path")
                else "not_available"
            ),
            "candidate_clip_count": len(candidate.get("clips") or [])
            if isinstance(candidate, dict)
            else None,
            "patch_ops": len(patch.get("operations") or [])
            if isinstance(patch, dict)
            else None,
            "repair_rounds": len(rounds) if isinstance(rounds, list) else 0,
            "retry_count": int(state.get("assembly_qc_retry_count") or 0),
            "warning_count": warning_count,
        }
        _emit_progress(detail)
    except Exception:  # noqa: BLE001 — 观测通道失败不阻断
        pass


def _emit_progress(detail: dict[str, Any]) -> None:
    """经 LangGraph custom stream 上报(同 node_05._emit_monitor_event 口径)。

    detail 整体透传 —— runner 归一为 node.progress 的 payload.summary,
    交付卡按 ``summary.event`` 判定,**event 键必须保留**。
    """
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if callable(writer):
            writer(
                {
                    "monitor_event": "progress",
                    "node_id": "assembly_write_report",
                    "detail": detail,
                }
            )
    except Exception:  # noqa: BLE001
        pass


@timed_assembly_node("assembly_write_report")
def assembly_write_report_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. 汇总输入 ----
    media_data = _safe_read_json(state.get("assembly_media_artifact"))
    qc_data = _safe_read_json(state.get("assembly_qc_report_path"))
    validation_data = _safe_read_json(state.get("assembly_timeline_validation_path"))
    timeline_path = state.get("assembly_timeline_path")
    preview_path = state.get("assembly_preview_path")
    qc_status = state.get("assembly_qc_status") or "unknown"
    retry_count = int(state.get("assembly_qc_retry_count") or 0)
    candidate_path = state.get("assembly_candidate_plan_path")
    patch = state.get("assembly_draft_patch")
    repair_history = _safe_read_json(state.get("assembly_repair_history_path"))

    # ---- 2. P1-7 最终计划提交 + 影响链收尾(绝不抛异常) ----
    try:
        submitted = _submit_final_plan(state, out_dir)
    except Exception as exc:  # noqa: BLE001 — 提交失败降级为 blocked,不崩
        submitted = {
            "final_plan": None,
            "final_plan_path": None,
            "final_plan_sha256": None,
            "final_status": "blocked",
            "final_notes": [f"提交过程异常(降级阻断): {exc!r}"],
        }
    final_status = submitted["final_status"]
    trace, trace_path_used, trace_notes = _finalize_influence_trace(
        state, out_dir, final_sha=submitted.get("final_plan_sha256")
    )
    final_notes = list(submitted.get("final_notes") or []) + trace_notes

    # ---- 3. 渲染 Markdown ----
    sections: list[str] = []
    sections.append("# Assembly QC 报告\n")
    sections.append("> 本次已生成组装质检报告,建议对照剪映草稿一并查看。")
    sections.append("> 最终编辑计划经本页第 7 节提交,由 generate_draft 生成正式草稿。\n")

    sections.append("## 1. 总览\n")
    sections.append(f"- QC 状态:`{qc_status}`")
    sections.append(f"- 修复循环重试次数:{retry_count}")
    sections.append(f"- timeline.json:`{timeline_path or '(未生成)'}`")
    sections.append(f"- preview.mp4:`{preview_path or '(未生成)'}`\n")

    sections.append("## 2. 素材清单 (media.json manifest)\n")
    sections.append(_summarize_media(media_data))
    sections.append("")

    sections.append("## 3. timeline 校验 (timeline_validation.json)\n")
    sections.append(_summarize_validation(validation_data))
    sections.append("")

    sections.append("## 4. timeline 内容\n")
    sections.append(_summarize_timeline(timeline_path))
    sections.append("")

    sections.append("## 5. 质检报告 (preview_qc_report.json)\n")
    sections.append(_summarize_qc(qc_data))
    sections.append("")

    sections.append("## 6. 候选编辑计划与修复 (candidate_plan.json / repair_history.json)\n")
    sections.append(
        _summarize_candidate_and_repair(candidate_path, patch, repair_history)
    )
    sections.append("")
    sections.append("### 6.1 LLM 调用诊断 (P2-1)\n")
    sections.append(_summarize_llm_diagnostics(state.get("assembly_llm_diagnostics")))
    sections.append("")
    sections.append("### 6.2 节点计时 (node_timing.json,P2-2)\n")
    sections.append(summarize_timing(out_dir / "node_timing.json"))
    sections.append("")

    sections.append("## 7. 最终计划提交 (P1-7)\n")
    status_icon = {"submitted": "✅ 已提交", "blocked": "⛔ 已阻断", "not_available": "— 无候选"}.get(
        final_status, final_status
    )
    sections.append(f"- 状态:**{status_icon}**")
    sections.append(f"- final_plan.json:`{submitted.get('final_plan_path') or '(未生成)'}`")
    sections.append(f"- final sha256:`{submitted.get('final_plan_sha256') or '(未提交)'}`")
    if final_status == "blocked":
        sections.append("- ⛔ 存在结构性错误,最终计划未提交;generate_draft 将回退原始 storyline_plan。")
    for note in final_notes:
        sections.append(f"  - {note}")
    sections.append("")

    sections.append("## 8. 影响链 (influence_trace.json)\n")
    sections.append(_summarize_trace(trace, final_status))
    sections.append("")

    sections.append(
        "---\n*本报告由 assembly_write_report 节点自动生成;关卡① 人工看到后建议把 "
        "`preview.mp4` 与 `剪映草稿` 一起过一遍。*"
    )

    out_path = out_dir / "report.md"
    out_path.write_text("\n".join(sections), encoding="utf-8")

    log_tags = ["assembly_write_report_done"]
    if final_status == "submitted":
        log_tags.append("assembly_final_plan_submitted")
    elif final_status == "blocked":
        log_tags.append("assembly_final_plan_blocked")

    out: dict[str, Any] = {
        "assembly_report_path": str(out_path),
        "assembly_influence_trace_path": trace_path_used or state.get(
            "assembly_influence_trace_path"
        ),
        "status_log": append_status_tag(state, *log_tags),
    }
    if final_status == "submitted":
        out["assembly_final_plan"] = submitted["final_plan"]
        out["assembly_final_plan_path"] = submitted["final_plan_path"]
        out["assembly_final_plan_sha256"] = submitted["final_plan_sha256"]
    # P2-4:交付卡 Assembly QC 行摘要(观测通道,失败静默)
    _emit_assembly_qc_summary(state)
    return out
