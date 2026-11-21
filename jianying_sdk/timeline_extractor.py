"""TimelineSnapshotExtractor:读取当前草稿并提取规范化时间线(迁移设计文档 §7;阶段2)。

node_07 之后(node_11 编译前)读取当前有效草稿(可能已经人工调序 / speed-fit /
配音重写),把视频/音频段标准化为 :class:`jianying_sdk.spec.TimelineSection`:

- **顺序以实际时间线为准**:按 ``target_timerange.start`` 排序,禁止按素材数组
  顺序推断(§7.3);
- 每段标准化为 path / source range / target range / speed / volume / 顺序 /
  clip transform(缩放、位置、旋转、翻转、透明度)与素材 crop(8 角点,
  与 SDK ``CropSettings`` 同形);
- AI 转场 MP4 是普通视频段;``canonical_plan`` 提供时恢复 ``ai_transition_pair``
  溯源(§6.5),缺失时降级为普通段(审计信息不阻断);
- **SDK 无法忠实重建的人工高级特征**写进 ``section.blocked_features``(§7.2:
  关键帧 / 蒙版类素材桶 / 未识别段引用 / 多视频轨 / 文本贴纸轨残留),
  编译器见非空即拒绝重建 —— 绝不静默生成丢失人工成果的新草稿。

提取器是**纯读**操作;唯一落盘行为由调用方(nodes/_draft_build_spec.py)负责。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jianying_sdk.spec import AudioClipSpec, CanvasSpec, TimelineClipSpec, TimelineSection
from nodes._draft_io import find_video_track

#: 我方链路产生的素材桶(非空不阻断;SDK 编译器全部可重建或由计划节重建)
_KNOWN_BUCKETS: frozenset[str] = frozenset({
    "videos", "audios", "texts", "transitions", "video_effects", "stickers",
    "material_animations", "audio_fades", "speeds", "canvases",
    "sound_channel_mappings", "vocal_separations", "effects",
})

#: 人工高级素材桶(非空 → blocked;SDK 无法按语义重建,§7.2)
_ADVANCED_BUCKETS: tuple[str, ...] = (
    "masks", "chromas", "color_curves", "hsl", "log_color_wheels",
    "primary_color_wheels", "realtime_denoises", "manual_deformations",
    "green_screens", "handwrites", "shapes", "smart_crops", "smart_relights",
    "ai_translates", "audio_balances", "audio_effects", "beats", "flowers",
    "loudnesses", "placeholders", "plugin_effects", "digital_humans",
    "images", "drafts", "material_colors", "multi_language_refs",
)

#: 我方视频段伴随素材桶(段上 extra_material_refs 只允许指向这些桶)
_COMPANION_BUCKETS: tuple[str, ...] = (
    "speeds", "canvases", "sound_channel_mappings", "vocal_separations",
)

_CROP_DEFAULT: dict[str, float] = {
    "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0,
    "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
    "upper_right_x": 1.0, "upper_right_y": 0.0,
}


@dataclass
class ExtractReport:
    """提取结果:规范化时间线 + 非阻断提示(warnings 进 status_log)。"""

    section: TimelineSection
    warnings: list[str] = field(default_factory=list)


def _canonical_clip_index(canonical_plan: dict | None) -> dict[str, dict]:
    """canonical plan clips 按 clip_id 索引(dict 形态;非法条目忽略)。"""
    out: dict[str, dict] = {}
    if not isinstance(canonical_plan, dict):
        return out
    for clip in canonical_plan.get("clips") or []:
        if isinstance(clip, dict) and clip.get("clip_id"):
            out[str(clip["clip_id"])] = clip
    return out


def _parse_ai_pair(raw: Any) -> dict[str, str] | None:
    """canonical ``ai_transition_pair``(形如 ``clip_0001->clip_0003``)→ 结构化。"""
    text = str(raw or "").strip()
    if "->" not in text:
        return None
    src, _, dst = text.partition("->")
    src, dst = src.strip(), dst.strip()
    if not src or not dst:
        return None
    return {"from_clip_id": src, "to_clip_id": dst}


def _load_sidecar(draft_dir: Path | None) -> dict:
    """读草稿目录的原生转换 sidecar(缺失/损坏返回空 dict)。"""
    from storyline.native_draft import NATIVE_DRAFT_SIDECAR_NAME

    if draft_dir is None:
        return {}
    path = Path(draft_dir) / NATIVE_DRAFT_SIDECAR_NAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _audio_role_resolver(sidecar: dict):
    """构造 native audio material id → role 解析器(sidecar 可用时精确,否则 manual)。"""
    old_role_by_new_id: dict[str, str] = {}
    id_map = (sidecar.get("id_map") or {}).get("material") or {}
    src_audios = ((sidecar.get("source_draft") or {}).get("materials") or {}).get("audios") or []
    role_by_old_id = {
        str(a.get("id")): str(a.get("role") or "manual")
        for a in src_audios
        if isinstance(a, dict) and a.get("id")
    }
    for old_id, new_id in id_map.items():
        role = role_by_old_id.get(str(old_id))
        if role:
            old_role_by_new_id[str(new_id)] = role

    def resolve(native_material_id: str) -> str:
        role = old_role_by_new_id.get(str(native_material_id))
        return role if role in ("bgm", "voiceover") else "manual"

    return resolve


def _blocked(kind: str, **detail: Any) -> dict[str, Any]:
    return {"kind": kind, **detail}


def extract_timeline_snapshot(
    draft: dict,
    *,
    draft_dir: Path | str | None = None,
    canonical_plan: dict | None = None,
    spec_subtitle_ids: list[str] | None = None,
) -> ExtractReport:
    """从当前(原生 schema)草稿提取规范化时间线。

    Args:
        draft: ``draft_content.json`` dict(原生 schema;storyline 4 键旧格式
            会整单 blocked —— 编译器拒绝,需先经 storyline_to_native)。
        draft_dir: 草稿目录(用于读取原生转换 sidecar 恢复音频 role;可选)。
        canonical_plan: ``state["storyline_plan"]``(dict);提供时恢复 AI 转场
            clip 的 ``ai_transition_pair`` 溯源。
        spec_subtitle_ids: spec 已规划的中文/英文字幕 material id;提供时这些
            文本段不视为人工残留(编译时由字幕计划重建)。

    Returns:
        :class:`ExtractReport`(section + warnings;blocked 特征在
        ``section.blocked_features``)。
    """
    warnings: list[str] = []
    blocked: list[dict[str, Any]] = []
    clips: list[TimelineClipSpec] = []
    audio_clips: list[AudioClipSpec] = []

    if not isinstance(draft, dict) or "tracks" not in draft:
        blocked.append(_blocked("non_native_draft", reason="draft 不是原生 schema(缺 tracks)"))
        return ExtractReport(
            section=TimelineSection(
                source_draft_path=str(draft_dir) if draft_dir else None,
                blocked_features=blocked,
            ),
            warnings=warnings,
        )

    materials = draft.get("materials") if isinstance(draft.get("materials"), dict) else {}

    # 1. 人工高级素材桶(§7.2:阻断重建并保留原草稿)
    for bucket in _ADVANCED_BUCKETS:
        entries = materials.get(bucket)
        if isinstance(entries, list) and entries:
            blocked.append(_blocked("manual_advanced_bucket", bucket=bucket, count=len(entries)))

    # 2. 视频轨:多条(含人工加轨)→ blocked;单轨逐段提取
    video_tracks = [
        t for t in (draft.get("tracks") or [])
        if isinstance(t, dict) and t.get("type") == "video"
    ]
    video_segments_all = [s for t in video_tracks for s in (t.get("segments") or []) if isinstance(s, dict)]
    if len(video_tracks) > 1 and video_segments_all:
        blocked.append(_blocked("multiple_video_tracks", count=len(video_tracks)))

    videos_by_id = {
        str(v.get("id")): v
        for v in (materials.get("videos") or [])
        if isinstance(v, dict) and v.get("id")
    }
    known_ref_ids = {
        str(m.get("id"))
        for bucket in _COMPANION_BUCKETS
        for m in (materials.get(bucket) or [])
        if isinstance(m, dict) and m.get("id")
    }
    canonical_index = _canonical_clip_index(canonical_plan)

    if video_tracks:
        track = video_tracks[0] if len(video_tracks) == 1 else find_video_track(draft)
        segments = [s for s in (track.get("segments") or []) if isinstance(s, dict)]
        # 顺序以实际时间线为准(§7.3);start 相同(贴轨)保持数组顺序稳定
        segments_sorted = sorted(
            enumerate(segments),
            key=lambda pair: (int((pair[1].get("target_timerange") or {}).get("start") or 0), pair[0]),
        )
        for array_idx, seg in segments_sorted:
            target = seg.get("target_timerange") or {}
            source = seg.get("source_timerange") or {}
            material_id = str(seg.get("material_id") or "")
            material = videos_by_id.get(material_id)
            if material is None:
                blocked.append(_blocked("dangling_segment_material", segment_id=str(seg.get("id")), material_id=material_id))
                continue
            # 段引用核对:extra_material_refs 必须全部落在伴随桶
            for ref in seg.get("extra_material_refs") or []:
                if str(ref) not in known_ref_ids:
                    blocked.append(_blocked(
                        "unrecognized_segment_ref",
                        segment_id=str(seg.get("id")), ref=str(ref),
                    ))
            if seg.get("common_keyframes"):
                blocked.append(_blocked("segment_keyframes", segment_id=str(seg.get("id"))))
            if seg.get("keyframe_refs"):
                blocked.append(_blocked("segment_keyframe_refs", segment_id=str(seg.get("id"))))

            # clip transform(SDK ClipSettings 同名字段直传)
            transform = seg.get("clip") if isinstance(seg.get("clip"), dict) else {}
            # 素材 crop(非全幅默认才保留;与 SDK CropSettings 同形)
            crop = material.get("crop") if isinstance(material.get("crop"), dict) else None
            crop_non_default = bool(crop) and {
                k: float(crop.get(k, _CROP_DEFAULT.get(k, 0.0)))
                for k in _CROP_DEFAULT
            } != _CROP_DEFAULT

            clip_settings: dict[str, Any] = {}
            if transform:
                clip_settings["transform"] = transform
            if crop_non_default:
                clip_settings["crop"] = crop

            clip_id = str(material.get("material_name") or "") or str(seg.get("id") or "")
            role = "main"
            pair: dict[str, str] | None = None
            canonical_clip = canonical_index.get(clip_id)
            if canonical_clip is not None:
                pair = _parse_ai_pair(canonical_clip.get("ai_transition_pair"))
                if pair:
                    role = "ai_transition"
            clips.append(TimelineClipSpec(
                clip_id=clip_id or f"clip-{array_idx + 1}",
                source_path=str(material.get("path") or ""),
                source_in_us=int(source.get("start") or 0),
                source_duration_us=int(source.get("duration") or 0),
                target_start_us=int(target.get("start") or 0),
                target_duration_us=int(target.get("duration") or 0),
                speed=float(seg.get("speed") or 1.0),
                volume=float(seg.get("volume") or 1.0),
                muted=bool(seg.get("muted") or False),
                order=len(clips),
                role=role,
                ai_transition_pair=pair,
                clip_settings=clip_settings or None,
            ))

    # 3. 音频轨(BGM / 配音 / 人工;合并进 spec.audio_clips,编译铺单条音轨)
    sidecar = _load_sidecar(Path(draft_dir)) if draft_dir else {}
    resolve_role = _audio_role_resolver(sidecar)
    audios_by_id = {
        str(a.get("id")): a
        for a in (materials.get("audios") or [])
        if isinstance(a, dict) and a.get("id")
    }
    fades_by_id = {
        str(f.get("id")): f
        for f in (materials.get("audio_fades") or [])
        if isinstance(f, dict) and f.get("id")
    }
    audio_ref_ids = set(fades_by_id) | known_ref_ids
    for track in draft.get("tracks") or []:
        if not isinstance(track, dict) or track.get("type") != "audio":
            continue
        for seg in track.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            target = seg.get("target_timerange") or {}
            source = seg.get("source_timerange") or {}
            material_id = str(seg.get("material_id") or "")
            material = audios_by_id.get(material_id)
            if material is None:
                blocked.append(_blocked("dangling_audio_material", segment_id=str(seg.get("id")), material_id=material_id))
                continue
            fade_in_us = fade_out_us = 0
            for ref in seg.get("extra_material_refs") or []:
                fade = fades_by_id.get(str(ref))
                if fade is not None:
                    fade_in_us = int(fade.get("fade_in_duration") or 0)
                    fade_out_us = int(fade.get("fade_out_duration") or 0)
                elif str(ref) not in audio_ref_ids:
                    blocked.append(_blocked(
                        "unrecognized_audio_ref",
                        segment_id=str(seg.get("id")), ref=str(ref),
                    ))
            path = str(material.get("path") or "")
            audio_clips.append(AudioClipSpec(
                audio_id=str(seg.get("id") or material_id),
                source_path=path,
                target_start_us=int(target.get("start") or 0),
                target_duration_us=int(target.get("duration") or 0),
                source_start_us=int(source.get("start") or 0),
                source_duration_us=int(source.get("duration") or 0),
                speed=float(seg.get("speed") or 1.0),
                volume=float(seg.get("volume") or 1.0),
                role=resolve_role(material_id),
                fade_in_us=fade_in_us,
                fade_out_us=fade_out_us,
                name=str(material.get("name") or material.get("material_name") or ""),
            ))

    # 4. 文本 / 贴纸轨残留:不在 spec 计划内的文本段视为人工成果 → 阻断
    subtitle_ids = {str(x) for x in (spec_subtitle_ids or [])}
    for track in draft.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        ttype = str(track.get("type") or "")
        segments = [s for s in (track.get("segments") or []) if isinstance(s, dict)]
        if not segments:
            continue
        if ttype == "text":
            manual = [s for s in segments if str(s.get("material_id") or "") not in subtitle_ids]
            if manual:
                blocked.append(_blocked(
                    "manual_text_track",
                    count=len(manual),
                    segment_ids=[str(s.get("id")) for s in manual[:10]],
                ))
        elif ttype in ("sticker", "effect", "filter"):
            blocked.append(_blocked(f"manual_{ttype}_track", count=len(segments)))

    total_duration_us = max(
        [c.target_start_us + c.target_duration_us for c in clips]
        + [a.target_start_us + a.target_duration_us for a in audio_clips]
        + [0]
    )
    section = TimelineSection(
        source_draft_path=str(draft_dir) if draft_dir else None,
        clips=clips,
        audio_clips=audio_clips,
        blocked_features=blocked,
    )
    if blocked:
        warnings.append(
            f"[sdk-extract] 检出 {len(blocked)} 项 SDK 无法忠实重建的特征,"
            "编译将被阻断(见 spec.timeline.blocked_features)"
        )
    return ExtractReport(section=section, warnings=warnings)


def canvas_of(draft: dict) -> CanvasSpec:
    """从草稿读画布(缺省回退项目 CANVAS_WIDTH/HEIGHT,与 node_05 同源)。"""
    import config

    canvas = draft.get("canvas_config") if isinstance(draft.get("canvas_config"), dict) else {}
    return CanvasSpec(
        width=int(canvas.get("width") or config.CANVAS_WIDTH),
        height=int(canvas.get("height") or config.CANVAS_HEIGHT),
        fps=int(float(draft.get("fps") or 30.0)),
    )


__all__ = ["ExtractReport", "canvas_of", "extract_timeline_snapshot"]
