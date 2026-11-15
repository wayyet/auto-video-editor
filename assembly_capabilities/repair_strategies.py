"""QC typed issues → 修复策略(P1-6,第六期设计文档 §3.5)。

设计纪律:
- **有类型、有目标、有前后差异**:每个策略产出明确的 DraftPatch operations
  (目标 clip / 源区间 / 原因 / 证据引用),绝不只写 metadata 标记;
- **有限、可解释、可回滚**:优先局部修复(trim);无法安全修复 → 撤销该
  clip 的候选修改(restore 回基线 clip);两者都不可行 → 标 manual 人工项,
  **不得伪造修复成功**;
- **结构性错误不得软降级**:broken_reference / missing_asset / invalid_range /
  missing_segment / extra_segment / track_desync 属结构性错误(节点层据此
  fail-closed);black_frame / freeze_frame / silence 属预览级问题(可修复);
- patch 绑定**候选计划**的 sha(修复在候选之上迭代,基线 storyline_plan
  仍是最终回退锚点)。
"""

from __future__ import annotations

from typing import Any

from .draft_patch import add_operation, new_patch, plan_sha256

#: 结构性错误码(QC 达到重试上限仍有这类 error → fail-closed,不进关卡①)
STRUCTURAL_CODES = {
    "broken_reference",
    "missing_asset",
    "invalid_range",
    "missing_segment",
    "extra_segment",
    "track_desync",
}

#: 预览级问题码(可由 trim/remove/撤销修复)
PREVIEW_CODES = {"black_frame", "freeze_frame", "silence"}

#: 预览坏窗覆盖率超过此值的 clip 直接移除(残段不值得保留)
REMOVE_COVERAGE_THRESHOLD = 0.8


def has_structural_error(issues: list[dict[str, Any]]) -> bool:
    """issues 里是否存在结构性 error(P1-6 fail-closed 判据)。"""
    return any(
        isinstance(i, dict)
        and i.get("severity") == "error"
        and i.get("code") in STRUCTURAL_CODES
        for i in issues or []
    )


def _baseline_clip_map(baseline_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(c.get("clip_id")): c
        for c in (baseline_plan.get("clips") or [])
        if isinstance(c, dict)
    }


def _media_duration_map(baseline_plan: dict[str, Any]) -> dict[str, int]:
    return {
        str(m.get("media_id")): int(m.get("duration_ms") or 0)
        for m in (baseline_plan.get("source_media") or [])
        if isinstance(m, dict)
    }


def _clip_by_id(candidate_plan: dict[str, Any], clip_id: str) -> dict[str, Any] | None:
    for c in candidate_plan.get("clips") or []:
        if isinstance(c, dict) and str(c.get("clip_id")) == clip_id:
            return c
    return None


def _clamp_range(
    in_ms: int, out_ms: int, duration_ms: int
) -> tuple[int, int] | None:
    """把源区间夹回 [0, duration] 且保证非空;无法给出有效区间返回 None。"""
    lo = max(0, min(int(in_ms), duration_ms))
    hi = max(0, min(int(out_ms), duration_ms))
    if hi <= lo:
        return None
    return lo, hi


def _issue_range_ms(issue: dict[str, Any]) -> tuple[int, int] | None:
    rng = issue.get("range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        try:
            return int(rng[0]), int(rng[1])
        except (TypeError, ValueError):
            return None
    return None


def _trim_candidate_clip_op(
    patch: dict[str, Any],
    clip: dict[str, Any],
    *,
    new_in_ms: int,
    new_out_ms: int,
    reason: str,
    evidence: list[str],
    confidence: float = 0.85,
) -> None:
    add_operation(
        patch,
        operation="trim",
        target_clip_id=str(clip.get("clip_id")),
        source_in_ms=int(new_in_ms),
        source_out_ms=int(new_out_ms),
        reason=reason,
        evidence_refs=evidence,
        confidence=confidence,
    )


def build_repair_patch(
    candidate_plan: dict[str, Any],
    issues: list[dict[str, Any]],
    *,
    baseline_plan: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """把 QC typed issues 转成修复 patch。

    Returns:
        (patch, manual_issues, notes):
        - patch: 绑定候选 sha 的 DraftPatch(可能为空 —— 全部 manual 时);
        - manual_issues: 无法安全自动修复、保留给人工的 issue 原文;
        - notes: 策略决策的说明(供 repair_history / report 消费)。
    """
    patch = new_patch(plan_sha256(candidate_plan), created_by="assembly_repair_loop")
    manual: list[dict[str, Any]] = []
    notes: list[str] = []
    handled_clips: set[str] = set()
    baseline_by_id = _baseline_clip_map(baseline_plan)
    durations = _media_duration_map(baseline_plan)

    def _revert_clip(clip_id: str, why: str, evidence: list[str]) -> bool:
        """撤销候选对某 clip 的修改:恢复基线 clip;无基线 → 移除候选新增 clip。"""
        if clip_id in handled_clips:
            return True
        base = baseline_by_id.get(clip_id)
        if base is not None:
            add_operation(
                patch, operation="restore", target_clip_id=clip_id,
                reason=why, evidence_refs=evidence,
                extra={"restore_clip": dict(base)},
                confidence=0.95,
            )
            handled_clips.add(clip_id)
            notes.append(f"clip {clip_id}: {why} → restore 基线 clip")
            return True
        if _clip_by_id(candidate_plan, clip_id) is not None:
            add_operation(
                patch, operation="remove", target_clip_id=clip_id,
                reason=why, evidence_refs=evidence,
                confidence=0.95,
            )
            handled_clips.add(clip_id)
            notes.append(f"clip {clip_id}: {why} → remove(无基线可恢复)")
            return True
        notes.append(f"clip {clip_id}: {why} → 目标不存在,跳过")
        return False

    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        code = str(issue.get("code") or "")
        severity = str(issue.get("severity") or "")
        clip_id = issue.get("clip_id")
        evidence = [str(r) for r in issue.get("evidence_refs") or []]

        if severity != "error":
            # warning 不进修复(如实保留在 QC 报告)
            continue

        if code in ("broken_reference", "missing_asset", "missing_segment", "extra_segment"):
            if not clip_id:
                manual.append(dict(issue, manual_reason="无法定位 clip"))
                continue
            _revert_clip(
                str(clip_id),
                f"{code}: {'引用/段错位,恢复基线 clip'}",
                evidence or [issue.get("message", "")],
            )
            continue

        if code == "track_desync":
            ids = issue.get("clip_ids") or ([clip_id] if clip_id else [])
            for cid in ids:
                _revert_clip(
                    str(cid),
                    "track_desync: 关联轨道与候选 clips 失配,恢复基线 clip",
                    evidence or [issue.get("message", "")],
                )
            if not ids:
                manual.append(dict(issue, manual_reason="track_desync 无 clip 定位"))
            continue

        if code == "invalid_range":
            if not clip_id:
                manual.append(dict(issue, manual_reason="无法定位 clip"))
                continue
            cid = str(clip_id)
            if cid in handled_clips:
                continue
            clip = _clip_by_id(candidate_plan, cid)
            if clip is None:
                manual.append(dict(issue, manual_reason="目标 clip 不在候选中"))
                continue
            media_id = str(clip.get("source_media_id"))
            duration = durations.get(media_id, 0)
            clamped = _clamp_range(
                int(clip.get("source_in_ms") or 0),
                int(clip.get("source_out_ms") or 0),
                duration,
            )
            if clamped is None:
                _revert_clip(cid, "invalid_range: 源区间无法夹回素材时长,恢复基线", evidence)
                continue
            _trim_candidate_clip_op(
                patch, clip,
                new_in_ms=clamped[0], new_out_ms=clamped[1],
                reason=f"invalid_range: 源区间夹回素材时长 {duration}ms",
                evidence=evidence or [issue.get("message", "")],
            )
            handled_clips.add(cid)
            notes.append(f"clip {cid}: invalid_range → trim 夹回合法区间")
            continue

        if code in PREVIEW_CODES:
            # 预览级问题:issue 必须带候选时间轴上的 range(ms)(由节点层把
            # qc_preview 的可疑窗口映射成 range);无 range → 人工
            rng = _issue_range_ms(issue)
            if rng is None:
                manual.append(dict(issue, manual_reason=f"{code} 无时间区间定位"))
                continue
            start_ms, end_ms = rng
            target: dict[str, Any] | None = None
            best_overlap = 0.0
            for clip in candidate_plan.get("clips") or []:
                if not isinstance(clip, dict):
                    continue
                lo = max(start_ms, int(clip["timeline_in_ms"]))
                hi = min(end_ms, int(clip["timeline_out_ms"]))
                overlap = max(0, hi - lo)
                if overlap > best_overlap:
                    best_overlap = overlap
                    target = clip
            if target is None or best_overlap <= 0:
                manual.append(dict(issue, manual_reason=f"{code} 窗口不落在任何候选 clip 上"))
                continue
            cid = str(target["clip_id"])
            span = int(target["timeline_out_ms"]) - int(target["timeline_in_ms"])
            coverage = best_overlap / max(1, span)
            if coverage >= REMOVE_COVERAGE_THRESHOLD or span - best_overlap < 500:
                # 预览级坏内容:直接移除坏片段(不是"撤销候选修改"——内容本身坏,
                # restore 基线只会带回同样的坏画面)
                if cid in handled_clips:
                    continue
                add_operation(
                    patch, operation="remove", target_clip_id=cid,
                    reason=f"{code}: 坏窗覆盖 {coverage:.0%}(或剩余不足 0.5s),移除坏片段",
                    evidence_refs=evidence or [issue.get("message", "")],
                    confidence=0.9,
                )
                handled_clips.add(cid)
                notes.append(f"clip {cid}: {code} → remove 坏片段")
                continue
            # 局部修复:坏窗把 clip 时间轴切成头/尾两段干净区(候选 clip 速度
            # 恒 1.0,源偏移 = 时间轴偏移)。保留较长一侧并映射回源区间。
            head_span = max(0, start_ms - int(target["timeline_in_ms"]))
            tail_span = max(0, int(target["timeline_out_ms"]) - end_ms)
            if tail_span >= head_span:
                keep_in = int(target["source_in_ms"]) + (end_ms - int(target["timeline_in_ms"]))
                keep_out = int(target["source_out_ms"])
            else:
                keep_in = int(target["source_in_ms"])
                keep_out = int(target["source_in_ms"]) + head_span
            if keep_out - keep_in < 500:
                if cid not in handled_clips:
                    add_operation(
                        patch, operation="remove", target_clip_id=cid,
                        reason=f"{code}: 干净部分过短(<0.5s),移除坏片段",
                        evidence_refs=evidence or [issue.get("message", "")],
                        confidence=0.9,
                    )
                    handled_clips.add(cid)
                    notes.append(f"clip {cid}: {code} → remove 坏片段")
                continue
            _trim_candidate_clip_op(
                patch, target,
                new_in_ms=keep_in, new_out_ms=keep_out,
                reason=f"{code}: 裁剪避开坏窗 [{start_ms},{end_ms}]ms(覆盖率 {coverage:.0%})",
                evidence=evidence or [issue.get("message", "")],
            )
            handled_clips.add(cid)
            notes.append(f"clip {cid}: {code} → trim 避开坏窗")
            continue

        manual.append(dict(issue, manual_reason=f"无修复策略的 issue 类型: {code or 'unknown'}"))

    return patch, manual, notes


def patch_fingerprint(patch: dict[str, Any]) -> str:
    """修复 patch 的去重指纹(同 patch 重放检测)。"""
    return plan_sha256(patch.get("operations") or [])


__all__ = [
    "STRUCTURAL_CODES",
    "PREVIEW_CODES",
    "REMOVE_COVERAGE_THRESHOLD",
    "has_structural_error",
    "build_repair_patch",
    "patch_fingerprint",
]
