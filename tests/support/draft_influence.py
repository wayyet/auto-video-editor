"""草稿影响对比 helpers(第六期 P1-0 防回归)。

供 assembly 重构各阶段测试复用:
- :func:`draft_video_summary` / :func:`draft_audio_summary`:把 draft(私有小写
  格式或剪映原生格式,两者 video/audio 段都带 ``target_timerange`` /
  ``source_timerange``)的轨/段结构拍平成可比较摘要;
- :func:`compare_draft_video` / :func:`assert_video_track_equal`:视频轨逐段对比,
  这是"assembly 决策改变最终剪映视频轨"类断言的统一入口;
- :func:`assert_draft_refs_intact`:素材引用完整性(缺失素材 / 悬空引用 /
  重复 ID 三类),缺失与悬空复用 ``draft_integrity.check_track_references``,
  重复 ID 在此补齐检测;
- :func:`load_canonical_sample` / :func:`load_canonical_sample_model`:共享
  CanonicalTimeline fixture(tests/fixtures/storyline/canonical_timeline_sample.json)。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from draft_ops.draft_integrity import check_track_references
from storyline.contract import CanonicalTimeline

_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_SAMPLE_PATH = _ROOT / "tests" / "fixtures" / "storyline" / "canonical_timeline_sample.json"


# ---------------------------------------------------------------------------
# fixture 加载
# ---------------------------------------------------------------------------
def load_canonical_sample() -> dict[str, Any]:
    """读共享 CanonicalTimeline fixture,返回原始 dict(每次深拷贝,防串扰)。"""
    with CANONICAL_SAMPLE_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_canonical_sample_model() -> CanonicalTimeline:
    """读共享 fixture 并过 ``CanonicalTimeline.model_validate``。"""
    return CanonicalTimeline.model_validate(load_canonical_sample())


# ---------------------------------------------------------------------------
# 轨/段摘要
# ---------------------------------------------------------------------------
def _segments_of(draft: dict[str, Any], track_type: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for track in draft.get("tracks") or []:
        if isinstance(track, dict) and track.get("type") == track_type:
            segments.extend(seg for seg in track.get("segments") or [] if isinstance(seg, dict))
    return segments


def _target_range(seg: dict[str, Any]) -> tuple[int, int]:
    """段的 target 区间 (start_us, end_us)。优先 target_timerange,回落派生字段。"""
    tr = seg.get("target_timerange") or {}
    if isinstance(tr, dict) and tr.get("start") is not None:
        start = int(tr["start"])
        duration = int(tr.get("duration") or 0)
    else:
        start = int(seg.get("target_start_us") or 0)
        end = int(seg.get("target_end_us") or start)
        duration = end - start
    return start, start + duration


def _source_range(seg: dict[str, Any]) -> tuple[int, int]:
    sr = seg.get("source_timerange") or {}
    if isinstance(sr, dict) and sr.get("start") is not None:
        start = int(sr["start"])
        duration = int(sr.get("duration") or 0)
    else:
        start = int(seg.get("source_in_us") or 0)
        end = int(seg.get("source_out_us") or start)
        duration = end - start
    return start, start + duration


def draft_video_summary(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """视频轨逐段摘要:顺序 = 轨内出现顺序(即最终成片顺序)。"""
    summary = []
    for seg in _segments_of(draft, "video"):
        t_start, t_end = _target_range(seg)
        s_start, s_end = _source_range(seg)
        summary.append(
            {
                "segment_id": seg.get("id"),
                "material_id": seg.get("material_id"),
                "target_start_us": t_start,
                "target_end_us": t_end,
                "target_duration_us": t_end - t_start,
                "source_start_us": s_start,
                "source_end_us": s_end,
                "source_duration_us": s_end - s_start,
                "speed": seg.get("speed"),
                "transcript": seg.get("transcript"),
            }
        )
    return summary


def draft_audio_summary(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """音频轨逐段摘要(BGM / 配音落位)。"""
    summary = []
    for seg in _segments_of(draft, "audio"):
        t_start, t_end = _target_range(seg)
        summary.append(
            {
                "segment_id": seg.get("id"),
                "material_id": seg.get("material_id"),
                "target_start_us": t_start,
                "target_end_us": t_end,
                "loop": seg.get("loop"),
            }
        )
    return summary


def draft_audio_materials(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """materials.audios 摘要(role / ref / provider 等,音轨字段断言用)。"""
    materials = draft.get("materials") or {}
    return [m for m in materials.get("audios") or [] if isinstance(m, dict)]


# ---------------------------------------------------------------------------
# 对比 / 断言
# ---------------------------------------------------------------------------
def compare_draft_video(
    actual: dict[str, Any], expected: dict[str, Any]
) -> list[str]:
    """两份 draft 的视频轨逐段对比,返回差异文本列表(空 = 一致)。"""
    diffs: list[str] = []
    a = draft_video_summary(actual)
    b = draft_video_summary(expected)
    if len(a) != len(b):
        diffs.append(f"视频轨段数不同: actual={len(a)} expected={len(b)}")
    for idx, (left, right) in enumerate(zip(a, b)):
        for key in left:
            if left.get(key) != right.get(key):
                diffs.append(
                    f"段#{idx} {left.get('segment_id')!r} 字段 {key}: "
                    f"actual={left.get(key)!r} expected={right.get(key)!r}"
                )
    return diffs


def assert_video_track_equal(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    """视频轨不一致时抛 AssertionError,消息里带逐字段差异。"""
    diffs = compare_draft_video(actual, expected)
    if diffs:
        raise AssertionError("视频轨与期望不一致:\n" + "\n".join(diffs))


def _duplicate_issues(draft: dict[str, Any]) -> list[str]:
    """重复 ID 检测:materials 各桶内重复 id + 每条轨内重复 segment id。"""
    issues: list[str] = []
    materials = draft.get("materials") or {}
    for bucket_name, bucket in materials.items():
        if not isinstance(bucket, list):
            continue
        seen: dict[str, int] = {}
        for item in bucket:
            if isinstance(item, dict) and item.get("id"):
                key = str(item["id"])
                seen[key] = seen.get(key, 0) + 1
        for mid, count in seen.items():
            if count > 1:
                issues.append(f"materials.{bucket_name} 中素材 id={mid} 重复出现 {count} 次")
    for ti, track in enumerate(draft.get("tracks") or []):
        if not isinstance(track, dict):
            continue
        seen_seg: dict[str, int] = {}
        for seg in track.get("segments") or []:
            if isinstance(seg, dict) and seg.get("id"):
                key = str(seg["id"])
                seen_seg[key] = seen_seg.get(key, 0) + 1
        for sid, count in seen_seg.items():
            if count > 1:
                issues.append(f"track#{ti}({track.get('type')}) 中 segment id={sid} 重复出现 {count} 次")
    return issues


def collect_ref_issues(draft: dict[str, Any]) -> list[str]:
    """引用完整性全部 issue:悬空引用(check_track_references)+ 重复 ID。"""
    issues = [
        f"{i.get('check', '?')}: {i.get('detail', '')}"
        for i in check_track_references(copy.deepcopy(draft), tag="draft_influence_helper")
        if isinstance(i, dict)
    ]
    issues.extend(_duplicate_issues(draft))
    return issues


def assert_draft_refs_intact(draft: dict[str, Any]) -> None:
    """素材引用完整性断言:缺失素材 / 悬空引用 / 重复 ID 任一出现即失败。"""
    issues = collect_ref_issues(draft)
    if issues:
        raise AssertionError("草稿引用完整性检查未通过:\n" + "\n".join(issues))


__all__ = [
    "CANONICAL_SAMPLE_PATH",
    "load_canonical_sample",
    "load_canonical_sample_model",
    "draft_video_summary",
    "draft_audio_summary",
    "draft_audio_materials",
    "compare_draft_video",
    "assert_video_track_equal",
    "collect_ref_issues",
    "assert_draft_refs_intact",
]
