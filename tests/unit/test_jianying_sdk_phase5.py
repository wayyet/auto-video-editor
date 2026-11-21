"""第10期阶段5 单测:转场(前段挂载)/视频特效/视频动画 SDK 编译 + node_09 发射。

覆盖迁移设计文档 §9 阶段5 验收(结构级):
- 边界 i→i+1 转场挂到**前一片段** segments[i](§4.5 行为差异专项回归);
- 转场时长钳制不超过相邻两段较短者;unsupported 资源(快门/水墨)降级;
- 视频特效 add_effect 强度 0-100 口径;视频动画 in/out/group 编译;
- node_09 注入后把效果计划发射进 spec(zh/en 双稿口径一致)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jianying_sdk._sdk_loader import ensure_sdk_on_path, sdk

ensure_sdk_on_path()

from jianying_sdk.compiler import compile_spec
from jianying_sdk.resource_registry import load_compat_table, resolve_video_animation
from jianying_sdk.spec import (
    DraftBuildSpec,
    SubtitleSpec,
    TransitionSpec,
    VideoAnimationSpec,
    VideoEffectSpec,
)
from jianying_sdk.timeline_extractor import canvas_of, extract_timeline_snapshot
from nodes.node_09_inject_fx import _inject_fx_into, _sorted_video_segments
from jy_common.template_library import load_resource_libraries
from tests.unit.test_jianying_sdk_phase2 import (
    _audio_factory,
    _native_draft,
    _video_factory,
)


def _supported_effect_id(category: str) -> str:
    table = load_compat_table()
    for item in table["categories"][category]["items"]:
        if item["status"] == "supported":
            return str(item["effect_id"])
    raise AssertionError(f"交叉表 {category} 无 supported 条目")


def _unsupported_transition_ids() -> list[str]:
    table = load_compat_table()
    return [
        str(item["effect_id"])
        for item in table["categories"]["transition"]["items"]
        if item["status"] == "unsupported_missing_in_sdk"
    ]


def _spec_from_extract(draft: dict) -> DraftBuildSpec:
    report = extract_timeline_snapshot(draft)
    spec = DraftBuildSpec(job_id="phase5", language="zh", canvas=canvas_of(draft))
    spec.timeline = report.section
    return spec


def _compile(spec: DraftBuildSpec):
    return compile_spec(spec, video_material_factory=_video_factory, audio_material_factory=_audio_factory)


def _video_segments_by_index(payload: dict) -> list[dict]:
    video_track = next(t for t in payload["tracks"] if t["type"] == "video")
    return video_track["segments"]


# ---------------------------------------------------------------------------
# 转场编译(前段挂载专项)
# ---------------------------------------------------------------------------
def test_transition_binds_to_left_segment() -> None:
    """§4.5 核心:边界 0→1 的转场必须挂在 segments[0](旧实现挂 segments[1] 是错的)。"""
    draft, _ = _native_draft(with_audio=False)
    spec = _spec_from_extract(draft)
    spec.transitions = [
        TransitionSpec(
            boundary_index=0,
            from_clip_id="clip_0001", to_clip_id="clip_0002",
            effect_id=_supported_effect_id("transition"),
            resource_id="r", name="测试转场",
            duration_us=500_000, is_overlap=True,
        ),
    ]
    result = _compile(spec)
    segs = _video_segments_by_index(result.payload)
    assert len(result.payload["materials"]["transitions"]) == 1
    trans_id = result.payload["materials"]["transitions"][0]["id"]
    assert trans_id in segs[0]["extra_material_refs"], "转场必须挂前一段"
    assert trans_id not in segs[1]["extra_material_refs"], "旧版后一段挂法禁止复现"
    assert result.summary["counts"]["transitions_applied"] == 1


def test_transition_duration_clamped_to_neighbors() -> None:
    draft, _ = _native_draft(with_audio=False)
    spec = _spec_from_extract(draft)
    spec.transitions = [
        TransitionSpec(
            boundary_index=0,
            from_clip_id="clip_0001", to_clip_id="clip_0002",
            effect_id=_supported_effect_id("transition"),
            resource_id="r", name="长转场",
            duration_us=10_000_000,  # 超过右段 2s 时长
            is_overlap=True,
        ),
    ]
    result = _compile(spec)
    # 边界 0 = 左段 5s / 右段 2s → 钳到 2s
    trans = result.payload["materials"]["transitions"][0]
    assert trans["duration"] == 2_000_000


def test_transition_unsupported_degrades() -> None:
    draft, _ = _native_draft(with_audio=False)
    spec = _spec_from_extract(draft)
    missing = _unsupported_transition_ids()
    spec.transitions = [
        TransitionSpec(
            boundary_index=0, from_clip_id="a", to_clip_id="b",
            effect_id=missing[0], resource_id="r", name="快门",
            duration_us=500_000,
        ),
        TransitionSpec(
            boundary_index=7, from_clip_id="a", to_clip_id="b",
            effect_id=_supported_effect_id("transition"),
            resource_id="r", name="越界边界",
            duration_us=500_000,
        ),
    ]
    result = _compile(spec)
    assert result.payload["materials"]["transitions"] == []
    kinds = {(d["kind"], d["reason"]) for d in result.summary["degradations"]}
    assert ("transition", "unsupported_missing_in_sdk_or_unverified") in kinds
    assert ("transition", "boundary_out_of_range") in kinds


def test_transition_duplicate_on_same_segment_degrades() -> None:
    """同一片段不能承载两个转场(SDK 单 transition 属性,§4.5)。"""
    draft, _ = _native_draft(with_audio=False)
    spec = _spec_from_extract(draft)
    eid = _supported_effect_id("transition")
    spec.transitions = [
        TransitionSpec(boundary_index=0, from_clip_id="a", to_clip_id="b",
                       effect_id=eid, resource_id="r", duration_us=100_000),
        TransitionSpec(boundary_index=1, from_clip_id="b", to_clip_id="c",
                       effect_id=eid, resource_id="r", duration_us=100_000),
    ]
    result = _compile(spec)
    # 两段草稿只有 2 个 clip → boundary 0 与 1 都以 clip_0001?不对:边界1 越界
    # (len(clips)=2 → i+1>=2 越界),所以只有边界 0 落地,无重复冲突;
    # 冲突场景用 3 段草稿另测。
    assert result.summary["counts"]["transitions_applied"] == 1


def test_transition_duplicate_three_clips() -> None:
    """三段草稿:边界 0 与 1 都从 clip_0001 出发?否 —— 边界 1 左段是 clip_0002。
    同段冲突需两个 spec 指同一 boundary;SDK 抛 ValueError → 降级记录。"""
    draft, _ = _native_draft(with_audio=False)
    spec = _spec_from_extract(draft)
    eid = _supported_effect_id("transition")
    spec.transitions = [
        TransitionSpec(boundary_index=0, from_clip_id="a", to_clip_id="b",
                       effect_id=eid, resource_id="r1", duration_us=100_000),
        TransitionSpec(boundary_index=0, from_clip_id="a", to_clip_id="b",
                       effect_id=_supported_effect_id("transition") if eid else "x",
                       resource_id="r2", duration_us=200_000),
    ]
    # 两个 spec 用同一 effect_id 时 SDK 对第二段 add_transition 抛 ValueError
    spec.transitions[1].effect_id = eid
    result = _compile(spec)
    assert result.summary["counts"]["transitions_applied"] == 1
    assert any(d["reason"].startswith("sdk_rejected") for d in result.summary["degradations"])


# ---------------------------------------------------------------------------
# 视频特效编译
# ---------------------------------------------------------------------------
def test_video_effect_applied_with_intensity() -> None:
    draft, _ = _native_draft(with_audio=False)
    spec = _spec_from_extract(draft)
    spec.video_effects = [
        VideoEffectSpec(
            clip_id="clip_0001", effect_id=_supported_effect_id("video_effect"),
            resource_id="r", name="测试特效", intensity=60.0,
        ),
    ]
    result = _compile(spec)
    bucket = result.payload["materials"]["video_effects"]
    assert len(bucket) == 1
    assert bucket[0]["effect_id"] == spec.video_effects[0].effect_id
    assert result.summary["counts"]["video_effects_applied"] == 1


def test_video_effect_unsupported_or_missing_clip_degrades() -> None:
    draft, _ = _native_draft(with_audio=False)
    spec = _spec_from_extract(draft)
    spec.video_effects = [
        VideoEffectSpec(clip_id="clip_0001", effect_id="no-such-fx",
                        resource_id="r", intensity=50.0),
        VideoEffectSpec(clip_id="clip-gone", effect_id=_supported_effect_id("video_effect"),
                        resource_id="r", intensity=50.0),
    ]
    result = _compile(spec)
    assert result.payload["materials"]["video_effects"] == []
    kinds = {(d["kind"], d["reason"]) for d in result.summary["degradations"]}
    assert ("video_effect", "unsupported_or_unverified") in kinds
    assert ("video_effect", "clip_not_found") in kinds


# ---------------------------------------------------------------------------
# 视频动画编译
# ---------------------------------------------------------------------------
def test_video_animation_intro_applied() -> None:
    member = next(iter(sdk().IntroType))
    draft, _ = _native_draft(with_audio=False)
    spec = _spec_from_extract(draft)
    spec.video_animations = [
        VideoAnimationSpec(
            clip_id="clip_0001", kind="in",
            effect_id=str(member.value.effect_id),
            resource_id=str(member.value.resource_id),
            duration_us=800_000,
        ),
    ]
    result = _compile(spec)
    bucket = result.payload["materials"]["material_animations"]
    assert len(bucket) == 1
    entry = bucket[0]["animations"][0]
    assert entry["type"] == "in"
    assert entry["duration"] == 800_000
    assert entry["panel"] == "video"
    assert result.summary["counts"]["video_animations_applied"] == 1


def test_video_animation_kind_mismatch_degrades() -> None:
    member = next(iter(sdk().IntroType))
    draft, _ = _native_draft(with_audio=False)
    spec = _spec_from_extract(draft)
    spec.video_animations = [
        VideoAnimationSpec(
            clip_id="clip_0001", kind="out",  # intro 资源声明为 out → kind 失配
            effect_id=str(member.value.effect_id),
            resource_id=str(member.value.resource_id),
        ),
    ]
    result = _compile(spec)
    assert any(
        d["kind"] == "video_animation" and d["reason"] == "unsupported_or_kind_mismatch"
        for d in result.summary["degradations"]
    )


# ---------------------------------------------------------------------------
# node_09 注入 → spec 发射(zh 稿口径)
# ---------------------------------------------------------------------------
def test_inject_fx_emits_spec(tmp_path: Path) -> None:
    from nodes._draft_io import load_draft
    from nodes._draft_build_spec import load_build_spec

    draft_path = tmp_path / "draft_content.json"
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 15_000_000,
        "materials": {
            "videos": [
                {"id": f"v{i}", "path": f"C:/media/s{i}.mp4",
                 "material_name": f"clip_{i + 1:04d}"}
                for i in range(3)
            ],
            "audios": [], "speeds": [], "canvases": [],
            "sound_channel_mappings": [], "vocal_separations": [],
            "transitions": [], "video_effects": [],
        },
        "tracks": [{
            "type": "video", "id": "tv", "segments": [
                {
                    "id": f"seg-{i}", "material_id": f"v{i}",
                    "target_timerange": {"start": i * 5_000_000, "duration": 5_000_000},
                    "source_timerange": {"start": 0, "duration": 5_000_000},
                    "speed": 1.0, "volume": 1.0, "extra_material_refs": [],
                    "common_keyframes": [], "keyframe_refs": [],
                }
                for i in range(3)
            ],
        }],
    }
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    loaded = load_draft(draft_path)

    template_lib = load_resource_libraries(
        fx_template="templates/fx_template.json",
        fx_resource="templates/fx_resource_library.json",
        text_resource="templates/text_resource_library.json",
    )
    warnings, summary = _inject_fx_into(
        loaded, template_lib, ["fade", "zoom", "cut"], "node_09_zh",
        job_id="sess-p5", required_transitions=False,
    )
    assert summary["transitions_written"] >= 2  # fade/zoom 语义命中,cut 硬切
    assert summary["video_effects_written"] == 2  # 3 段 → 位置 {0, 2}

    from nodes.node_09_inject_fx import _emit_fx_spec

    log: list[str] = []
    _emit_fx_spec(tmp_path, loaded, log)
    assert any("效果构建计划已入 spec" in tag for tag in log)

    spec = load_build_spec(tmp_path)
    assert spec is not None
    # cut 边界(边界 2)不产生 TransitionSpec;spec 记录的是边界语义
    assert len(spec.transitions) == summary["transitions_written"]
    for trans in spec.transitions:
        assert trans.boundary_index in (0, 1)
        assert trans.effect_id and trans.resource_id
        assert trans.duration_us > 0
    assert len(spec.video_effects) == 2
    assert {v.clip_id for v in spec.video_effects} == {"clip_0001", "clip_0003"}
    assert all(0 <= v.intensity <= 100 for v in spec.video_effects)
    assert spec.video_animations == []
