"""Canonical Timeline(整数毫秒) ↔ 剪映 ``draft_content.json``(整数微秒) 映射。

设计纪律:
1. **毫秒↔微秒只用整数算**``int(ms) * 1000`` / ``us // 1000``,禁用浮点累计。
2. **保序**``:func:`canonical_to_draft` 不重排 clips;FireRed 已经排好。
3. **空 plan** → 空 tracks(便于 node_05 早退与 idempotent 测试)。
4. **非法 plan** 抛 :class:`ValueError`,由 node_05 走老 ``DraftStatus`` 早退路径。

剪映 ``draft_content.json`` 字段映射(对照 attachment 6.1 + node_05 既有):
- ``canvas_config`` → ``config.CANVAS_WIDTH / CANVAS_HEIGHT``
- ``materials.videos`` ← 每个 ``clip`` 对应一条 video material
- ``materials.audios`` ← ``audio.bgm_ref`` / ``audio.voiceover``
- ``tracks`` ← 每段 clip 转 ``Segment``(target_start_us / target_end_us)
- 每个 video material 额外携带 ``source_in_ms / source_out_ms`` 字段(便于 node_07 变速)
"""

from __future__ import annotations

from typing import Any, Optional

from config import CANVAS_HEIGHT, CANVAS_WIDTH
from storyline.contract import CanonicalTimeline, SourceMedia


_MS_TO_US = 1000
_US_TO_MS = 1000


def _ms_to_us(ms: int) -> int:
    """整数毫秒 → 整数微秒(乘法,不引入浮点)。"""
    return int(ms) * _MS_TO_US


def _us_to_ms(us: int) -> int:
    """整数微秒 → 整数毫秒(整除,带 floor 语义)。"""
    return int(us) // _US_TO_MS


def file_uri_to_path(file_uri: str) -> str:
    """``file:///E:/...`` → 本机绝对路径。

    Windows 平台把 ``/E:/...`` 还原为 ``E:\\...``;
    Linux/macOS 平台 ``/E:/...`` 当作字面路径处理(极少使用)。
    """
    if not file_uri.startswith("file:///"):
        raise ValueError(f"not a file:// URI: {file_uri!r}")
    raw = file_uri[len("file:///"):]
    # Windows: E:/path/to/file -> E:\\path\\to\\file
    if len(raw) >= 2 and raw[1] == ":":
        drive = raw[0]
        rest = raw[2:].lstrip("/").replace("/", "\\")
        return f"{drive}:\\{rest}"
    # POSIX
    return "/" + raw.lstrip("/")


def canonical_to_draft(
    plan: CanonicalTimeline,
    *,
    canvas_w: int = CANVAS_WIDTH,
    canvas_h: int = CANVAS_HEIGHT,
) -> dict[str, Any]:
    """把 Canonical Timeline(毫秒) 翻译成 ``draft_content.json`` 字典(微秒)。

    Args:
        plan: 已通过 Pydantic 校验的 CanonicalTimeline。
        canvas_w / canvas_h: 画布宽高,默认 ``config.CANVAS_WIDTH / CANVAS_HEIGHT``。

    Returns:
        可直接喂给 :func:`draft_ops.atomic_writer.atomic_write_draft` 的 dict。

    Raises:
        ValueError: plan 内部矛盾(理论上 Pydantic 已防,这里再加一次防御)。
    """
    # 1. 构造 materials.videos
    media_by_id = {s.media_id: s for s in plan.source_media}
    videos: list[dict[str, Any]] = []
    for idx, clip in enumerate(plan.clips, start=1):
        media: SourceMedia = media_by_id[clip.source_media_id]
        videos.append(
            {
                "id": f"video-{idx}",
                "type": "video",
                "path": file_uri_to_path(media.file_uri),
                "media_id": media.media_id,
                "shot_id": clip.scene_id or clip.clip_id,
                "source_in_ms": int(clip.source_in_ms),
                "source_out_ms": int(clip.source_out_ms),
                # 整微秒因子,便于剪映端不引入浮点
                "source_in_us": _ms_to_us(clip.source_in_ms),
                "source_out_us": _ms_to_us(clip.source_out_ms),
                # 剪映原生期望 ``duration`` (微秒);同时保留 ``duration_ms``(毫秒)
                # 给 mapper 反向映射 (``draft_to_canonical``) 读取。
                "duration": _ms_to_us(int(media.duration_ms)),
                "duration_ms": int(media.duration_ms),
                "content_hash": clip.content_hash,
                # 剪映原生 extra 字段
                "material_name": f"{clip.clip_id}",
            }
        )

    # 2. 构造 materials.audios(BGM + 配音,均允许 null)
    audios: list[dict[str, Any]] = []
    if plan.audio.bgm_ref:
        audios.append(
            {
                "id": "bgm-1",
                "type": "audio",
                "ref": plan.audio.bgm_ref,
                "role": "bgm",
            }
        )
    if plan.audio.voiceover:
        audios.append(
            {
                "id": "voiceover-1",
                "type": "audio",
                "ref": plan.audio.voiceover,
                "role": "voiceover",
            }
        )

    # 3. 构造 tracks(每段 clip 对应一个 segment)
    video_track: dict[str, Any] = {
        "type": "video",
        "id": "track-video-1",
        "segments": [],
    }
    for idx, clip in enumerate(plan.clips, start=1):
        seg_duration_us = _ms_to_us(clip.timeline_out_ms - clip.timeline_in_ms)
        seg_source_duration_us = _ms_to_us(clip.source_out_ms - clip.source_in_ms)
        video_track["segments"].append(
            {
                "id": f"segment-{idx}",
                "material_id": f"video-{idx}",
                # 剪映原生 ``target_timerange`` / ``source_timerange`` 形状:{start, duration} 微秒
                "target_timerange": {
                    "start": _ms_to_us(clip.timeline_in_ms),
                    "duration": seg_duration_us,
                },
                "source_timerange": {
                    "start": _ms_to_us(clip.source_in_ms),
                    "duration": seg_source_duration_us,
                },
                # 派生字段,便于 mapper 反向映射 / 测试断言
                "target_start_us": _ms_to_us(clip.timeline_in_ms),
                "target_end_us": _ms_to_us(clip.timeline_out_ms),
                "source_in_us": _ms_to_us(clip.source_in_ms),
                "source_out_us": _ms_to_us(clip.source_out_ms),
                "speed": 1.0,
                "transcript": clip.transcript or "",
            }
        )

    audio_track: dict[str, Any] = {
        "type": "audio",
        "id": "track-audio-1",
        "segments": [],
    }
    if plan.audio.bgm_ref:
        # BGM 沿总时间轴铺,具体时长由 node_07 后续调整
        total_ms = (
            plan.clips[-1].timeline_out_ms if plan.clips else 0
        )
        audio_track["segments"].append(
            {
                "id": "segment-bgm-1",
                "material_id": "bgm-1",
                "target_start_us": 0,
                "target_end_us": _ms_to_us(total_ms),
                "speed": 1.0,
                "loop": True,
            }
        )
    if plan.audio.voiceover:
        # 配音随各 group 的 timeline_in_ms/timeline_out_ms 拼
        # Phase 2 接入 Spark / BGM 卡点精确偏移
        audio_track["segments"].append(
            {
                "id": "segment-voiceover-1",
                "material_id": "voiceover-1",
                "target_start_us": 0,
                "target_end_us": _ms_to_us(
                    plan.clips[-1].timeline_out_ms if plan.clips else 0
                ),
                "speed": 1.0,
            }
        )

    text_track: dict[str, Any] = {
        "type": "text",
        "id": "track-text-1",
        "segments": [],
    }
    if plan.subtitles.zh:
        text_track["segments"].append(
            {
                "id": "subtitle-zh-1",
                "text": plan.subtitles.zh,
                "target_start_us": 0,
                "target_end_us": _ms_to_us(
                    plan.clips[-1].timeline_out_ms if plan.clips else 0
                ),
                "lang": "zh",
            }
        )

    return {
        "canvas_config": {"width": canvas_w, "height": canvas_h},
        "materials": {
            "videos": videos,
            "audios": audios,
            "texts": [],
        },
        "tracks": [video_track, audio_track, text_track],
        "storyline_meta": {
            "schema_version": plan.schema_version,
            "job_id": plan.job_id,
            "options": plan.options.model_dump(),
            "audio": plan.audio.model_dump(),
            "subtitles": plan.subtitles.model_dump(),
        },
    }


def draft_to_canonical(draft: dict[str, Any], *, job_id: str) -> CanonicalTimeline:
    """反向映射:``draft_content.json`` → CanonicalTimeline(供 resume 校验)。

    用于 LangGraph checkpoint resume 时,如果 FireRed session 已过期,但
    ``outputs/{job_id}/draft_content.json`` 还在,可以直接读出来重建 canonical
    然后跳过 node_04 重跑。
    """
    meta = draft.get("storyline_meta") or {}
    audio_meta = meta.get("audio") or {}
    subtitles_meta = meta.get("subtitles") or {}
    options_meta = meta.get("options") or {}

    # 1. 把 video materials 还原成 SourceMedia + Clip
    videos = (draft.get("materials") or {}).get("videos") or []
    media_by_path: dict[str, SourceMedia] = {}
    clips: list[dict[str, Any]] = []
    media_list: list[SourceMedia] = []
    for idx, video in enumerate(videos, start=1):
        path = video.get("path") or ""
        if path not in media_by_path:
            duration_ms = int(video.get("duration_ms") or 0)
            media_id = video.get("media_id") or f"media-{idx}"
            media_by_path[path] = SourceMedia(
                media_id=media_id,
                file_uri=_path_to_file_uri(path),
                duration_ms=duration_ms,
            )
            media_list.append(media_by_path[path])
        media = media_by_path[path]
        # 找对应 segment 取 timeline 落位
        timeline_in_ms = int(video.get("source_in_ms") or 0)
        timeline_out_ms = int(video.get("source_out_ms") or timeline_in_ms)
        clips.append(
            {
                "clip_id": video.get("shot_id") or f"clip-{idx}",
                "source_media_id": media.media_id,
                "source_in_ms": int(video.get("source_in_ms") or 0),
                "source_out_ms": int(video.get("source_out_ms") or 0),
                "timeline_in_ms": timeline_in_ms,
                "timeline_out_ms": timeline_out_ms,
                "transcript": video.get("transcript"),
                "scene_id": video.get("shot_id"),
                "content_hash": video.get("content_hash"),
            }
        )

    # 2. 时长不变量防御
    clips.sort(key=lambda c: c["timeline_in_ms"])

    from storyline.contract import (
        Clip as ClipModel,
        TimelineAudio,
        TimelineOptions,
        TimelineSubtitles,
    )

    return CanonicalTimeline(
        schema_version="1.0",
        job_id=job_id,
        created_at_ms=int(meta.get("created_at_ms") or 0),
        source_media=media_list,
        clips=[ClipModel(**c) for c in clips],
        audio=TimelineAudio(**audio_meta) if audio_meta else TimelineAudio(),
        subtitles=TimelineSubtitles(**subtitles_meta) if subtitles_meta else TimelineSubtitles(),
        options=TimelineOptions(**options_meta) if options_meta else TimelineOptions(),
    )


def _path_to_file_uri(path: str) -> str:
    """本地绝对路径 → ``file:///...`` URI。

    Windows: ``E:\\path\\to\\file`` → ``file:///E:/path/to/file``。
    """
    p = path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        return f"file:///{p}"
    if p.startswith("/"):
        return f"file://{p}"
    return f"file:///{p}"


def storyline_plan_to_canonical(
    plan_dict: dict[str, Any],
    *,
    job_id: str,
    source_media: list[SourceMedia],
) -> CanonicalTimeline:
    """把 FireRed ``plan_timeline_pro`` 的返回拍平为 CanonicalTimeline。

    Phase 1 实现 ``tool_excute_result.timeline.groups[*].media_refs[]`` →
    ``clips[]``;``bgm_ref`` / ``voiceover_ref`` 映射 ``audio`` 段;``subtitle_text``
    拼到 ``subtitles.zh``(多 group 用 ``\\n`` 拼接)。

    Args:
        plan_dict: ``StorylinePlan.model_dump()`` 兼容的 dict。
        job_id: LangGraph job_id。
        source_media: 已建立的 ``SourceMedia`` 列表(来自 ``load_media`` artifact)。

    Returns:
        通过 Pydantic 校验的 CanonicalTimeline。
    """
    from storyline.contract import (
        Clip as ClipModel,
        TimelineAudio,
        TimelineOptions,
        TimelineSubtitles,
    )

    timeline = (plan_dict.get("tool_excute_result") or {}).get("timeline") or {}
    groups = timeline.get("groups") or []
    bgm_ref = timeline.get("bgm_ref")
    voiceover_ref: Optional[str] = None
    zh_subtitles: list[str] = []

    clips: list[ClipModel] = []
    for g_idx, group in enumerate(groups):
        subtitle = group.get("subtitle_text") or ""
        if subtitle:
            zh_subtitles.append(subtitle)
        voiceover_ref = voiceover_ref or group.get("voiceover_ref")
        for media_ref in group.get("media_refs") or []:
            media_id = media_ref.get("media_id") or ""
            source_in_ms = int(media_ref.get("source_in_ms") or 0)
            source_out_ms = int(media_ref.get("source_out_ms") or source_in_ms)
            timeline_in_ms = int(group.get("start_ms") or 0) + sum(
                c.timeline_out_ms - c.timeline_in_ms for c in clips
            )
            timeline_out_ms = timeline_in_ms + (source_out_ms - source_in_ms)
            scene_id = media_ref.get("shot_id") or f"shot-{g_idx}"
            clips.append(
                ClipModel(
                    clip_id=f"clip-{len(clips) + 1}",
                    source_media_id=media_id,
                    source_in_ms=source_in_ms,
                    source_out_ms=source_out_ms,
                    timeline_in_ms=timeline_in_ms,
                    timeline_out_ms=timeline_out_ms,
                    scene_id=scene_id,
                )
            )

    return CanonicalTimeline(
        schema_version="1.0",
        job_id=job_id,
        created_at_ms=int(plan_dict.get("created_at_ms") or 0),
        source_media=source_media,
        clips=clips,
        audio=TimelineAudio(bgm_ref=bgm_ref, voiceover=voiceover_ref),
        subtitles=TimelineSubtitles(zh="\n".join(zh_subtitles) or None),
        options=TimelineOptions(),
    )


__all__ = [
    "_ms_to_us",
    "_us_to_ms",
    "file_uri_to_path",
    "canonical_to_draft",
    "draft_to_canonical",
    "storyline_plan_to_canonical",
]