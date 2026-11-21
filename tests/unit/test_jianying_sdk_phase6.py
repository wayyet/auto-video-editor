"""第10期阶段6 单测:贴纸 SDK 独立轨 + node_11 发射 + AI 转场普通片段断言。

覆盖迁移设计文档 §9 阶段6 验收(结构级):
- 贴纸走 ``StickerSegment`` + 独立 sticker 轨(不再挂 video segment refs);
- 占位/非 19 位 resource_id fail-closed 降级(§4.7/§6.6);
- node_11 注入后把贴纸计划发射进 spec;
- AI 转场 MP4 在编译产物中是**普通视频片段**(§0 约束 B:不产生假 transition)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jianying_sdk._sdk_loader import ensure_sdk_on_path

ensure_sdk_on_path()

from jianying_sdk.compiler import TRACK_STICKERS, compile_spec
from jianying_sdk.spec import DraftBuildSpec, StickerSpec, TimelineClipSpec
from jianying_sdk.timeline_extractor import canvas_of, extract_timeline_snapshot
from tests.unit.test_jianying_sdk_phase2 import (
    _audio_factory,
    _native_draft,
    _video_factory,
)


def _compile(spec: DraftBuildSpec):
    return compile_spec(spec, video_material_factory=_video_factory, audio_material_factory=_audio_factory)


def _spec_from_extract(draft: dict) -> DraftBuildSpec:
    report = extract_timeline_snapshot(draft)
    spec = DraftBuildSpec(job_id="phase6", language="zh", canvas=canvas_of(draft))
    spec.timeline = report.section
    return spec


# ---------------------------------------------------------------------------
# 贴纸编译
# ---------------------------------------------------------------------------
_REAL_ID = "7049979667406656014"  # 19 位真实形态 ID(交叉表转场资源同形)


def test_sticker_independent_track() -> None:
    draft, _ = _native_draft(with_audio=False)
    spec = _spec_from_extract(draft)
    spec.stickers = [
        StickerSpec(sticker_id="sticker-text-zh-1", resource_id=_REAL_ID,
                    start_us=500_000, duration_us=1_500_000,
                    keyword="测试", source_subtitle_id="text-zh-1"),
    ]
    result = _compile(spec)
    tracks = {t["name"]: t for t in result.payload["tracks"]}
    assert TRACK_STICKERS in tracks, "贴纸必须有独立 sticker 轨"
    segs = tracks[TRACK_STICKERS]["segments"]
    assert len(segs) == 1
    assert segs[0]["target_timerange"] == {"start": 500_000, "duration": 1_500_000}
    bucket = result.payload["materials"]["stickers"]
    assert len(bucket) == 1
    assert bucket[0]["resource_id"] == _REAL_ID
    assert result.summary["counts"]["stickers_applied"] == 1
    # 贴纸不在视频段 refs 上(SDK 独立轨表达)
    video_track = next(t for t in result.payload["tracks"] if t["type"] == "video")
    for seg in video_track["segments"]:
        assert all(
            r not in [b["id"] for b in bucket]
            for r in seg.get("extra_material_refs", [])
        )


def test_sticker_placeholder_fail_closed() -> None:
    draft, _ = _native_draft(with_audio=False)
    spec = _spec_from_extract(draft)
    spec.stickers = [
        StickerSpec(sticker_id="sticker-a", resource_id="PLACEHOLDER_LOL",
                    start_us=0, duration_us=1_000_000),
        StickerSpec(sticker_id="sticker-b", resource_id="12345",
                    start_us=0, duration_us=1_000_000),
    ]
    result = _compile(spec)
    assert result.payload["materials"]["stickers"] == []
    reasons = {(d["kind"], d["reason"]) for d in result.summary["degradations"]}
    assert ("sticker", "placeholder_or_invalid_resource_id") in reasons


def test_sticker_zero_duration_degrades() -> None:
    draft, _ = _native_draft(with_audio=False)
    spec = _spec_from_extract(draft)
    spec.stickers = [
        StickerSpec(sticker_id="sticker-a", resource_id=_REAL_ID,
                    start_us=0, duration_us=0),
    ]
    result = _compile(spec)
    assert result.payload["materials"]["stickers"] == []


# ---------------------------------------------------------------------------
# AI 转场 = 普通视频片段(§0 约束 B)
# ---------------------------------------------------------------------------
def test_ai_transition_clip_is_plain_video_segment() -> None:
    draft, _ = _native_draft(with_audio=False)
    plan = {
        "clips": [
            {"clip_id": "clip_0001", "ai_transition_pair": None},
            {"clip_id": "clip_0002", "ai_transition_pair": "clip_0001->clip_0002"},
        ]
    }
    report = extract_timeline_snapshot(draft, canonical_plan=plan)
    assert report.section.clips[1].role == "ai_transition"

    spec = DraftBuildSpec(job_id="p6", language="zh", canvas=canvas_of(draft))
    spec.timeline = report.section
    result = _compile(spec)
    # 无假 transition material;AI 片段是普通视频段
    assert result.payload["materials"]["transitions"] == []
    video_track = next(t for t in result.payload["tracks"] if t["type"] == "video")
    assert len(video_track["segments"]) == 2
    assert video_track["segments"][1]["speed"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# node_11 发射
# ---------------------------------------------------------------------------
def _sticker_draft(text_id: str = "text-zh-1") -> dict:
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 4_000_000,
        "materials": {
            "videos": [{"id": "v1", "path": "C:/m.mp4", "material_name": "clip_0001"}],
            "audios": [], "speeds": [], "canvases": [],
            "sound_channel_mappings": [], "vocal_separations": [],
            "texts": [{"id": text_id, "content": "哈哈",
                       "target_timerange": {"start": 500_000, "duration": 1_500_000}}],
            "stickers": [],
        },
        "tracks": [
            {"type": "video", "id": "tv", "segments": [{
                "id": "s1", "material_id": "v1",
                "target_timerange": {"start": 0, "duration": 4_000_000},
                "extra_material_refs": [],
            }]},
            {"type": "text", "id": "tt", "segments": [{
                "id": "ts1", "material_id": text_id,
                "target_timerange": {"start": 500_000, "duration": 1_500_000},
                "extra_material_refs": [],
            }]},
        ],
    }


def test_node_11_emits_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 19 位 ID:原生写入照旧 + spec 记录计划(SDK 编译落独立轨)。"""
    from nodes.node_11_inject_sticker import _inject_sticker_into, _emit_sticker_spec

    draft = _sticker_draft()
    monkeypatch.setattr(
        "nodes.node_11_inject_sticker.resolve_sticker_resource_id",
        lambda text: _REAL_ID,
    )
    warnings, summary = _inject_sticker_into(draft, "node_11_zh")
    assert summary["written"] == 1

    log: list[str] = []
    _emit_sticker_spec(tmp_path, draft, log)
    assert any("贴纸计划已入 spec:1 条" in tag for tag in log)

    from nodes._draft_build_spec import load_build_spec

    spec = load_build_spec(tmp_path)
    assert spec is not None
    assert len(spec.stickers) == 1
    s = spec.stickers[0]
    assert s.resource_id == _REAL_ID
    assert s.start_us == 500_000
    assert s.duration_us == 1_500_000
    assert s.source_subtitle_id == "text-zh-1"


def test_node_11_placeholder_no_spec_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """占位 ID:fail-closed 不落草稿,spec 也不得携带占位条目(§6.6)。"""
    from nodes.node_11_inject_sticker import _inject_sticker_into, _emit_sticker_spec

    draft = _sticker_draft()
    monkeypatch.setattr(
        "nodes.node_11_inject_sticker.resolve_sticker_resource_id",
        lambda text: "PLACEHOLDER_X",
    )
    warnings, summary = _inject_sticker_into(draft, "node_11_zh")
    assert summary["written"] == 0
    assert summary["degraded_placeholder"] == 1

    log: list[str] = []
    _emit_sticker_spec(tmp_path, draft, log)
    from nodes._draft_build_spec import load_build_spec

    spec = load_build_spec(tmp_path)
    assert spec.stickers == []


def test_node_11_manual_sticker_not_in_spec(tmp_path: Path) -> None:
    """人工贴纸(uuid id)不由 spec 管理,原样保留在草稿不进计划。"""
    from nodes.node_11_inject_sticker import _emit_sticker_spec

    draft = _sticker_draft()
    draft["materials"]["stickers"].append({
        "id": "manual-uuid-sticker", "type": "sticker",
        "resource_id": _REAL_ID, "duration": 999_999,
    })
    log: list[str] = []
    _emit_sticker_spec(tmp_path, draft, log)
    from nodes._draft_build_spec import load_build_spec

    spec = load_build_spec(tmp_path)
    assert spec.stickers == []
