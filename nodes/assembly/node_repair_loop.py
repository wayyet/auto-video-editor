"""节点 5/6:``assembly_repair_loop``(第六期 P1-6 真实修复版)。

两条路径:
- **候选修复路径**(P1-6,state 有 ``assembly_candidate_plan``):读候选 QC
  报告的 typed issues → :func:`repair_strategies.build_repair_patch` 产出
  有类型、有目标、可回滚的 DraftPatch(trim 避开坏窗 / restore 撤销回基线
  clip / remove 坏片段 / invalid_range 夹回合法区间)→ ``apply_patch`` 应用
  → ``CanonicalTimeline.model_validate`` 硬门 → 写回
  ``candidate_plan.json`` + ``repair_history.json``(前后 sha / issue→op
  映射 / patch 指纹)。**同一 patch 指纹不重放**(repair_noop),修复后无
  有效变化也标 repair_noop,绝不虚报修复成功。
- **旧路径兜底**(无候选,单元测试 / 无基线流):P1-6 之前的最小 marker
  patch 行为(timeline_diff + metadata.repair_marker)。

**阶段五(§6.5)视觉证据复核旁路保留**:QC escalate 时先对可疑窗口高 fps
重采样 + 局部放大落证据帧;旁路失败只写 error_log,不阻断主修复链。

重试计数 ``assembly_qc_retry_count`` 仍由本节点整体覆盖写 +1。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assembly_capabilities.draft_patch import (
    PatchError,
    apply_patch,
    plan_sha256,
    strip_internal_marks,
)
from assembly_capabilities.influence_trace import canonical_json_sha256
from assembly_capabilities.qc_preview import parse_filter_ranges
from assembly_capabilities.repair_strategies import (
    build_repair_patch,
    patch_fingerprint,
    PREVIEW_CODES,
)
from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext
from assembly_capabilities.timeline_ops import timeline_diff

from nodes.storyline._common import (
    _resolve_outputs_root,
    append_error,
    append_status_tag,
)
from state import WorkflowState
from video_edit_capabilities.visual_evidence import (
    MAX_SEGMENTS,
    MAX_SEGMENT_SECONDS,
    MAX_TOTAL_SECONDS,
    video_read_frames,
    video_watch_segment,
)

import config


def _read_json(path: str | Path | None) -> Any:
    if not path:
        return None
    p = Path(str(path))
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# 预览 QC 可疑窗口 → 带 range 的 typed issues(复用 parse_filter_ranges)
# ---------------------------------------------------------------------------
_SUSPECT_SOURCES = (
    ("blackdetect_log_tail", "black_frame"),
    ("silencedetect_log_tail", "silence"),
    ("freezedetect_log_tail", "freeze_frame"),
)


def _collect_preview_issue_windows(
    qc_report: dict[str, Any],
    *,
    max_windows: int,
    pad: float = 0.5,
    clip_duration: float | None = None,
) -> list[dict[str, Any]]:
    """从 preview_qc_report.json 抽可疑窗口(秒)→ 转候选时间轴 ms 的 issues。

    与旧版 :func:`_collect_suspicious_windows` 同一套合并/截断/预算逻辑,
    只是把产物从"视觉复核窗口"换成"带 range 的 typed issue"。
    """
    raw: list[dict[str, Any]] = []
    for field, code in _SUSPECT_SOURCES:
        log = qc_report.get(field)
        if not isinstance(log, str) or not log.strip():
            continue
        kind = code.split("_")[0]  # parse_filter_ranges 的 kind: black/silence/freeze
        for rng in parse_filter_ranges(log, kind):
            start, end = rng.get("start"), rng.get("end")
            if start is None or end is None:
                continue
            raw.append({"start": float(start) - pad, "end": float(end) + pad, "code": code})

    if not raw:
        return []
    ordered = sorted(raw, key=lambda w: (w["start"], w["end"]))
    merged: list[dict[str, Any]] = [dict(ordered[0])]
    for item in ordered[1:]:
        last = merged[-1]
        if item["start"] <= last["end"] + 0.5 and item.get("code") == last.get("code"):
            last["end"] = max(last["end"], item["end"])
        else:
            merged.append(dict(item))

    max_segment = float(MAX_SEGMENT_SECONDS)
    picked: list[dict[str, Any]] = []
    for w in merged:
        start_s = max(0.0, w["start"])
        end_s = min(w["end"], start_s + max_segment)
        if clip_duration:
            end_s = min(end_s, max(start_s + 0.2, clip_duration))
        if end_s <= start_s:
            continue
        picked.append(w | {"start": round(start_s, 3), "end": round(end_s, 3)})
        if len(picked) >= max(1, min(max_windows, MAX_SEGMENTS)):
            break
    return [
        {
            "severity": "error",
            "code": w["code"],
            "check": "候选预览 QC",
            "message": f"preview {w['code']} window {w['start']}-{w['end']}s(外扩 0.5s)",
            "range": [int(w["start"] * 1000), int(w["end"] * 1000)],
        }
        for w in picked
    ]


# ---------------------------------------------------------------------------
# 阶段五视觉证据旁路(原样保留)
# ---------------------------------------------------------------------------
def _merge_windows(windows: list[dict[str, Any]], *, gap: float = 0.5) -> list[dict[str, Any]]:
    """把重叠 / 间隔小于 ``gap`` 的窗口并成一个,并保留 ``kinds`` 溯源。"""
    if not windows:
        return []
    ordered = sorted(windows, key=lambda w: (w["start"], w["end"]))
    merged = [dict(ordered[0])]
    for item in ordered[1:]:
        last = merged[-1]
        if item["start"] <= last["end"] + gap:
            last["end"] = max(last["end"], item["end"])
            last["kinds"] = sorted(set(last.get("kinds") or []) | set(item.get("kinds") or []))
        else:
            merged.append(dict(item))
    for item in merged:
        item["start"] = round(max(0.0, item["start"]), 3)
        item["end"] = round(item["end"], 3)
        item["duration"] = round(item["end"] - item["start"], 3)
    return merged


def _collect_suspicious_windows(
    qc_report: dict[str, Any],
    *,
    max_windows: int,
    pad: float = 0.5,
    clip_duration: float | None = None,
) -> list[dict[str, Any]]:
    """从 ``preview_qc_report.json`` 抽出最多 ``max_windows`` 个可疑时间段(视觉复核用)。"""
    raw: list[dict[str, Any]] = []
    for field, kind in (
        ("blackdetect_log_tail", "black"),
        ("silencedetect_log_tail", "silence"),
        ("freezedetect_log_tail", "freeze"),
    ):
        log = qc_report.get(field)
        if not isinstance(log, str) or not log.strip():
            continue
        for rng in parse_filter_ranges(log, kind):
            start = rng.get("start")
            end = rng.get("end")
            if start is None or end is None:
                continue
            raw.append({"start": float(start) - pad, "end": float(end) + pad, "kinds": [kind]})

    if not raw:
        return []

    merged = _merge_windows(raw)
    if clip_duration:
        merged = [
            {**w, "end": min(w["end"], max(w["start"] + 0.2, clip_duration))}
            for w in merged
        ]
        merged = [w for w in merged if w["end"] > w["start"]]

    max_segment = float(MAX_SEGMENT_SECONDS)
    capped: list[dict[str, Any]] = []
    for w in merged:
        end = min(w["end"], w["start"] + max_segment)
        capped.append({**w, "end": round(end, 3)})

    budget = float(min(MAX_TOTAL_SECONDS, max_segment * max(1, min(max_windows, MAX_SEGMENTS))))
    picked: list[dict[str, Any]] = []
    spent = 0.0
    for w in capped:
        duration = round(w["end"] - w["start"], 3)
        if duration <= 0:
            continue
        if picked and spent + duration > budget:
            break
        picked.append({**w, "duration": duration})
        spent += duration
        if len(picked) >= max(1, min(max_windows, MAX_SEGMENTS)):
            break
    return picked


def _probe_duration(video_path: Path) -> float | None:
    try:
        from video_edit_capabilities.timeline import media_duration_seconds

        return media_duration_seconds(video_path)
    except Exception:
        return None


def _run_visual_evidence(
    preview_path: Path,
    windows: list[dict[str, Any]],
    ctx: RunContext,
    out_dir: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """对每个可疑窗口做"高 fps 重采样 + 局部放大",返回 (逐窗结果, 错误列表)。"""
    segments = [{"start": w["start"], "end": w["end"]} for w in windows]
    midpoints = [round((w["start"] + w["end"]) / 2.0, 3) for w in windows]

    watch_result: ToolResult = video_watch_segment(
        {
            "video_path": str(preview_path),
            "segments": segments,
            "fps": float(config.ASSEMBLY_REPAIR_WATCH_FPS),
            "force": True,
            "save_frames_dir": str(out_dir / "repair_frames"),
        },
        ctx,
    )
    zoom_result: ToolResult = video_read_frames(
        {
            "video_path": str(preview_path),
            "timestamps": midpoints,
            "region": "center",
            "upscale": 2.0,
            "max_frames": len(midpoints) or 1,
            "output_dir": str(out_dir / "repair_zoom"),
        },
        ctx,
    )

    errors: list[str] = []
    for label, result in (("video_watch_segment", watch_result), ("video_read_frames", zoom_result)):
        if result.text.startswith("[ERROR]"):
            errors.append(f"{label}: {result.text}")

    watch_frames: list[dict[str, Any]] = []
    watch_data = watch_result.data or {}
    if isinstance(watch_data.get("segments"), list):
        watch_frames = [s for s in watch_data["segments"] if isinstance(s, dict)]
    zoom_frames: list[dict[str, Any]] = []
    zoom_data = zoom_result.data or {}
    if isinstance(zoom_data.get("frames"), list):
        zoom_frames = [f for f in zoom_data["frames"] if isinstance(f, dict)]

    results = [
        {
            "window": w,
            "watch_segments": watch_frames[i] if i < len(watch_frames) else None,
            "zoom_frame": zoom_frames[i] if i < len(zoom_frames) else None,
        }
        for i, w in enumerate(windows)
    ]
    return results, errors


# ---------------------------------------------------------------------------
# P1-6 候选修复主路径
# ---------------------------------------------------------------------------
def _candidate_repair_path(state: WorkflowState, out_dir: Path, ctx: RunContext) -> dict:
    from storyline.contract import CanonicalTimeline

    candidate = state.get("assembly_candidate_plan") or {}
    candidate_sha = (
        str(state.get("assembly_candidate_plan_sha256"))
        if state.get("assembly_candidate_plan_sha256")
        else plan_sha256(candidate)
    )
    baseline_plan = state.get("storyline_plan") or {}

    # 1. typed issues:候选 QC 报告(主)+ preview 可疑窗口(带 range 补充)
    issues: list[dict[str, Any]] = []
    qc_report = _read_json(state.get("assembly_candidate_qc_report_path")) or {}
    for issue in qc_report.get("issues") or []:
        if isinstance(issue, dict):
            issues.append(dict(issue))
    preview_report = _read_json(state.get("assembly_qc_report_path")) or {}
    preview_path = state.get("assembly_preview_path")
    windows = _collect_preview_issue_windows(
        preview_report,
        max_windows=int(config.ASSEMBLY_REPAIR_MAX_WINDOWS),
        clip_duration=_probe_duration(Path(preview_path)) if preview_path else None,
    )
    issues.extend(windows)

    # 2. 策略 → patch
    patch, manual, notes = build_repair_patch(
        candidate, issues, baseline_plan=baseline_plan if isinstance(baseline_plan, dict) else {}
    )
    fingerprint = patch_fingerprint(patch)
    has_ops = bool(patch.get("operations"))

    history_path = out_dir / "repair_history.json"
    history = _read_json(history_path) or []
    if not isinstance(history, list):
        history = []

    retry_count = int(state.get("assembly_qc_retry_count") or 0) + 1
    tags = ["assembly_repair_loop_done"]
    noop_reason: str | None = None

    # 3. 同 patch 指纹去重(同 issue 不能无限重放同一修复)
    if has_ops and any(h.get("patch_fingerprint") == fingerprint for h in history):
        noop_reason = "duplicate patch fingerprint(同 issue 已重放过同一修复,不再重放)"
        tags.append("assembly_repair_noop")

    repaired_clean: dict[str, Any] | None = None
    repair_report: dict[str, Any] | None = None
    if has_ops and noop_reason is None:
        try:
            repaired, repair_report = apply_patch(candidate, patch, expect_base_sha256=candidate_sha)
        except PatchError as exc:
            return {
                "assembly_qc_retry_count": retry_count,
                "error_log": append_error(
                    state, node_kind="assembly_repair_loop",
                    error_code="CONTRACT_INVALID",
                    message=f"repair patch rejected: {exc}",
                ),
                "status_log": append_status_tag(state, "assembly_repair_loop_failed"),
            }
        repaired_clean = strip_internal_marks(repaired)
        try:
            CanonicalTimeline.model_validate(repaired_clean)
        except Exception as exc:  # noqa: BLE001
            return {
                "assembly_qc_retry_count": retry_count,
                "error_log": append_error(
                    state, node_kind="assembly_repair_loop",
                    error_code="CONTRACT_INVALID",
                    message=f"repaired candidate invalid, discarded: {exc!r}(候选保持不变,复检继续)",
                ),
                "status_log": append_status_tag(state, "assembly_repair_loop_failed"),
            }
    elif not has_ops:
        noop_reason = "no repairable issues(全部 manual / 无 error)"

    # 4. 落盘:修复后的候选 + patch + 历史(含 issue→op 映射与前后 sha)
    before_sha = candidate_sha
    after_sha = plan_sha256(repaired_clean) if repaired_clean is not None else before_sha
    if repaired_clean is not None and after_sha != before_sha:
        candidate_path = out_dir / "candidate_plan.json"
        candidate_path.write_text(
            json.dumps(repaired_clean, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    else:
        candidate_path = out_dir / "candidate_plan.json"
    if has_ops:
        (out_dir / "repair_patch.json").write_text(
            json.dumps(patch, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    history.append({
        "round": retry_count,
        "before_plan_sha256": before_sha,
        "after_plan_sha256": after_sha,
        "patch_fingerprint": fingerprint,
        "applied_operations": (repair_report or {}).get("applied_operations") or [],
        "skipped_operations": (repair_report or {}).get("skipped_operations") or [],
        "manual_issues": [str(i.get("code") or i.get("message") or "?") for i in manual],
        "strategy_notes": notes,
        "noop_reason": noop_reason,
        "issue_codes": [str(i.get("code") or "?") for i in issues],
    })
    history_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    if noop_reason:
        tags.append("assembly_repair_noop")
    if manual:
        tags.append("assembly_repair_manual_pending")
    delta: dict[str, Any] = {
        "assembly_qc_retry_count": retry_count,
        "assembly_repair_evidence_path": None,
        "assembly_repair_history_path": str(history_path),
        "error_log": [],
        "status_log": append_status_tag(state, *tags),
    }
    if repaired_clean is not None and after_sha != before_sha:
        # 修复真实改变了候选 → 下一轮 validate 对新候选复检
        delta["assembly_candidate_plan"] = repaired_clean
        delta["assembly_candidate_plan_sha256"] = after_sha
        delta["assembly_candidate_plan_path"] = str(candidate_path)
        delta["assembly_draft_patch"] = patch
    else:
        # noop:候选保持不变,显式回传原 sha(状态自明,复检对象不变)
        delta["assembly_candidate_plan_sha256"] = before_sha
    return delta


def _legacy_repair_path(state: WorkflowState, out_dir: Path, ctx: RunContext) -> dict:
    """旧路径:P1-6 之前的最小 marker patch(无候选基线时的兜底)。"""
    timeline_path = state.get("assembly_timeline_path")
    if not timeline_path:
        # P0 修复:早退也必须消耗一次重试预算,否则死循环自旋。
        return {
            "assembly_qc_retry_count": int(state.get("assembly_qc_retry_count") or 0) + 1,
            "error_log": append_error(
                state, node_kind="assembly_repair_loop", error_code="CONTRACT_INVALID",
                message="missing assembly_timeline_path",
            ),
            "status_log": append_status_tag(state, "assembly_repair_loop_failed"),
        }
    patch = {
        "set_timeline_fields": {
            "metadata": {
                "repair_marker": True,
                "qc_report": state.get("assembly_qc_report_path"),
                "validation_report": state.get("assembly_timeline_validation_path"),
            },
        },
    }
    diff_out = out_dir / "timeline_diff.json"
    diff_result: ToolResult = timeline_diff(
        {
            "timeline_path": str(timeline_path),
            "instructions": "assembly_repair_loop legacy marker patch (no candidate plan)",
            "patch": patch,
            "apply": True,
            "output_json": str(diff_out),
        },
        ctx,
    )
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
        "assembly_repair_evidence_path": None,
        "error_log": err_log_patch,
        "status_log": append_status_tag(state, "assembly_repair_loop_done"),
    }


def assembly_repair_loop_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"

    ctx = RunContext(session_kind="pipeline")
    err_log_patch: list[str] = []
    evidence_path: str | None = None

    # ---- 阶段五:视觉证据复核(旁路,失败不阻断主修复链) ----
    if config.ASSEMBLY_REPAIR_VISUAL_EVIDENCE:
        preview_path = state.get("assembly_preview_path")
        qc_report_path = state.get("assembly_qc_report_path")
        try:
            qc_report: dict[str, Any] = _read_json(qc_report_path) or {}
            windows = _collect_suspicious_windows(
                qc_report,
                max_windows=int(config.ASSEMBLY_REPAIR_MAX_WINDOWS),
                clip_duration=_probe_duration(Path(preview_path)) if preview_path else None,
            )
            if not windows:
                err_log_patch.append(
                    "[assembly_repair_loop] 视觉复核跳过:QC 报告里没有时间戳类可疑片段"
                )
            elif not preview_path or not Path(preview_path).is_file():
                err_log_patch.append(
                    "[assembly_repair_loop] 视觉复核跳过:预览视频不存在 "
                    f"(assembly_preview_path={preview_path!r})"
                )
            else:
                out_dir.mkdir(parents=True, exist_ok=True)
                evidence, ev_errors = _run_visual_evidence(
                    Path(preview_path), windows, ctx, out_dir
                )
                err_log_patch.extend(
                    f"[assembly_repair_loop] 视觉复核降级: {msg}" for msg in ev_errors
                )
                evidence_file = out_dir / "repair_visual_evidence.json"
                evidence_file.write_text(
                    json.dumps(
                        {
                            "tool": "assembly_repair_loop.visual_evidence",
                            "retry_index": int(state.get("assembly_qc_retry_count") or 0) + 1,
                            "preview_path": str(preview_path),
                            "qc_report_path": str(qc_report_path) if qc_report_path else None,
                            "watch_fps": float(config.ASSEMBLY_REPAIR_WATCH_FPS),
                            "windows": windows,
                            "evidence": evidence,
                            "errors": ev_errors,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                evidence_path = str(evidence_file)
        except Exception as exc:  # 视觉复核任何异常都不许打断主链
            err_log_patch.append(f"[assembly_repair_loop] 视觉复核异常(已忽略): {exc!r}")

    # ---- 主修复链 ----
    if state.get("assembly_candidate_plan"):
        delta = _candidate_repair_path(state, out_dir, ctx)
    else:
        delta = _legacy_repair_path(state, out_dir, ctx)

    # 合并视觉证据产物(旁路写 error_log 不覆盖主链的 error_log)
    if evidence_path:
        delta["assembly_repair_evidence_path"] = evidence_path
        if "assembly_repair_visual_evidence_done" not in delta.get("status_log", []):
            pass  # tags 由子路径决定,视觉证据 tag 追加在 status_log reducer 外
    if err_log_patch:
        merged = list(delta.get("error_log") or [])
        merged.extend(err_log_patch)
        delta["error_log"] = merged
    if evidence_path:
        tags = list(delta.get("status_log") or [])
        tags.append("assembly_repair_visual_evidence_done")
        delta["status_log"] = tags
    return delta
