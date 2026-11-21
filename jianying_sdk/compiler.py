"""DraftBuildSpec → pyJianYingDraft ScriptFile 统一编译器(迁移设计文档 §5;阶段1/2)。

编译模式(§0 约束 A):**读取时间线 → DraftBuildSpec → 空白 SDK 草稿完整重建
→ 安全发布**。编译器在一次构建里创建所有 SDK 对象与受控轨道:

- 受控轨道(video_main / 音频轨 / subtitles / stickers)按需由 ``append_track``
  新建;**绝不**在模板导入轨道上追加任何效果(§0 约束 A);
- 原生转场挂到边界的**前一片段**(``segments[i].add_transition``,SDK 语义;
  §4.5/§6.3 —— 旧实现挂后一段是行为差异,编译器禁止沿用);
- AI 转场 MP4 是普通 ``VideoMaterial`` + ``VideoSegment`` 片段(§0 约束 B),
  绝不伪装成 TransitionType;
- ``timeline.blocked_features`` 非空 → 抛 :class:`UnsupportedManualFeatureError`
  暂停编译并保留原草稿(§7.2,不静默生成丢失人工成果的重建稿);
- 指纹一致跳过编译(:func:`jianying_sdk.fingerprint.needs_compile`);指纹变化
  从空白草稿完整重建,无增量 upsert(§5.3)。

阶段1 交付:骨架 + 无效果最小草稿;阶段2 交付:时间线片段(视频+音频)编译。
字幕/文本装饰/视频效果/贴纸/转场在阶段3-6 逐段接入。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from jianying_sdk._sdk_loader import sdk
from jianying_sdk.fingerprint import compute_build_fingerprint
from jianying_sdk.publisher import validate_sdk_payload
from jianying_sdk.spec import AudioClipSpec, DraftBuildSpec, TimelineClipSpec

#: 编译器版本(写入 spec.compile.compiler_version 与摘要;契约变更须 bump)
COMPILER_VERSION = "pyjy-adapter-v1"

#: 受控轨道名(§8.1;轨道名同时出现在草稿 JSON 里,便于人工识别我方轨道)
TRACK_VIDEO_MAIN = "video_main"
TRACK_AUDIO = "audio_main"
TRACK_SUBTITLES = "subtitles"
TRACK_STICKERS = "stickers"

#: SDK VideoMaterial 工厂签名:(path, crop_dict|None) → material 实例。
#: 生产默认 = SDK VideoMedia 探测;测试注入替身(阶段2 单测不依赖真实媒体)。
VideoMaterialFactory = Callable[[str, dict[str, float] | None], Any]
AudioMaterialFactory = Callable[[str], Any]


class UnsupportedManualFeatureError(RuntimeError):
    """spec 携带 SDK 无法忠实重建的人工高级特征 —— 暂停编译,保留原草稿(§7.2)。"""


class FingerprintUnchangedError(RuntimeError):
    """spec 指纹与上次编译一致 —— 调用方应跳过本次编译/发布。"""


@dataclass
class CompileResult:
    """一次编译的产物与摘要(payload 经 publisher 发布;对象本身不落盘)。"""

    payload: dict[str, Any]
    fingerprint: str
    duration_us: int
    #: 编译摘要(轨道名/各轨道段数/素材桶计数),写回 spec.compile.last_result
    summary: dict[str, Any] = field(default_factory=dict)


def new_script(spec: DraftBuildSpec):
    """按 spec 画布创建空白 ``ScriptFile``(纯内存,不触盘)。"""
    jy = sdk()
    return jy.ScriptFile(
        int(spec.canvas.width),
        int(spec.canvas.height),
        int(spec.canvas.fps),
        maintrack_adsorb=True,
    )


def append_base_tracks(script, spec: DraftBuildSpec) -> dict[str, Any]:
    """按需创建受控轨道(渲染层级 = 追加顺序:视频在底,字幕/贴纸在上)。

    空轨道不创建(剪映对空轨行为未验证,SDK 正常用法也是"加段时建轨");
    字幕/贴纸轨在阶段3/6 有计划数据时才出现。
    """
    jy = sdk()
    refs: dict[str, Any] = {
        "video": script.append_track(jy.TrackSpec(jy.TrackType.video, name=TRACK_VIDEO_MAIN)),
    }
    if spec.timeline.audio_clips:
        refs["audio"] = script.append_track(jy.TrackSpec(jy.TrackType.audio, name=TRACK_AUDIO))
    if spec.subtitles:
        refs["text"] = script.append_track(jy.TrackSpec(jy.TrackType.text, name=TRACK_SUBTITLES))
    if spec.stickers:
        refs["sticker"] = script.append_track(jy.TrackSpec(jy.TrackType.sticker, name=TRACK_STICKERS))
    return refs


def _default_crop() -> dict[str, float]:
    return {
        "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0,
        "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
        "upper_right_x": 1.0, "upper_right_y": 0.0,
    }


def _crop_is_default(crop: dict[str, float] | None) -> bool:
    if not crop:
        return True
    base = _default_crop()
    try:
        return {k: float(crop.get(k, base[k])) for k in base} == base
    except (TypeError, ValueError):
        return False


def _build_video_material(jy, path: str, crop: dict[str, float] | None):
    """按 crop 构造 VideoMaterial(默认全幅 → 无 crop 设置)。"""
    if crop and not _crop_is_default(crop):
        return jy.VideoMaterial(path, crop_settings=jy.CropSettings(**{k: float(crop[k]) for k in crop}))
    return jy.VideoMaterial(path)


def _build_clips(
    script,
    spec: DraftBuildSpec,
    refs: dict[str, Any],
    *,
    video_material_factory: VideoMaterialFactory | None = None,
) -> list[TimelineClipSpec]:
    """时间线视频片段 → SDK VideoSegment(顺序 = spec.order/target start)。"""
    jy = sdk()
    clips = sorted(spec.timeline.clips, key=lambda c: (c.order, c.target_start_us))
    for clip in clips:
        transform = (clip.clip_settings or {}).get("transform") or {}
        crop = (clip.clip_settings or {}).get("crop")
        if video_material_factory is not None:
            material = video_material_factory(clip.source_path, crop)
        else:
            material = _build_video_material(jy, clip.source_path, crop)
        clip_settings = _build_clip_settings(jy, transform)
        segment = jy.VideoSegment(
            material,
            jy.Timerange(int(clip.target_start_us), int(clip.target_duration_us)),
            source_timerange=jy.Timerange(int(clip.source_in_us), int(clip.source_duration_us)),
            volume=float(clip.volume),
            clip_settings=clip_settings,
        )
        script.add_segment(segment, refs["video"])
    return clips


def _build_clip_settings(jy, transform: dict[str, Any]):
    """原生 ``clip.transform`` → SDK ``ClipSettings``(同字段;全默认返回 None)。

    原生嵌套形状 ``{scale:{x,y}, transform:{x,y}, flip:{horizontal,vertical},
    rotation, alpha}`` 与 ClipSettings 平面参数一一对应;缺省字段用 SDK 默认值,
    与全默认 ClipSettings 相等时不设置(保持草稿与旧链路的默认形态一致)。
    """
    scale = transform.get("scale") or {}
    position = transform.get("transform") or {}
    flip = transform.get("flip") or {}
    clip_kwargs: dict[str, Any] = {
        "alpha": float(transform.get("alpha", 1.0)),
        "flip_horizontal": bool(flip.get("horizontal", False)),
        "flip_vertical": bool(flip.get("vertical", False)),
        "rotation": float(transform.get("rotation", 0.0)),
        "scale_x": float(scale.get("x", 1.0)),
        "scale_y": float(scale.get("y", 1.0)),
        "transform_x": float(position.get("x", 0.0)),
        "transform_y": float(position.get("y", 0.0)),
    }
    default = {
        "alpha": 1.0, "flip_horizontal": False, "flip_vertical": False,
        "rotation": 0.0, "scale_x": 1.0, "scale_y": 1.0,
        "transform_x": 0.0, "transform_y": 0.0,
    }
    if clip_kwargs == default:
        return None
    return jy.ClipSettings(**clip_kwargs)


def _build_audio_clips(
    script,
    spec: DraftBuildSpec,
    refs: dict[str, Any],
    *,
    audio_material_factory: AudioMaterialFactory | None = None,
) -> list[AudioClipSpec]:
    """音频片段(BGM/配音/人工)→ SDK AudioSegment(单条音轨,含淡入淡出)。"""
    jy = sdk()
    audio_clips = sorted(spec.timeline.audio_clips, key=lambda a: (a.target_start_us, a.audio_id))
    for audio in audio_clips:
        if audio_material_factory is not None:
            material = audio_material_factory(audio.source_path)
        else:
            material = jy.AudioMaterial(audio.source_path)
        segment = jy.AudioSegment(
            material,
            jy.Timerange(int(audio.target_start_us), int(audio.target_duration_us)),
            source_timerange=(
                jy.Timerange(int(audio.source_start_us), int(audio.source_duration_us))
                if audio.source_duration_us > 0
                else None
            ),
            volume=float(audio.volume),
        )
        if audio.fade_in_us > 0 or audio.fade_out_us > 0:
            segment.add_fade(int(audio.fade_in_us), int(audio.fade_out_us))
        script.add_segment(segment, refs["audio"])
    return audio_clips


def _build_subtitles(script, spec: DraftBuildSpec, refs: dict[str, Any]) -> list:
    """字幕计划 → SDK TextSegment(样式经 style_mapper,折行文本为渲染口径)。"""
    jy = sdk()
    from jianying_sdk.style_mapper import subtitle_text_segment_kwargs

    built = []
    for sub in sorted(spec.subtitles, key=lambda s: (s.start_us, s.subtitle_id)):
        duration_us = int(sub.end_us - sub.start_us)
        if duration_us <= 0:
            continue
        kwargs = subtitle_text_segment_kwargs(sub)
        clip_kwargs = kwargs.pop("clip_settings")
        style_kwargs = kwargs.pop("style")
        border_kwargs = kwargs.pop("border", None)
        text = sub.wrap_content or sub.text
        segment = jy.TextSegment(
            text,
            jy.Timerange(int(sub.start_us), duration_us),
            style=jy.TextStyle(**style_kwargs),
            border=jy.TextBorder(**border_kwargs) if border_kwargs else None,
            clip_settings=jy.ClipSettings(**clip_kwargs),
        )
        script.add_segment(segment, refs["text"])
        built.append(sub)
    return built


def _assert_no_unimplemented_sections(spec: DraftBuildSpec) -> None:
    """阶段边界守卫:装饰/效果/贴纸/转场编译未交付前,非空 spec 不产出半成品。"""
    pending = []
    if spec.text_decorations:
        pending.append("text_decorations(阶段4)")
    if spec.transitions:
        pending.append("transitions(阶段5)")
    if spec.video_effects:
        pending.append("video_effects(阶段5)")
    if spec.video_animations:
        pending.append("video_animations(阶段5)")
    if spec.stickers:
        pending.append("stickers(阶段6)")
    if pending:
        raise NotImplementedError(
            "以下构建段编译尚未交付,请清空对应字段后再试: " + ", ".join(pending)
        )


def compile_spec(
    spec: DraftBuildSpec,
    *,
    force: bool = False,
    video_material_factory: VideoMaterialFactory | None = None,
    audio_material_factory: AudioMaterialFactory | None = None,
) -> CompileResult:
    """把一份 DraftBuildSpec 编译为 SDK 草稿载荷(内存,不触盘)。

    Raises:
        FingerprintUnchangedError: 指纹与 last_compiled_fingerprint 一致。
        UnsupportedManualFeatureError: timeline.blocked_features 非空(§7.2)。
        NotImplementedError: spec 声明了尚未交付编译段的构建块(阶段守卫)。
        ValueError/NameError: SDK 构建失败(素材越界/轨道缺失等)。
    """
    fingerprint = compute_build_fingerprint(spec)
    if not force and spec.compile.last_compiled_fingerprint == fingerprint:
        raise FingerprintUnchangedError(fingerprint)

    if spec.timeline.blocked_features:
        raise UnsupportedManualFeatureError(
            "spec 携带 SDK 无法忠实重建的人工高级特征,已暂停编译并保留原草稿:"
            + json.dumps(spec.timeline.blocked_features, ensure_ascii=False)
        )
    _assert_no_unimplemented_sections(spec)

    script = new_script(spec)
    refs = append_base_tracks(script, spec)
    clips = _build_clips(
        script, spec, refs, video_material_factory=video_material_factory
    )
    audio_clips = _build_audio_clips(
        script, spec, refs, audio_material_factory=audio_material_factory
    )
    subtitles = _build_subtitles(script, spec, refs)

    payload = json.loads(script.dumps())
    validate_sdk_payload(payload)

    duration_us = int(script.duration or 0)
    counts = {
        "video_clips": len(clips),
        "audio_clips": len(audio_clips),
        "subtitles": len(subtitles),
        "tracks": {
            TRACK_VIDEO_MAIN: len(clips),
            TRACK_AUDIO: len(audio_clips),
            TRACK_SUBTITLES: len(subtitles),
        },
        "materials_total": sum(
            len(v) for v in payload.get("materials", {}).values() if isinstance(v, list)
        ),
    }
    summary = {
        "compiler_version": COMPILER_VERSION,
        "fingerprint": fingerprint,
        "compiled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": counts,
    }
    return CompileResult(
        payload=payload,
        fingerprint=fingerprint,
        duration_us=duration_us,
        summary=summary,
    )


__all__ = [
    "COMPILER_VERSION",
    "CompileResult",
    "FingerprintUnchangedError",
    "TRACK_AUDIO",
    "TRACK_STICKERS",
    "TRACK_SUBTITLES",
    "TRACK_VIDEO_MAIN",
    "UnsupportedManualFeatureError",
    "_build_clip_settings",
    "append_base_tracks",
    "compile_spec",
    "new_script",
]
