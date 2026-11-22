"""第10期阶段2 单测:TimelineSnapshotExtractor + 片段编译 + spec 读写 + node_07 接线。

覆盖迁移设计文档 §9 阶段2 验收:
- 时间线顺序以实际时间线时间为准(数组乱序也能按 target start 排序);
- 同一快照 SDK 重建前后:镜头顺序、源范围、目标范围、速度、画面位置、总时长一致;
- SDK 无法表达的人工高级特征 → blocked_features,编译拒绝(§7.2 不静默丢弃);
- node_07 写基线 spec(成功/放行两条路径),spec 读写幂等。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jianying_sdk.compiler import (
    TRACK_AUDIO,
    TRACK_VIDEO_MAIN,
    UnsupportedManualFeatureError,
    compile_spec,
)
from jianying_sdk.spec import AudioClipSpec, DraftBuildSpec, TimelineClipSpec
from jianying_sdk.timeline_extractor import canvas_of, extract_timeline_snapshot
from nodes._draft_build_spec import (
    load_build_spec,
    replace_spec_sections,
    spec_path_of,
)


# ---------------------------------------------------------------------------
# 测试替身:跳过 MediaInfo 探测的 SDK 素材子类(阶段2 单测不依赖真实媒体)
# ---------------------------------------------------------------------------
from jianying_sdk._sdk_loader import ensure_sdk_on_path

ensure_sdk_on_path()
from pyJianYingDraft.local_materials import AudioMaterial, CropSettings, VideoMaterial


class StubVideoMaterial(VideoMaterial):
    """形态与 VideoMaterial 一致,但跳过文件探测(时长/尺寸用固定值)。"""

    def __init__(self, path: str, crop_settings: CropSettings | None = None):
        self.material_name = Path(path).name
        self.material_id = "stubvid-" + path.replace("\\", "/").split("/")[-1].replace(".", "-")
        self.path = path
        self.crop_settings = crop_settings or CropSettings()
        self.local_material_id = ""
        self.material_type = "video"
        self.duration = 30_000_000
        self.width = 1080
        self.height = 1920

    def export_json(self) -> dict:
        return {
            "audio_fade": None,
            "category_id": "",
            "category_name": "local",
            "check_flag": 63487,
            "crop": self.crop_settings.export_json(),
            "crop_ratio": "free",
            "crop_scale": 1.0,
            "duration": self.duration,
            "height": self.height,
            "id": self.material_id,
            "local_material_id": self.local_material_id,
            "material_id": self.material_id,
            "material_name": self.material_name,
            "media_path": "",
            "path": self.path,
            "type": self.material_type,
            "width": self.width,
        }


class StubAudioMaterial(AudioMaterial):
    def __init__(self, path: str, material_name: str | None = None):
        self.material_name = material_name or Path(path).name
        self.material_id = "stubaud-" + Path(path).name.replace(".", "-")
        self.path = path
        self.duration = 60_000_000

    def export_json(self) -> dict:
        return {
            "category_id": "",
            "category_name": "local",
            "duration": self.duration,
            "id": self.material_id,
            "material_id": self.material_id,
            "material_name": self.material_name,
            "path": self.path,
            "type": "extract_music",
        }


def _video_factory(path: str, crop: dict | None):
    return StubVideoMaterial(path, CropSettings(**crop) if crop else None)


def _audio_factory(path: str):
    return StubAudioMaterial(path)


# ---------------------------------------------------------------------------
# 合成原生草稿(与 storyline/native_draft 转换产物同形态的最小子集)
# ---------------------------------------------------------------------------
def _segment(
    seg_id: str,
    material_id: str,
    target: tuple[int, int],
    source: tuple[int, int],
    *,
    speed: float = 1.0,
    volume: float = 1.0,
    transform: dict | None = None,
    companion_ids: list[str] | None = None,
) -> dict:
    return {
        "id": seg_id,
        "material_id": material_id,
        "target_timerange": {"start": target[0], "duration": target[1]},
        "source_timerange": {"start": source[0], "duration": source[1]},
        "speed": speed,
        "volume": volume,
        "clip": transform if transform is not None else {
            "alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0, "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": 0.0},
        },
        "extra_material_refs": list(companion_ids or []),
        "common_keyframes": [],
        "keyframe_refs": [],
    }


def _companion_materials(seg_id: str, speed: float) -> tuple[dict[str, list[dict]], list[str]]:
    speed_id = f"speed-{seg_id}"
    canvas_id = f"canvas-{seg_id}"
    scm_id = f"scm-{seg_id}"
    vocal_id = f"vocal-{seg_id}"
    mats = {
        "speeds": [{"id": speed_id, "speed": speed, "mode": 0, "type": "speed", "curve_speed": None}],
        "canvases": [{"id": canvas_id, "type": "canvas_color"}],
        "sound_channel_mappings": [{"id": scm_id, "audio_channel_mapping": 0}],
        "vocal_separations": [{"id": vocal_id, "type": "vocal_separation"}],
    }
    return mats, [speed_id, canvas_id, scm_id, vocal_id]


def _native_draft(
    *,
    clips_reversed: bool = False,
    with_audio: bool = True,
    with_fade: bool = False,
) -> tuple[dict, dict]:
    """两段视频 + 可选 BGM 的原生草稿;clips_reversed 模拟人工调序后的数组。"""
    materials: dict = {"videos": [], "audios": [], "speeds": [], "canvases": [],
                       "sound_channel_mappings": [], "vocal_separations": [], "audio_fades": []}
    segments = []
    for idx, (src, tgt, speed) in enumerate([
        ((0, 5_000_000), (0, 5_000_000), 1.0),
        ((2_000_000, 4_000_000), (5_000_000, 2_000_000), 2.0),
    ]):
        seg_id = f"segment-{idx + 1}"
        mats, companion_ids = _companion_materials(seg_id, speed)
        for bucket, entries in mats.items():
            materials[bucket].extend(entries)
        materials["videos"].append({
            "id": f"video-{idx + 1}",
            "path": f"C:/media/source_{idx + 1}.mp4",
            "material_name": f"clip_{idx + 1:04d}",
            "crop": {"lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0,
                     "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
                     "upper_right_x": 1.0, "upper_right_y": 0.0},
        })
        segments.append(_segment(
            seg_id, f"video-{idx + 1}", tgt, src,
            speed=speed, companion_ids=companion_ids,
        ))
    if clips_reversed:
        segments.reverse()
    tracks = [{"type": "video", "id": "track-video-1", "segments": segments}]
    if with_audio:
        fade_id = None
        refs: list[str] = []
        if with_fade:
            fade_id = "audio-fade-1"
            materials["audio_fades"].append({
                "id": fade_id, "type": "audio_fade",
                "fade_in_duration": 500_000, "fade_out_duration": 800_000,
                "fade_in_type": "linear", "fade_out_type": "linear",
            })
            refs = [fade_id]
        materials["audios"].append({"id": "bgm-1", "path": "C:/media/bgm.mp3", "name": "bgm.mp3"})
        tracks.append({
            "type": "audio", "id": "track-audio-1",
            "segments": [_segment(
                "audio-seg-1", "bgm-1", (0, 7_000_000), (0, 7_000_000),
                volume=0.4, companion_ids=refs,
            )],
        })
    draft = {
        "canvas_config": {"width": 1080, "height": 1920, "ratio": "original"},
        "fps": 30.0,
        "duration": 7_000_000,
        "materials": materials,
        "tracks": tracks,
    }
    return draft, materials


# ---------------------------------------------------------------------------
# 提取器
# ---------------------------------------------------------------------------
def test_extract_orders_by_timeline_not_array() -> None:
    draft, _ = _native_draft(clips_reversed=True)
    report = extract_timeline_snapshot(draft)
    clips = report.section.clips
    assert [c.clip_id for c in clips] == ["clip_0001", "clip_0002"]
    assert [c.target_start_us for c in clips] == [0, 5_000_000]
    assert clips[1].speed == 2.0
    assert clips[1].source_in_us == 2_000_000
    assert clips[0].source_path == "C:/media/source_1.mp4"
    assert not report.section.blocked_features


def test_extract_ai_transition_pair_from_canonical_plan() -> None:
    draft, _ = _native_draft()
    plan = {
        "clips": [
            {"clip_id": "clip_0001", "ai_transition_pair": None},
            {"clip_id": "clip_0002", "ai_transition_pair": "clip_0001->clip_0002"},
        ]
    }
    report = extract_timeline_snapshot(draft, canonical_plan=plan)
    assert report.section.clips[0].role == "main"
    assert report.section.clips[1].role == "ai_transition"
    assert report.section.clips[1].ai_transition_pair == {
        "from_clip_id": "clip_0001", "to_clip_id": "clip_0002"
    }


def test_extract_audio_clips_with_fade_and_sidecar_role(tmp_path: Path) -> None:
    draft, _ = _native_draft(with_fade=True)
    sidecar = {
        "schema": "autovideo_storyline_source/1",
        "id_map": {"material": {"bgm-old": "bgm-1"}},
        "source_draft": {"materials": {"audios": [{"id": "bgm-old", "role": "bgm"}]}},
    }
    from storyline.native_draft import NATIVE_DRAFT_SIDECAR_NAME

    (tmp_path / NATIVE_DRAFT_SIDECAR_NAME).write_text(
        json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
    )
    report = extract_timeline_snapshot(draft, draft_dir=tmp_path)
    audio = report.section.audio_clips
    assert len(audio) == 1
    assert audio[0].role == "bgm"
    assert audio[0].volume == 0.4
    assert audio[0].fade_in_us == 500_000
    assert audio[0].fade_out_us == 800_000
    assert audio[0].source_path == "C:/media/bgm.mp3"


def test_extract_audio_role_from_voiceover_track_names() -> None:
    """2026-11-22 补:配音 role 按**源轨名**判定(编译稿按 role 分轨落回同名轨)。

    轨名精确匹配优先于 node_05 sidecar 回退;未命名/bgm 轨仍走原解析路径。
    node_12 关卡②/镜像、node_13 音量角色、QA 语言隔离的轨名语义依赖此判定。
    """
    draft, _ = _native_draft()
    draft["tracks"].extend([
        {
            "type": "audio", "id": "track-vo-zh", "name": "Voiceover_zh",
            "segments": [_segment("vo-zh-1", "vo-zh-1", (0, 3_000_000), (0, 3_000_000))],
        },
        {
            "type": "audio", "id": "track-vo-en", "name": "en_voiceover",
            "segments": [_segment("vo-en-1", "vo-en-1", (0, 3_000_000), (0, 3_000_000))],
        },
    ])
    draft["materials"]["audios"].extend([
        {"id": "vo-zh-1", "path": "C:/media/vo_zh.mp3", "name": "vo_zh.mp3"},
        {"id": "vo-en-1", "path": "C:/media/vo_en.mp3", "name": "vo_en.mp3"},
    ])
    report = extract_timeline_snapshot(draft)
    roles = {c.audio_id.split("@")[0]: c.role for c in report.section.audio_clips}
    assert roles["vo_zh"] == "voiceover_zh"
    assert roles["vo_en"] == "voiceover_en"
    assert roles["bgm"] == "manual"


@pytest.mark.parametrize(
    "mutate,kind",
    [
        (lambda d: d["materials"].update(masks=[{"id": "m1"}]), "manual_advanced_bucket"),
        (
            lambda d: d["tracks"][0]["segments"][0]["extra_material_refs"].append("mystery-1"),
            "unrecognized_segment_ref",
        ),
        (
            lambda d: d["tracks"][0]["segments"][0].update(common_keyframes=[{"id": "kf"}]),
            "segment_keyframes",
        ),
        (lambda d: d["tracks"].append({"type": "text", "id": "t-text", "segments": [{"id": "s1", "material_id": "t1"}]}), "manual_text_track"),
        (lambda d: d["tracks"].append({"type": "sticker", "id": "t-st", "segments": [{"id": "s2", "material_id": "st1"}]}), "manual_sticker_track"),
    ],
)
def test_extract_blocks_unrebuildable_manual_features(mutate, kind) -> None:
    draft, _ = _native_draft()
    mutate(draft)
    report = extract_timeline_snapshot(draft)
    kinds = [b["kind"] for b in report.section.blocked_features]
    assert kind in kinds


def test_extract_multiple_video_tracks_blocked() -> None:
    draft, _ = _native_draft()
    draft["tracks"].append({
        "type": "video", "id": "track-video-2",
        "segments": [_segment("v2", "video-1", (0, 1_000_000), (0, 1_000_000))],
    })
    report = extract_timeline_snapshot(draft)
    assert any(b["kind"] == "multiple_video_tracks" for b in report.section.blocked_features)


def test_extract_dangling_material_blocked() -> None:
    draft, _ = _native_draft()
    draft["tracks"][0]["segments"][0]["material_id"] = "video-999"
    report = extract_timeline_snapshot(draft)
    assert any(b["kind"] == "dangling_segment_material" for b in report.section.blocked_features)


def test_extract_tolerates_legacy_effect_refs() -> None:
    """SDK 链转正(2026-11-22):legacy 链挂视频段的转场/特效/贴纸/动画 refs
    全部是我方效果桶条目、可由编译器按 spec 重建 —— 提取放行,不再阻断。"""
    draft, _ = _native_draft(with_audio=False)
    draft["materials"].update({
        "transitions": [{"id": "transition-1", "type": "Transition", "name": "叠化"}],
        "video_effects": [{"id": "vfx-1", "type": "video_effect", "name": "模糊"}],
        "stickers": [{"id": "sticker-1", "resource_id": "1234567890123456789"}],
        "material_animations": [{"id": "anim-1", "type": "animation", "name": "入场"}],
        "texts": [], "effects": [],
    })
    segs = draft["tracks"][0]["segments"]
    segs[0]["extra_material_refs"].extend(["transition-1", "vfx-1"])
    segs[1]["extra_material_refs"].extend(["sticker-1", "anim-1"])
    report = extract_timeline_snapshot(draft)
    assert report.section.blocked_features == []
    assert len(report.section.clips) == 2


def test_extract_still_blocks_unknown_video_refs() -> None:
    """我方效果桶放行后,未知 refs 的阻断语义保持不变(§7.2)。"""
    draft, _ = _native_draft()
    draft["tracks"][0]["segments"][0]["extra_material_refs"].append("manual-uuid-1")
    report = extract_timeline_snapshot(draft)
    kinds = [b["kind"] for b in report.section.blocked_features]
    assert "unrecognized_segment_ref" in kinds


# ---------------------------------------------------------------------------
# node_09b 开场钩子标题的窄范围豁免(2026-11-23 交付 QA 修复)
# ---------------------------------------------------------------------------
def _append_hook_title(
    draft: dict,
    *,
    track_id: str = "track-text-hook",
    track_name: str = "opening_title",
    seg_id: str = "hook-title-seg-1",
    material_id: str = "hook-title-1",
    material_track_name: str = "opening_title",
    include_material: bool = True,
) -> None:
    """往草稿追加 node_09b 形态的开场标题轨(+ 可选 text material)。"""
    if include_material:
        draft["materials"].setdefault("texts", []).append({
            "id": material_id,
            "content": json.dumps({"text": "开场标题"}, ensure_ascii=False),
            "type": "text",
            "track_name": material_track_name,
            "target_timerange": {"start": 0, "duration": 3_000_000},
        })
    draft["tracks"].append({
        "type": "text",
        "id": track_id,
        "name": track_name,
        "segments": [{
            "id": seg_id,
            "material_id": material_id,
            "target_timerange": {"start": 0, "duration": 3_000_000},
        }],
    })


def test_extract_exempts_our_hook_title_segment() -> None:
    """标准 node_09b 形态(轨 id/轨名/段前缀/material 前缀+存在+轨名一致):
    开场标题段不落入 manual_text_track,重编译不再被阻断。"""
    draft, _ = _native_draft()
    _append_hook_title(draft)
    report = extract_timeline_snapshot(draft)
    assert report.section.blocked_features == []


@pytest.mark.parametrize(
    "kwargs,desc",
    [
        ({"track_name": "user_title"}, "hook 轨名被人工改名 → 仍阻断"),
        ({"seg_id": "manual-seg-1"}, "hook 轨上的非 hook 段 id → 仍阻断"),
        ({"material_id": "manual-title-1"}, "hook 轨上非 hook material id → 仍阻断"),
        ({"include_material": False}, "hook 段引用不存在的 material(悬空) → 仍阻断"),
        ({"material_track_name": "Subtitles"}, "material 轨名字段与产物契约不符 → 仍阻断"),
        ({"track_id": "track-text-other"}, "非 hook 轨道 id → 仍阻断"),
    ],
)
def test_extract_still_blocks_mismatched_hook_identity(kwargs: dict, desc: str) -> None:
    """身份五重校验任一不满足都不得豁免:人工文本保护语义不放宽。"""
    draft, _ = _native_draft()
    _append_hook_title(draft, **kwargs)
    report = extract_timeline_snapshot(draft)
    kinds = [b["kind"] for b in report.section.blocked_features]
    assert "manual_text_track" in kinds, desc


def test_extract_exempts_only_hook_among_manual_text() -> None:
    """hook 段与普通人工段并存:只豁免 hook,人工段仍进入 blocked。"""
    draft, _ = _native_draft()
    _append_hook_title(draft)
    draft["tracks"].append({
        "type": "text",
        "id": "track-text-manual",
        "name": "手工字幕",
        "segments": [{
            "id": "user-seg-9",
            "material_id": "user-material-9",
            "target_timerange": {"start": 5_000_000, "duration": 2_000_000},
        }],
    })
    report = extract_timeline_snapshot(draft)
    manual_blocks = [
        b for b in report.section.blocked_features if b["kind"] == "manual_text_track"
    ]
    assert len(manual_blocks) == 1
    assert manual_blocks[0]["segment_ids"] == ["user-seg-9"]


def test_canvas_of_fallback() -> None:
    draft, _ = _native_draft()
    assert canvas_of(draft).fps == 30
    assert canvas_of({}).width == 1080


# ---------------------------------------------------------------------------
# 编译:片段 + 音频 + 重建不变量
# ---------------------------------------------------------------------------
def _spec_from_extract(draft: dict) -> DraftBuildSpec:
    report = extract_timeline_snapshot(draft)
    spec = DraftBuildSpec(job_id="phase2", language="zh", canvas=canvas_of(draft))
    spec.timeline = report.section
    return spec


def _payload_video_segments(payload: dict) -> list[dict]:
    video_track = next(t for t in payload["tracks"] if t["type"] == "video")
    return video_track["segments"]


def test_compile_clips_and_audio_shape() -> None:
    draft, _ = _native_draft()
    spec = _spec_from_extract(draft)
    result = compile_spec(spec, video_material_factory=_video_factory, audio_material_factory=_audio_factory)
    payload = result.payload

    tracks = {t["name"]: t for t in payload["tracks"]}
    assert set(tracks) == {TRACK_VIDEO_MAIN, TRACK_AUDIO}
    segs = tracks[TRACK_VIDEO_MAIN]["segments"]
    assert len(segs) == 2
    # 顺序与目标区间一致
    assert [s["target_timerange"]["start"] for s in segs] == [0, 5_000_000]
    assert [s["target_timerange"]["duration"] for s in segs] == [5_000_000, 2_000_000]
    assert [s["source_timerange"]["start"] for s in segs] == [0, 2_000_000]
    # 速度 = 源跨度/目标跨度(SDK 由 source/target 派生)
    assert segs[0]["speed"] == pytest.approx(1.0)
    assert segs[1]["speed"] == pytest.approx(2.0)
    # 素材桶登记(add_segment 自动登记,无孤儿素材)
    assert len(payload["materials"]["videos"]) == 2
    assert result.duration_us == 7_000_000
    # 音频轨:单段 BGM + 淡入淡出
    audio_segs = tracks[TRACK_AUDIO]["segments"]
    assert len(audio_segs) == 1
    assert audio_segs[0]["volume"] == 0.4


def test_compile_audio_roles_split_tracks() -> None:
    """2026-11-22 补:配音按 role 分轨落回原轨名,bgm/manual 留 audio_main。

    编译稿若把配音压进单条 audio_main,node_12 关卡②会把配音当 BGM、
    node_13 会按 BGM 压配音音量、QA 语言隔离按轨名检测失效——本用例锁定
    "role → 轨名" 分轨行为。
    """
    draft, _ = _native_draft()
    draft["tracks"].extend([
        {
            "type": "audio", "id": "track-vo-zh", "name": "Voiceover_zh",
            "segments": [_segment("vo-zh-1", "vo-zh-1", (0, 2_000_000), (0, 2_000_000))],
        },
        {
            "type": "audio", "id": "track-vo-en", "name": "en_voiceover",
            "segments": [_segment("vo-en-1", "vo-en-1", (0, 2_000_000), (0, 2_000_000))],
        },
    ])
    draft["materials"]["audios"].extend([
        {"id": "vo-zh-1", "path": "C:/media/vo_zh.mp3", "name": "vo_zh.mp3"},
        {"id": "vo-en-1", "path": "C:/media/vo_en.mp3", "name": "vo_en.mp3"},
    ])
    spec = _spec_from_extract(draft)
    result = compile_spec(spec, video_material_factory=_video_factory, audio_material_factory=_audio_factory)
    tracks = {t["name"]: t for t in result.payload["tracks"]}
    assert set(tracks) == {TRACK_VIDEO_MAIN, TRACK_AUDIO, "Voiceover_zh", "en_voiceover"}
    assert len(tracks["Voiceover_zh"]["segments"]) == 1
    assert len(tracks["en_voiceover"]["segments"]) == 1
    assert len(tracks[TRACK_AUDIO]["segments"]) == 1


def test_rebuild_preserves_invariants() -> None:
    """§阶段2 通过标准:提取 → SDK 重建 → 再提取,不变量逐项相等。"""
    draft, _ = _native_draft(with_fade=True)
    spec = _spec_from_extract(draft)
    result = compile_spec(spec, video_material_factory=_video_factory, audio_material_factory=_audio_factory)

    # SDK 载荷里的 material_name = 文件名(替身口径),与源草稿
    # material_name(clip_id)口径不同;在重建稿副本上用 clip_id 桥接以便对比。
    rebuilt_draft = {
        "canvas_config": dict(result.payload["canvas_config"]),
        "fps": result.payload["fps"],
        "materials": {
            "videos": [
                {**m, "crop": m["crop"]} for m in result.payload["materials"]["videos"]
            ],
            "audios": result.payload["materials"]["audios"],
            "audio_fades": result.payload["materials"]["audio_fades"],
            "speeds": result.payload["materials"]["speeds"],
            "canvases": result.payload["materials"]["canvases"],
            "sound_channel_mappings": result.payload["materials"]["sound_channel_mappings"],
            "vocal_separations": result.payload["materials"]["vocal_separations"],
        },
        "tracks": result.payload["tracks"],
    }
    path_to_clip_id = {c.source_path: c.clip_id for c in spec.timeline.clips}
    for material in rebuilt_draft["materials"]["videos"]:
        clip_id = path_to_clip_id.get(material["path"])
        if clip_id:
            material["material_name"] = clip_id
    report2 = extract_timeline_snapshot(rebuilt_draft)
    assert not report2.section.blocked_features, report2.section.blocked_features

    by_id1 = {c.clip_id: c for c in spec.timeline.clips}
    by_id2 = {c.clip_id: c for c in report2.section.clips}
    assert set(by_id1) == set(by_id2)
    for clip_id, c1 in by_id1.items():
        c2 = by_id2[clip_id]
        assert (c2.source_in_us, c2.source_duration_us) == (c1.source_in_us, c1.source_duration_us)
        assert (c2.target_start_us, c2.target_duration_us) == (c1.target_start_us, c1.target_duration_us)
        assert c2.speed == pytest.approx(c1.speed)
        assert c2.volume == pytest.approx(c1.volume)
        assert c2.source_path == c1.source_path
        assert c2.order == c1.order
    # 总时长不变
    assert result.duration_us == max(
        c.target_start_us + c.target_duration_us for c in spec.timeline.clips
    )


def test_compile_transform_passthrough() -> None:
    draft, _ = _native_draft()
    seg = draft["tracks"][0]["segments"][0]
    seg["clip"] = {
        "alpha": 0.9, "flip": {"horizontal": True, "vertical": False},
        "rotation": 45.0, "scale": {"x": 1.2, "y": 0.8},
        "transform": {"x": 0.1, "y": -0.2},
    }
    spec = _spec_from_extract(draft)
    result = compile_spec(spec, video_material_factory=_video_factory, audio_material_factory=_audio_factory)
    seg_out = _payload_video_segments(result.payload)[0]
    clip = seg_out["clip"]
    assert clip["alpha"] == 0.9
    assert clip["flip"]["horizontal"] is True
    assert clip["rotation"] == 45.0
    assert clip["scale"] == {"x": 1.2, "y": 0.8}
    assert clip["transform"] == {"x": 0.1, "y": -0.2}


def test_compile_blocked_features_rejected() -> None:
    draft, _ = _native_draft()
    draft["materials"]["masks"] = [{"id": "m1"}]
    spec = _spec_from_extract(draft)
    with pytest.raises(UnsupportedManualFeatureError):
        compile_spec(spec, video_material_factory=_video_factory, audio_material_factory=_audio_factory)


def test_compile_fingerprint_changes_with_clips() -> None:
    draft, _ = _native_draft()
    spec = _spec_from_extract(draft)
    fp1 = spec.model_dump()
    spec.timeline.clips.append(
        TimelineClipSpec(
            clip_id="extra", source_path="C:/media/x.mp4",
            source_in_us=0, source_duration_us=1,
            target_start_us=0, target_duration_us=1, order=99,
        )
    )
    assert spec.model_dump() != fp1


# ---------------------------------------------------------------------------
# nodes/_draft_build_spec 读写与 node_07 接线
# ---------------------------------------------------------------------------
def test_replace_spec_sections_idempotent(tmp_path: Path) -> None:
    spec1 = replace_spec_sections(tmp_path, job_id="job-1", language="zh")
    assert spec1.job_id == "job-1"
    assert load_build_spec(tmp_path) is not None
    # 时间线段整体替换,重放幂等
    from jianying_sdk.spec import TimelineSection

    section = TimelineSection(clips=[TimelineClipSpec(
        clip_id="c1", source_path="C:/m.mp4", source_in_us=0,
        source_duration_us=1, target_start_us=0, target_duration_us=1, order=0,
    )])
    spec2 = replace_spec_sections(tmp_path, timeline=section)
    spec3 = replace_spec_sections(tmp_path, timeline=section)
    assert spec2.timeline.clips[0].clip_id == "c1"
    assert spec3.model_dump() == spec2.model_dump()
    # 不动的段原样保留(None = 不拥有)
    assert spec3.job_id == "job-1"


def test_load_build_spec_missing_or_corrupt(tmp_path: Path) -> None:
    assert load_build_spec(tmp_path) is None
    spec_path_of(tmp_path).write_text("{not json", encoding="utf-8")
    assert load_build_spec(tmp_path) is None


def test_node_07_writes_build_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """node_07 speed-fit 写回后同时落 DraftBuildSpec 基线(两条路径)。"""
    from nodes.node_07_speed_fit import speed_fit

    draft, _ = _native_draft(with_audio=False)
    # 目标 35s > 现有 7s:触发"未达标压缩→重试"路径也要能落 spec;
    # 这里直接给达标场景(7s < 35s)走放行路径。
    draft_path = tmp_path / "draft_content.json"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    state = {
        "draft_path": str(draft_path),
        "retry_counts": {},
        "status_log": [],
        "error_log": [],
        "session_id": "sess-phase2",
    }
    out = speed_fit(state)
    assert "node_07_build_spec_updated:clips=2,audio=0,blocked=0" in out["status_log"]
    spec = load_build_spec(tmp_path)
    assert spec is not None
    assert spec.job_id == "sess-phase2"
    assert len(spec.timeline.clips) == 2
    assert spec.timeline.clips[1].speed == 2.0


def test_node_07_spec_failure_degrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """spec 快照失败(提取异常)不阻断 node_07 放行路径。"""
    from nodes.node_07_speed_fit import speed_fit

    draft, _ = _native_draft(with_audio=False)
    draft_path = tmp_path / "draft_content.json"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    state = {"draft_path": str(draft_path), "retry_counts": {}, "status_log": [], "error_log": []}

    import jianying_sdk.timeline_extractor as te

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(te, "extract_timeline_snapshot", _boom)
    out = speed_fit(state)
    assert any("构建说明快照失败" in tag for tag in out["status_log"])
    assert out.get("snapshot2_path")  # 放行路径照常产快照②


# ---------------------------------------------------------------------------
# 2026-11-23 SegmentOverlap 修复(console-3e866b87d5d6):同轨音频重叠预检
# ---------------------------------------------------------------------------
def _with_zh_voiceover(draft: dict, segments: list[dict]) -> dict:
    draft["tracks"].append(
        {"type": "audio", "id": "track-vo-zh", "name": "Voiceover_zh", "segments": segments}
    )
    draft["materials"]["audios"].extend([
        {"id": "vo-zh-1", "path": "C:/media/vo_zh_1.wav", "name": "vo_zh_1.wav"},
        {"id": "vo-zh-2", "path": "C:/media/vo_zh_2.wav", "name": "vo_zh_2.wav"},
    ])
    return draft


def test_compile_voiceover_overlap_raises_located_error() -> None:
    """同轨配音重叠 → 编译前预检抛 AudioClipOverlapError(仍是 ValueError),
    报错带轨名/重叠毫秒/上游提示,不再是 SDK 裸 SegmentOverlap。"""
    from jianying_sdk.compiler import AudioClipOverlapError

    draft, _ = _native_draft()
    _with_zh_voiceover(draft, [
        _segment("vo-zh-1", "vo-zh-1", (0, 3_314_000), (0, 3_314_000)),
        _segment("vo-zh-2", "vo-zh-2", (3_000_000, 2_000_000), (0, 2_000_000)),
    ])
    spec = _spec_from_extract(draft)
    assert not spec.timeline.blocked_features  # 重叠不是人工特征,提取不阻断
    with pytest.raises(AudioClipOverlapError) as exc:
        compile_spec(spec, video_material_factory=_video_factory, audio_material_factory=_audio_factory)
    assert isinstance(exc.value, ValueError)
    msg = str(exc.value)
    assert "Voiceover_zh" in msg
    assert "314ms" in msg
    assert "node_08" in msg


def test_compile_voiceover_abutting_and_sped_up_ok() -> None:
    """首尾相接不算重叠;node_08 加速塞入的配音段(源 3.2s → 目标 3.0s)可编译,
    SDK 由 source/target 派生同一速度。"""
    draft, _ = _native_draft()
    _with_zh_voiceover(draft, [
        _segment("vo-zh-1", "vo-zh-1", (0, 3_000_000), (0, 3_200_000), speed=3.2 / 3.0),
        _segment("vo-zh-2", "vo-zh-2", (3_000_000, 2_000_000), (0, 2_000_000)),
    ])
    spec = _spec_from_extract(draft)
    result = compile_spec(spec, video_material_factory=_video_factory, audio_material_factory=_audio_factory)
    tracks = {t["name"]: t for t in result.payload["tracks"]}
    vo_segs = sorted(
        tracks["Voiceover_zh"]["segments"], key=lambda s: s["target_timerange"]["start"]
    )
    assert len(vo_segs) == 2
    assert vo_segs[0]["target_timerange"] == {"start": 0, "duration": 3_000_000}
    assert vo_segs[0]["source_timerange"]["duration"] == 3_200_000
    assert vo_segs[0]["speed"] == pytest.approx(3.2 / 3.0)
