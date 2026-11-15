"""DraftPatch:针对 CanonicalTimeline(原始 storyline_plan)的编辑补丁。

第六期 P1-4(设计文档 §2.3-C / §3.3):``assembly_build_timeline`` 不再产出
独立无关的 assembly timeline,而是生成"会改变最终剪映视频轨"的候选编辑计划:

- base = 原始 ``storyline_plan``(CanonicalTimeline dict,整数毫秒),**不可变**
  —— 只深拷贝,永不覆盖;
- patch 绑定 ``base_plan_sha256``(:func:`influence_trace.canonical_json_sha256`
  统一口径),基线漂移时拒绝应用(防旧 patch 套新计划);
- operation 类型:``select`` / ``trim`` / ``reorder`` / ``remove``,每个 op 必有
  目标 clip、源素材引用、原因与证据引用(P1-3 视觉证据透传);
- 候选只来自 plan 已有 ``source_media`` —— 不虚构素材引用;
- ``track_alignment_policy = "preserve_and_remap"``:op 应用后统一重排
  timeline 位置(顺序拼接),字幕/配音/BGM 等关联轨由 mapper 从 clips 重推导
  (mapper.py 的配音组区间与 BGM 终点都是 clips 推导,自动跟随);
- 幂等:同一 patch 对同一 base 重复 apply,结果逐字节一致;
- 最终合法性由 ``CanonicalTimeline.model_validate`` 作硬门(速度护栏 /
  单调 / 引用闭合),apply 层再做源区间不溢出的前置校验。
"""

from __future__ import annotations

import copy
from typing import Any

from .influence_trace import canonical_json_sha256

PATCH_SCHEMA_VERSION = 1

OPERATION_TYPES = {"select", "trim", "reorder", "remove", "restore"}
TRACK_ALIGNMENT_POLICY = "preserve_and_remap"


class PatchError(Exception):
    """patch 应用失败(基线漂移 / 非法 operation / 源溢出)。"""


# ---------------------------------------------------------------------------
# 构造
# ---------------------------------------------------------------------------
def new_patch(base_plan_sha256: str, *, created_by: str) -> dict[str, Any]:
    """建一个空 patch,绑定基线 hash。"""
    return {
        "schema_version": PATCH_SCHEMA_VERSION,
        "base_plan_sha256": base_plan_sha256,
        "operations": [],
        "track_alignment_policy": TRACK_ALIGNMENT_POLICY,
        "created_by": created_by,
    }


def add_operation(
    patch: dict[str, Any],
    *,
    operation: str,
    reason: str,
    evidence_refs: list[str] | None = None,
    target_clip_id: str | None = None,
    source_media_id: str | None = None,
    source_in_ms: int | None = None,
    source_out_ms: int | None = None,
    new_order: int | None = None,
    confidence: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """追加一条 operation,返回该 op(含确定性 operation_id)。

    operation_id 按 (patch 内序号, operation, target) 派生,同一 patch 重复
    构造得到同一 id(幂等前提)。
    """
    if operation not in OPERATION_TYPES:
        raise PatchError(f"unknown operation type: {operation!r}")
    ops = patch.setdefault("operations", [])
    op: dict[str, Any] = {
        "operation_id": f"op_{len(ops) + 1:04d}",
        "operation": operation,
        "target_clip_id": target_clip_id,
        "source_media_id": source_media_id,
        "source_in_ms": source_in_ms,
        "source_out_ms": source_out_ms,
        "new_order": new_order,
        "reason": str(reason or ""),
        "evidence_refs": [str(r) for r in (evidence_refs or [])],
        "confidence": confidence,
        "created_by": patch.get("created_by"),
    }
    if extra:
        op["extra"] = extra
    ops.append(op)
    return op


def plan_sha256(plan_dict: dict[str, Any]) -> str:
    """CanonicalTimeline plan 的规范化 hash(P1-0 统一口径)。"""
    return canonical_json_sha256(plan_dict)


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
def _clip_span(clip: dict[str, Any]) -> int:
    return int(clip["timeline_out_ms"]) - int(clip["timeline_in_ms"])


def _relayout(clips: list[dict[str, Any]]) -> None:
    """preserve_and_remap:按当前列表顺序顺序拼接,重写 timeline_in/out。

    未被 trim/select 触碰的 clip 保持原时长(尊重原计划里可能存在的变速);
    被 trim/select 重写过 source 区间的 clip,时长 = 新源跨度(速度恒 1.0,
    见 _apply_one 的 trim/select 分支)。重排只改时间落位,不改任何 clip 的
    身份、voiceover_id、转场语义与字幕文本。
    """
    cursor = 0
    for clip in clips:
        span = _clip_span(clip)
        clip["timeline_in_ms"] = cursor
        clip["timeline_out_ms"] = cursor + span
        cursor += span


def _check_source_within_media(
    plan: dict[str, Any], clip: dict[str, Any]
) -> None:
    media_by_id = {
        str(m.get("media_id")): m
        for m in plan.get("source_media") or []
        if isinstance(m, dict)
    }
    media = media_by_id.get(str(clip.get("source_media_id")))
    if media is None:
        raise PatchError(
            f"clip {clip.get('clip_id')!r}: source_media_id "
            f"{clip.get('source_media_id')!r} 不在 source_media 中"
        )
    duration = int(media.get("duration_ms") or 0)
    if int(clip.get("source_out_ms")) > duration:
        raise PatchError(
            f"clip {clip.get('clip_id')!r}: source_out_ms {clip['source_out_ms']} "
            f"溢出 media {media.get('media_id')!r} duration_ms={duration}"
        )


def _apply_one(
    plan: dict[str, Any], clips: list[dict[str, Any]], op: dict[str, Any], op_index: int
) -> str:
    """应用单条 op,返回 "applied" / "skipped:<原因>"(跳过不视为失败)。"""
    operation = op.get("operation")
    target_id = op.get("target_clip_id")
    index_by_id = {str(c.get("clip_id")): i for i, c in enumerate(clips)}
    evidence = list(op.get("evidence_refs") or [])

    if operation == "remove":
        i = index_by_id.get(str(target_id))
        if i is None:
            return f"skipped:target clip {target_id!r} not found"
        clips.pop(i)
        return "applied"

    if operation == "trim":
        i = index_by_id.get(str(target_id))
        if i is None:
            return f"skipped:target clip {target_id!r} not found"
        clip = clips[i]
        new_in = op.get("source_in_ms")
        new_out = op.get("source_out_ms")
        if new_in is None or new_out is None:
            return "skipped:trim requires source_in_ms/source_out_ms"
        new_in, new_out = int(new_in), int(new_out)
        if new_out <= new_in:
            return f"skipped:trim range empty ({new_in}->{new_out})"
        clip["source_in_ms"] = new_in
        clip["source_out_ms"] = new_out
        # trim 恒速度 1.0:时间轴跨度 = 新源跨度(重排阶段统一落位)
        clip["timeline_out_ms"] = clip["timeline_in_ms"] + (new_out - new_in)
        # 证据引用并回 clip,保证 diff 可追溯
        merged = [str(r) for r in (clip.get("_patch_evidence") or [])]
        for ref in evidence:
            if ref not in merged:
                merged.append(ref)
        clip["_patch_evidence"] = merged
        _check_source_within_media(plan, clip)
        return "applied"

    if operation == "reorder":
        i = index_by_id.get(str(target_id))
        if i is None:
            return f"skipped:target clip {target_id!r} not found"
        new_order = op.get("new_order")
        if new_order is None:
            return "skipped:reorder requires new_order"
        new_order = max(0, min(int(new_order), len(clips) - 1))
        item = clips.pop(i)
        clips.insert(new_order, item)
        return "applied"

    if operation == "restore":
        # P1-6:撤销对某 clip 的全部候选修改,恢复基线版本的 clip 记录
        # (extra.restore_clip 为基线 clip 深拷贝)。目标存在 → 原位替换;
        # 不存在(此前被 remove)→ 追加回去。时间轴落位由重排统一处理,
        # 恢复后保持基线 clip 的原时长(尊重其原始速度)。
        restored = (op.get("extra") or {}).get("restore_clip")
        if not isinstance(restored, dict):
            return "skipped:restore requires extra.restore_clip"
        clip = copy.deepcopy(restored)
        clip.pop("_patch_evidence", None)
        clip.pop("_patch_reason", None)
        i = index_by_id.get(str(target_id))
        if i is None:
            clips.append(clip)
        else:
            clips[i] = clip
        return "applied"

    if operation == "select":
        media_id = op.get("source_media_id")
        if not media_id:
            return "skipped:select requires source_media_id"
        new_in = op.get("source_in_ms")
        new_out = op.get("source_out_ms")
        if new_in is None or new_out is None or int(new_out) <= int(new_in):
            return "skipped:select requires valid source range"
        new_clip = {
            "clip_id": str(target_id or f"clip_sel_{op_index + 1:04d}"),
            "source_media_id": str(media_id),
            "source_in_ms": int(new_in),
            "source_out_ms": int(new_out),
            # 速度恒 1.0:时间轴跨度 = 源跨度(重排阶段统一落位)
            "timeline_in_ms": 0,
            "timeline_out_ms": int(new_out) - int(new_in),
            "transcript": None,
            "semantic_tags": [],
            "_patch_evidence": [str(r) for r in evidence],
            "_patch_reason": str(op.get("reason") or ""),
        }
        clips.append(new_clip)
        _check_source_within_media(plan, new_clip)
        return "applied"

    return f"skipped:unknown operation {operation!r}"


def apply_patch(
    base_plan: dict[str, Any],
    patch: dict[str, Any],
    *,
    expect_base_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把 patch 应用到基线 plan 的**深拷贝**上,返回 (candidate_plan, report)。

    - ``expect_base_sha256`` 给定时,与 patch.base_plan_sha256 不一致 → PatchError
      (基线漂移拒绝应用);base_plan 本身的实际 hash 也必须等于 patch 绑定值,
      否则同样是漂移。
    - operations 按列表顺序应用;``remove`` 目标不存在 → skip(幂等);
    - 全部应用后统一重排 timeline(preserve_and_remap);
    - 返回的 report 含 applied/skipped 明细与 before/after hash(diff 可解释)。
    """
    if not isinstance(base_plan, dict):
        raise PatchError("base_plan must be a dict (CanonicalTimeline shape)")
    ops = patch.get("operations") if isinstance(patch, dict) else None
    if not isinstance(ops, list):
        raise PatchError("patch.operations must be a list")

    bound_sha = str(patch.get("base_plan_sha256") or "")
    actual_sha = plan_sha256(base_plan)
    if expect_base_sha256 is not None and str(expect_base_sha256) != bound_sha:
        raise PatchError(
            f"base plan sha mismatch: patch bound to {bound_sha[:12]}…, "
            f"expected {str(expect_base_sha256)[:12]}… (基线漂移,拒绝应用)"
        )
    if actual_sha != bound_sha:
        raise PatchError(
            f"base plan content drift: actual {actual_sha[:12]}… != patch bound {bound_sha[:12]}…"
        )

    candidate = copy.deepcopy(base_plan)
    clips = candidate.get("clips")
    if not isinstance(clips, list):
        raise PatchError("base_plan.clips must be a list")
    clips = [c for c in clips if isinstance(c, dict)]

    applied: list[str] = []
    skipped: list[dict[str, Any]] = []
    for idx, op in enumerate(ops):
        if not isinstance(op, dict):
            skipped.append({"operation_index": idx, "reason": "operation must be a dict"})
            continue
        result = _apply_one(candidate, clips, op, idx)
        if result == "applied":
            applied.append(str(op.get("operation_id") or f"op_{idx + 1:04d}"))
        else:
            skipped.append(
                {"operation_id": op.get("operation_id"), "operation_index": idx, "reason": result}
            )

    candidate["clips"] = clips
    _relayout(clips)

    # 审计信息只放 report(CanonicalTimeline 是 extra="forbid",plan 内不得
    # 携带私有键;候选的 hash 用 strip 后口径计算)
    cleaned = strip_internal_marks(candidate)
    report: dict[str, Any] = {
        "applied_operations": applied,
        "skipped_operations": skipped,
        "base_plan_sha256": bound_sha,
        "candidate_plan_sha256": plan_sha256(cleaned),
        "operation_count": len(ops),
        "clip_count_before": len(base_plan.get("clips") or []),
        "clip_count_after": len(clips),
        "track_alignment_policy": TRACK_ALIGNMENT_POLICY,
    }
    return candidate, report


# ---------------------------------------------------------------------------
# diff(可解释变更清单)
# ---------------------------------------------------------------------------
def diff_plans(
    base_plan: dict[str, Any], candidate_plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """基线 vs 候选的逐 clip diff(removed / added / moved / trimmed)。

    每条 diff 都可追溯到 clip_id(或新增 clip 的 op 证据),供 report.md 与
    influence trace 消费;不含任何非结构性字段噪音。
    """
    base_clips = [c for c in (base_plan.get("clips") or []) if isinstance(c, dict)]
    cand_clips = [c for c in (candidate_plan.get("clips") or []) if isinstance(c, dict)]
    base_by_id = {str(c.get("clip_id")): c for c in base_clips}
    cand_by_id = {str(c.get("clip_id")): c for c in cand_clips}
    diffs: list[dict[str, Any]] = []

    for clip_id, before in base_by_id.items():
        after = cand_by_id.get(clip_id)
        if after is None:
            diffs.append({"change": "removed", "clip_id": clip_id, "before": _clip_view(before)})
            continue
        moved = int(after["timeline_in_ms"]) != int(before["timeline_in_ms"])
        trimmed = (int(after["source_in_ms"]), int(after["source_out_ms"])) != (
            int(before["source_in_ms"]), int(before["source_out_ms"])
        )
        if moved or trimmed:
            entry: dict[str, Any] = {
                "change": "trimmed" if trimmed else "moved",
                "clip_id": clip_id,
                "before": _clip_view(before),
                "after": _clip_view(after),
            }
            evidence = after.get("_patch_evidence") or []
            if evidence:
                entry["evidence_refs"] = list(evidence)
            diffs.append(entry)

    for clip_id, after in cand_by_id.items():
        if clip_id not in base_by_id:
            entry = {"change": "added", "clip_id": clip_id, "after": _clip_view(after)}
            evidence = after.get("_patch_evidence") or []
            if evidence:
                entry["evidence_refs"] = list(evidence)
            diffs.append(entry)
    return diffs


def _clip_view(clip: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_media_id": clip.get("source_media_id"),
        "source_in_ms": clip.get("source_in_ms"),
        "source_out_ms": clip.get("source_out_ms"),
        "timeline_in_ms": clip.get("timeline_in_ms"),
        "timeline_out_ms": clip.get("timeline_out_ms"),
        "voiceover_id": clip.get("voiceover_id"),
    }


def strip_internal_marks(candidate_plan: dict[str, Any]) -> dict[str, Any]:
    """去掉 apply 阶段的内部标记(_patch_evidence / _patch_reason)。

    对外产物(candidate_plan.json / mapper 输入)不携带下划线私有键;
    CanonicalTimeline 模型是 extra="forbid",这些键必须先剥掉。
    """
    cleaned = copy.deepcopy(candidate_plan)
    for clip in cleaned.get("clips") or []:
        if isinstance(clip, dict):
            clip.pop("_patch_evidence", None)
            clip.pop("_patch_reason", None)
    return cleaned


__all__ = [
    "PATCH_SCHEMA_VERSION",
    "OPERATION_TYPES",
    "TRACK_ALIGNMENT_POLICY",
    "PatchError",
    "new_patch",
    "add_operation",
    "plan_sha256",
    "apply_patch",
    "diff_plans",
    "strip_internal_marks",
]
