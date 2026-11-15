"""候选 CanonicalTimeline → 临时剪映草稿(P1-5,设计文档 §3.4 / F-06)。

QC 要评价的必须是"即将生成的剪映草稿",而不是 assembly 自己的 timeline JSON。
本模块用与正式 ``generate_draft`` **完全相同**的映射链:

    candidate_plan → canonical_to_draft(storyline/mapper) → storyline_to_native
    (storyline/native_draft) → 临时 draft dict

落盘纪律:
- 临时草稿只落 ``outputs/<job>/assembly/qc_draft/``,与正式草稿库完全隔离;
- 用 :func:`draft_ops.atomic_writer.atomic_write_draft_pair` 双文件原子写
  (与 safe_write_draft 同一落盘原语),但**不走** ``safe_write_draft`` ——
  无剪映互斥检查、无快照回滚(QC 产物不需要,且绝不能触碰剪映正式库);
- QC 全程不修改 ``draft_path`` / 剪映草稿库 / 原始 storyline_plan。

一致性校验(:func:`draft_video_binding` / :func:`validate_candidate_draft`):
临时草稿 video track 的 segment 顺序 / target_timerange / source_timerange
必须与候选计划逐一对应(按**值**核对,验证 ID 约定本身);配音 / BGM 段与
候选 clips 的组映射必须闭合;引用完整性复用 ``draft_ops.draft_integrity``
现成检查。issue 统一形状::

    {"severity", "code", "message", "check",
     "clip_id"?, "segment_id"?, "material_id"?, "range"?, "clip_ids"?}

``code`` ∈ missing_segment / extra_segment / broken_reference / track_desync /
missing_asset / invalid_range —— repair_loop(P1-6)按 code 分派修复策略。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from draft_ops.atomic_writer import atomic_write_draft_pair
from draft_ops.draft_integrity import (
    check_material_paths,
    check_track_references,
    check_video_track,
)
from storyline.contract import CanonicalTimeline
from storyline.mapper import canonical_to_draft
from storyline.native_draft import storyline_to_native

CANDIDATE_DRAFT_DIR_NAME = "qc_draft"
DRAFT_CONTENT_NAME = "draft_content.json"
DRAFT_INFO_NAME = "draft_info.json"


class CandidateDraftError(Exception):
    """候选计划 → 临时草稿映射失败(候选非法 / native 转换失败)。"""


# ---------------------------------------------------------------------------
# 映射(与正式 generate_draft 同一条链)
# ---------------------------------------------------------------------------
def build_candidate_draft(candidate_plan: dict[str, Any]) -> dict[str, Any]:
    """候选计划 → (storyline 格式草稿, 原生 5.9 草稿, sidecar)。

    Raises:
        CandidateDraftError: 候选过不了 ``CanonicalTimeline.model_validate``
            或 native 转换失败(QC 不放行非法候选)。
    """
    try:
        plan_model = CanonicalTimeline.model_validate(candidate_plan)
    except Exception as exc:  # noqa: BLE001 — ValidationError / ValueError
        raise CandidateDraftError(f"candidate plan invalid: {exc!r}") from exc
    draft_storyline = canonical_to_draft(plan_model)
    try:
        draft_native, sidecar = storyline_to_native(draft_storyline)
    except Exception as exc:  # noqa: BLE001
        raise CandidateDraftError(f"native conversion failed: {exc!r}") from exc
    return {
        "draft_storyline": draft_storyline,
        "draft_content": draft_native,
        "sidecar": sidecar,
    }


def write_candidate_draft(
    candidate_plan: dict[str, Any],
    out_dir: Path,
) -> dict[str, Any]:
    """临时草稿原子双写落盘(isolated QC 产物目录)。"""
    built = build_candidate_draft(candidate_plan)
    draft_dir = Path(out_dir) / CANDIDATE_DRAFT_DIR_NAME
    draft_dir.mkdir(parents=True, exist_ok=True)
    results = atomic_write_draft_pair(draft_dir, built["draft_content"])
    if built["sidecar"] is not None:
        (draft_dir / "autovideo_storyline_source.json").write_text(
            json.dumps(built["sidecar"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return {
        "draft_dir": str(draft_dir),
        "draft_content_path": str(draft_dir / DRAFT_CONTENT_NAME),
        "draft_info_path": str(draft_dir / DRAFT_INFO_NAME),
        "storyline_draft": built["draft_storyline"],
        "draft_content": built["draft_content"],
        "write_result": results,
    }


# ---------------------------------------------------------------------------
# 一致性映射(video track segment ↔ 候选 clip)
# ---------------------------------------------------------------------------
def draft_video_binding(
    candidate_plan: dict[str, Any], draft_content: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """候选 clip ↔ 草稿 video segment 逐一核对。

    Returns:
        (binding, issues):binding = 逐 clip 对应表(顺序即成片顺序);
        issues = 不一致处(adapter 统一 issue 形状,按值核对而非按 ID 约定)。
    """
    issues: list[dict[str, Any]] = []
    clips = [c for c in candidate_plan.get("clips") or [] if isinstance(c, dict)]
    video_segments: list[dict[str, Any]] = []
    for track in draft_content.get("tracks") or []:
        if isinstance(track, dict) and track.get("type") == "video":
            video_segments.extend(
                seg for seg in track.get("segments") or [] if isinstance(seg, dict)
            )
    binding: list[dict[str, Any]] = []
    for idx, clip in enumerate(clips):
        seg = video_segments[idx] if idx < len(video_segments) else None
        clip_id = str(clip.get("clip_id"))
        expected_target = {
            "start": int(clip["timeline_in_ms"]) * 1000,
            "duration": (int(clip["timeline_out_ms"]) - int(clip["timeline_in_ms"])) * 1000,
        }
        expected_source = {
            "start": int(clip["source_in_ms"]) * 1000,
            "duration": (int(clip["source_out_ms"]) - int(clip["source_in_ms"])) * 1000,
        }
        entry: dict[str, Any] = {
            "clip_id": clip_id,
            "segment_id": (seg or {}).get("id"),
            "expected_target": expected_target,
            "expected_source": expected_source,
        }
        binding.append(entry)
        if seg is None:
            issues.append({
                "severity": "error",
                "code": "missing_segment",
                "check": "候选草稿一致性",
                "message": f"候选 clip {clip_id} 在临时草稿 video track 中没有对应 segment",
                "clip_id": clip_id,
            })
            continue
        seg_target = seg.get("target_timerange") or {}
        seg_source = seg.get("source_timerange") or {}
        actual_target = {
            "start": int(seg_target.get("start") or 0),
            "duration": int(seg_target.get("duration") or 0),
        }
        actual_source = {
            "start": int(seg_source.get("start") or 0),
            "duration": int(seg_source.get("duration") or 0),
        }
        entry["actual_target"] = actual_target
        entry["actual_source"] = actual_source
        if actual_target != expected_target:
            issues.append({
                "severity": "error",
                "code": "track_desync",
                "check": "候选草稿一致性",
                "message": (
                    f"clip {clip_id} 的草稿 target_timerange 与候选计划不一致: "
                    f"draft={actual_target} candidate={expected_target}"
                ),
                "clip_id": clip_id,
                "segment_id": seg.get("id"),
                "range": [int(clip["timeline_in_ms"]), int(clip["timeline_out_ms"])],
            })
        if actual_source != expected_source:
            issues.append({
                "severity": "error",
                "code": "track_desync",
                "check": "候选草稿一致性",
                "message": (
                    f"clip {clip_id} 的草稿 source_timerange 与候选计划不一致: "
                    f"draft={actual_source} candidate={expected_source}"
                ),
                "clip_id": clip_id,
                "segment_id": seg.get("id"),
                "range": [int(clip["source_in_ms"]), int(clip["source_out_ms"])],
            })
    if len(video_segments) > len(clips):
        for extra in video_segments[len(clips):]:
            issues.append({
                "severity": "error",
                "code": "extra_segment",
                "check": "候选草稿一致性",
                "message": f"草稿 video track 多出候选计划之外的 segment {extra.get('id')}",
                "segment_id": extra.get("id"),
            })
    return binding, issues


def _audio_range_us(seg: dict[str, Any]) -> tuple[int, int]:
    """音频段的时间轴区间(微秒):storyline 格式带 *_us 派生字段,native 带
    target_timerange —— 两种形态统一读取。"""
    tr = seg.get("target_timerange") or {}
    if tr.get("start") is not None:
        start = int(tr["start"])
        return start, start + int(tr.get("duration") or 0)
    start = int(seg.get("target_start_us") or 0)
    return start, int(seg.get("target_end_us") or start)


def _audio_track_binding_issues(
    candidate_plan: dict[str, Any], audio_draft: dict[str, Any]
) -> list[dict[str, Any]]:
    """关联轨道同步校验:配音组区间 / BGM 总长必须与候选 clips 重推导一致。

    ``audio_draft`` 用 **mapper 输出(storyline 格式草稿)** —— native 转换会
    合法丢弃非本地资源(如音乐库 BGM ref),那属于发布层解析,不是映射错误;
    对 native 跑本检查会把合法草稿误报成 track_desync。
    """
    issues: list[dict[str, Any]] = []
    clips = [c for c in candidate_plan.get("clips") or [] if isinstance(c, dict)]
    audio_segments: list[dict[str, Any]] = []
    for track in audio_draft.get("tracks") or []:
        if isinstance(track, dict) and track.get("type") == "audio":
            audio_segments.extend(
                seg for seg in track.get("segments") or [] if isinstance(seg, dict)
            )
    seg_by_material = {str(seg.get("material_id")): seg for seg in audio_segments}

    # BGM:candidate 有 bgm_ref → mapper 必产出 segment-bgm-1 且铺到
    # clips[-1].timeline_out_ms;无 bgm_ref → 不得出现(BGM 不丢、不悬空)
    bgm_ref = (candidate_plan.get("audio") or {}).get("bgm_ref")
    bgm_seg = seg_by_material.get("bgm-1")
    if bgm_ref and clips:
        if bgm_seg is None:
            issues.append({
                "severity": "error",
                "code": "track_desync",
                "check": "关联轨道同步",
                "message": "候选计划含 BGM 引用但 mapper 草稿没有 BGM 段(BGM 不得因视频轨编辑而丢失)",
            })
        else:
            expected_end = int(clips[-1]["timeline_out_ms"]) * 1000
            actual_end = _audio_range_us(bgm_seg)[1]
            if actual_end != expected_end:
                issues.append({
                    "severity": "error",
                    "code": "track_desync",
                    "check": "关联轨道同步",
                    "message": f"BGM 段终点与候选总长不一致: draft={actual_end} candidate={expected_end}",
                    "segment_id": bgm_seg.get("id"),
                    "range": [0, expected_end // 1000],
                })
    elif not bgm_ref and bgm_seg is not None:
        issues.append({
            "severity": "error",
            "code": "broken_reference",
            "check": "关联轨道同步",
            "message": "候选计划无 BGM 引用但草稿出现 bgm-1 段(悬空引用)",
            "segment_id": bgm_seg.get("id"),
        })

    # 配音:每条可物化 voiceover(path 非空且时长>0)按组首/组末 clip 铺段
    voiceovers = (candidate_plan.get("audio") or {}).get("voiceovers") or []
    clips_by_vo: dict[str, list[dict]] = {}
    for clip in clips:
        if clip.get("voiceover_id"):
            clips_by_vo.setdefault(str(clip["voiceover_id"]), []).append(clip)
    for idx, vo in enumerate(voiceovers, start=1):
        if not isinstance(vo, dict):
            continue
        material_id = f"voiceover-{idx:04d}"
        vo_id = str(vo.get("voiceover_id"))
        expected_range: tuple[int, int] | None = None
        if vo.get("path") and int(vo.get("duration_ms") or 0) > 0:
            group = sorted(
                clips_by_vo.get(vo_id, []),
                key=lambda c: int(c["timeline_in_ms"]),
            )
            if group:
                expected_range = (
                    int(group[0]["timeline_in_ms"]) * 1000,
                    int(group[-1]["timeline_out_ms"]) * 1000,
                )
        actual_seg = seg_by_material.get(material_id)
        if expected_range is None:
            if actual_seg is not None:
                issues.append({
                    "severity": "error",
                    "code": "track_desync",
                    "check": "关联轨道同步",
                    "message": f"配音 {vo_id} 不可物化(空 path/0 时长/组空)但草稿出现其 segment",
                    "segment_id": actual_seg.get("id"),
                    "material_id": material_id,
                })
            continue
        if actual_seg is None:
            issues.append({
                "severity": "error",
                "code": "track_desync",
                "check": "关联轨道同步",
                "message": f"配音 {vo_id} 在候选计划中可物化但草稿缺失其 segment",
                "material_id": material_id,
                "clip_ids": [str(c.get("clip_id")) for c in clips_by_vo.get(vo_id, [])],
            })
            continue
        actual_range = _audio_range_us(actual_seg)
        if actual_range != expected_range:
            issues.append({
                "severity": "error",
                "code": "track_desync",
                "check": "关联轨道同步",
                "message": (
                    f"配音 {vo_id} 段区间与候选 clip 组不一致: "
                    f"draft={actual_range} candidate={expected_range}"
                ),
                "segment_id": actual_seg.get("id"),
                "material_id": material_id,
                "clip_ids": [str(c.get("clip_id")) for c in clips_by_vo.get(vo_id, [])],
                "range": [actual_range[0] // 1000, actual_range[1] // 1000],
            })
    return issues


# integrity 检查的 check 名 → typed code 映射(repair_loop 分派用)
_INTEGRITY_CODE_MAP = {
    "素材引用": "broken_reference",
    "素材路径": "missing_asset",
    "视频轨": "missing_asset",
    "候选草稿一致性": "track_desync",
    "关联轨道同步": "track_desync",
}


def _normalize_issue(raw: dict[str, Any]) -> dict[str, Any]:
    """把 integrity / adapter 两种来源的 issue 统一成 adapter 形状。"""
    issue = {
        "severity": str(raw.get("severity") or "warning"),
        "code": str(raw.get("code") or _INTEGRITY_CODE_MAP.get(str(raw.get("check") or ""), "invalid_range")),
        "check": str(raw.get("check") or "候选草稿一致性"),
        "message": str(raw.get("message") or raw.get("detail") or ""),
    }
    for key in ("clip_id", "segment_id", "material_id", "range", "clip_ids"):
        if raw.get(key) is not None:
            issue[key] = raw[key]
    return issue


def validate_candidate_draft(
    candidate_plan: dict[str, Any],
    draft_content: dict[str, Any],
    *,
    storyline_draft: dict[str, Any] | None = None,
    draft_tag: str = "candidate_qc",
) -> dict[str, Any]:
    """候选计划 ↔ 临时草稿的全套结构校验,返回 {issues, binding}。

    校验层次(设计文档 §3.4):
    1. 草稿结构/引用完整性(复用 draft_integrity 现成检查,跑 **native 草稿**);
    2. video track ↔ 候选 clip 逐一按值核对(跑 native 草稿;native 的 video
       segment 保留 target/source_timerange 原值);
    3. 关联轨道(BGM/配音)与候选 clips 的组映射闭合 —— 跑 **mapper 输出
       (storyline 草稿)**,见 :func:`_audio_track_binding_issues` 的纪律说明;
    4. 素材路径存在性(QC 产物素材都在本机)。

    Args:
        storyline_draft: ``build_candidate_draft`` 返回的 ``draft_storyline``;
            未提供时音频检查退回 draft_content(调用方自担形态差异)。
    """
    raw_issues: list[dict[str, Any]] = []
    for issue in (
        check_video_track(draft_content, draft_tag)
        + check_track_references(draft_content, draft_tag)
    ):
        if isinstance(issue, dict):
            raw_issues.append({
                "severity": issue.get("severity"),
                "code": _INTEGRITY_CODE_MAP.get(str(issue.get("check") or ""), "broken_reference"),
                "check": issue.get("check"),
                "message": issue.get("detail"),
            })
    for issue in check_material_paths(draft_content, draft_tag):
        if isinstance(issue, dict):
            # P1-6 排障修订:素材**文件**是否在磁盘上是 discover 层的职责
            # (probe_status=failed + degraded_reason 已记录,候选已按 degraded
            # 处理);QC 层把文件存在性降为 warning,避免把"种子草稿 + 虚构
            # 素材路径"的合法流与发布层才解析的资源误杀成结构性错误。
            # 真正的结构完整性 = 引用闭合(check_track_references)+
            # 契约硬门 + binding 一致性,均为 error 级。
            raw_issues.append({
                "severity": "warning",
                "code": "missing_asset",
                "check": issue.get("check"),
                "message": issue.get("detail"),
            })
    binding, binding_issues = draft_video_binding(candidate_plan, draft_content)
    raw_issues.extend(binding_issues)
    audio_draft = storyline_draft if storyline_draft is not None else draft_content
    raw_issues.extend(_audio_track_binding_issues(candidate_plan, audio_draft))

    issues = [_normalize_issue(i) for i in raw_issues if isinstance(i, dict)]
    return {"issues": issues, "binding": binding}


__all__ = [
    "CANDIDATE_DRAFT_DIR_NAME",
    "CandidateDraftError",
    "build_candidate_draft",
    "write_candidate_draft",
    "draft_video_binding",
    "validate_candidate_draft",
]
