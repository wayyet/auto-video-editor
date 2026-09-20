"""OpenStoryline 产物读盘 → CanonicalTimeline。

事实源:
- 文件落盘:``<outputs_dir>/<session_id>/<node_id>/<artifact_id>.json``
  (FireRed ``storage/agent_memory.py:77-119`` ``save_result`` 规则)
- 字段 schema:``plan_timeline_pro.py:649-656`` 真实输出
  ``{"tracks": {"video": [...], "subtitles": [...], "voiceover": [...], "bgm": [...]}}``
- 每个 video 元素:``{source_path, source_window{start,end,duration},
  timeline_window{start,end,duration}, clip_id, size}``
- 解析模板:FireRed ``.claude/skills/openstoryline-to-jianying/scripts/
  build_draft.py:86-128`` 的 ``add_videos`` 段(只用了 4 个字段)。

本模块只负责**读盘 + 拍平**;``ContractInvalid`` 校验由 CanonicalTimeline 的
``model_validator`` 触发,失败由 node_04 的 ``_fallback_to_shot_plan`` 兜底。

兼容两种 artifact schema:
- v2(新,NodeInterceptor.compress_payload_to_base64 写入):
  ``{"payload": <packed_output>, "session_id": ..., "artifact_id": ...,
  "create_time": ..., "node_id": ...}``
- v1(旧,直接写):``{"tracks": {...}, "artifact_id": ..., ...}``
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from storyline.contract import (
    CanonicalTimeline,
    Clip as ClipModel,
    SourceMedia,
    TimelineAudio,
    TimelineOptions,
    TimelineSubtitles,
)
from storyline.mapper import _path_to_file_uri


class PlanReaderError(Exception):
    """产物读取或字段缺失时抛出。"""


@dataclass
class PlanTimelineProData:
    artifact_id: str
    raw: dict  # 原始 JSON(已剥 payload 包装),便于诊断

    def to_canonical(self, *, job_id: str) -> CanonicalTimeline:
        """拍平为 :class:`CanonicalTimeline`,失败抛 :class:`ContractInvalid`。"""
        tracks = self.raw.get("tracks") or {}
        videos = tracks.get("video") or []
        if not videos:
            raise PlanReaderError("tracks.video 为空(plan_timeline_pro 没产出)")

        # 1. 按 source_path 去重,重建 SourceMedia
        media_list: list[SourceMedia] = []
        media_seen: dict[str, SourceMedia] = {}
        for v in videos:
            spath = v.get("source_path") or ""
            if not spath or spath in media_seen:
                continue
            sw = v.get("source_window") or {}
            dur_ms = int(sw.get("duration") or 0)
            if not dur_ms and sw.get("end") is not None and sw.get("start") is not None:
                dur_ms = int(sw["end"]) - int(sw["start"])
            media = SourceMedia(
                media_id=f"media-{len(media_list) + 1}",
                file_uri=_path_to_file_uri(spath),
                duration_ms=dur_ms or 0,
            )
            media_seen[spath] = media
            media_list.append(media)

        media_by_uri: dict[str, SourceMedia] = {m.file_uri: m for m in media_list}

        # 2. 拍平 clips(单调非降由 CanonicalTimeline.model_validator 校验)
        clips: list[ClipModel] = []
        for idx, v in enumerate(videos, start=1):
            spath = v.get("source_path") or ""
            media = media_by_uri.get(_path_to_file_uri(spath))
            if media is None:
                # 孤立 video(理论上不会发生,videos 至少 1 个 source_path 已建 media)
                continue
            sw = v.get("source_window") or {}
            tw = v.get("timeline_window") or {}
            source_in_ms = int(sw.get("start") or 0)
            source_out_ms = int(sw.get("end") or source_in_ms)
            timeline_in_ms = int(tw.get("start") or 0)
            timeline_out_ms = int(tw.get("end") or timeline_in_ms)
            clips.append(
                ClipModel(
                    clip_id=v.get("clip_id") or f"clip-{idx}",
                    source_media_id=media.media_id,
                    source_in_ms=source_in_ms,
                    source_out_ms=source_out_ms,
                    timeline_in_ms=timeline_in_ms,
                    timeline_out_ms=timeline_out_ms,
                )
            )

        # 3. subtitles 拼到 subtitles.zh("\n" 分段;Phase 4 再细化为 segments)
        subtitles = tracks.get("subtitles") or []
        zh_lines: list[str] = []
        for s in subtitles:
            txt = (s.get("text") or "").strip()
            if txt:
                zh_lines.append(txt)

        # 4. voiceover / bgm 在 Phase 4 之前不进 canonical.audio(留空)
        return CanonicalTimeline(
            schema_version="1.0",
            job_id=job_id,
            created_at_ms=int(self.raw.get("create_time") or 0),
            source_media=media_list,
            clips=clips,
            audio=TimelineAudio(),
            subtitles=TimelineSubtitles(zh="\n".join(zh_lines) or None),
            options=TimelineOptions(),
        )


def find_latest_session_dir(root: Path) -> Optional[Path]:
    """在 ``<root>/`` 下找最新修改的子目录(UUID4 形式,agent_fastapi 产物命名)。"""
    if not root.exists():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def read_latest_plan_timeline_pro(
    session_dir: Path,
) -> tuple[Path, PlanTimelineProData]:
    """读 ``<session_dir>/plan_timeline_pro/plan_timeline_pro_*.json`` 中按 mtime 最新的一份。

    Args:
        session_dir: 单个 session 的产物目录(UUID4 形式)。

    Returns:
        ``(plan_file_path, parsed_data)``。

    Raises:
        PlanReaderError: 目录不存在 / 没有匹配文件 / JSON 解析失败。
    """
    ptp_dir = session_dir / "plan_timeline_pro"
    if not ptp_dir.exists():
        raise PlanReaderError(f"产物目录不存在: {ptp_dir}")
    files = [
        ptp_dir / f
        for f in ptp_dir.iterdir()
        if f.is_file()
        and f.name.startswith("plan_timeline_pro")
        and f.suffix == ".json"
    ]
    if not files:
        raise PlanReaderError(f"未在 {ptp_dir} 找到 plan_timeline_pro_*.json")
    plan_file = max(files, key=lambda p: p.stat().st_mtime)
    try:
        raw = json.loads(plan_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise PlanReaderError(f"产物 JSON 解析失败: {plan_file}: {e}") from e

    # 兼容 v2(``payload`` 包装)与 v1(直接 ``tracks``)
    if "payload" in raw:
        payload = raw.get("payload") or {}
    else:
        payload = raw
    artifact_id = raw.get("artifact_id") or plan_file.stem
    return plan_file, PlanTimelineProData(artifact_id=artifact_id, raw=payload)


__all__ = [
    "PlanReaderError",
    "PlanTimelineProData",
    "find_latest_session_dir",
    "read_latest_plan_timeline_pro",
]