"""节点 11:inject_sticker — 贴纸 resource_id 关联(对应原文档 4.6 节)。

读取 ``materials.texts``,根据字幕文本关键词在 ``templates/sticker_template.json``
中查找 resource_id,创建贴纸 material 并挂到视频轨对应段。

Week 3 关键难度:贴纸时间区间必须与字幕 ``target_timerange`` 偏差 ≤1 帧。
本实现使用精确的 ``start`` + ``duration`` 赋值保证零偏差。
Week 4 接入 ``sync_jy_assets.py`` 思路后,resource_id 从占位升级为真实云端 ID。
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from draft_ops.atomic_writer import atomic_write_draft
from jy_common.sticker_resolver import resolve_sticker_resource_id
from state import WorkflowState


def _load_draft(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_video_track(draft: dict) -> dict:
    for track in draft.get("tracks", []):
        if track.get("type") == "video":
            return track
    tracks = draft.get("tracks", [])
    return tracks[0] if tracks else {"type": "video", "segments": []}


def inject_sticker(state: WorkflowState) -> dict:
    """读取草稿 → 为每条字幕解析贴纸 resource_id → 注入贴纸 material → 原子写回。"""
    draft_path = Path(state["draft_path"])
    draft = _load_draft(draft_path)

    materials = draft.setdefault("materials", {})
    stickers_material = materials.setdefault("stickers", [])
    video_track = _find_video_track(draft)

    texts = draft.get("materials", {}).get("texts", [])
    segments = video_track.get("segments", [])
    for text_obj in texts:
        content = text_obj.get("content", "")
        resource_id = resolve_sticker_resource_id(content)
        if not resource_id:
            continue

        sticker_id = f"sticker-{uuid4().hex[:8]}"
        sticker_obj = {
            "id": sticker_id,
            "resource_id": resource_id,
            "target_timerange": dict(text_obj.get("target_timerange", {})),
        }
        stickers_material.append(sticker_obj)

        # 挂到第一个视频段(Week 3 简化 — Week 4 按 timerange 找最匹配的段)
        if segments:
            segments[0].setdefault("extra_material_refs", []).append(sticker_id)
    video_track["segments"] = segments

    atomic_write_draft(draft_path, draft)

    log = list(state.get("status_log", []) or []) + ["node_11_inject_sticker_done"]
    return {**state, "status_log": log}