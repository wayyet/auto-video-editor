"""节点 11:inject_sticker — 贴纸 resource_id 关联(对应原文档 4.6 节)。

读取 ``materials.texts``,根据字幕文本关键词在 ``templates/sticker_template.json``
中查找 resource_id,创建贴纸 material 并挂到视频轨对应段(按时间重叠匹配)。

Week 3 补全:
- **时间范围绑 segment**:Week 3 占位把贴纸全挂到 segment[0];
  Week 3 补全后按贴纸 timerange 与 segment timerange 重叠度匹配,
  多段重叠时取交集面积最大的段;无重叠段时退回 segment[0] + 写 error_log。
- resource_id 仍为占位值,真实云端 ID 留 Week 4 由用户剪映客户端逆向回填
  (见 ``templates/sticker_template.json`` 的 ``_reverse_engineering_pending`` 标注)。
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from draft_ops.atomic_writer import safe_write_draft
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


def _segment_range(seg: dict) -> tuple[int, int]:
    """返回 ``[start, start+duration)``(微秒)。字段缺失时退化为 0。"""
    tr = seg.get("target_timerange", {}) or {}
    start = int(tr.get("start", 0))
    duration = int(tr.get("duration", 0))
    return start, start + duration


def _timerange_range(tr: dict) -> tuple[int, int]:
    start = int(tr.get("start", 0))
    duration = int(tr.get("duration", 0))
    return start, start + duration


def _overlap_us(a: tuple[int, int], b: tuple[int, int]) -> int:
    """两闭区间重叠长度(微秒),无重叠返回 0。"""
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return max(0, hi - lo)


def _find_overlapping_segment(
    segments: list[dict],
    timerange: dict,
) -> dict | None:
    """按 timerange 找时间重叠最大的 segment;无重叠返回 None。

    选择规则:
    - 任一像素级重叠(``> 0 us``)即认为匹配
    - 多段重叠时取交集面积最大的段
    - 无任何重叠段 → 返回 None(由调用方决定 fallback 策略)
    """
    if not segments:
        return None
    target_range = _timerange_range(timerange)
    best: tuple[int, dict] | None = None
    for seg in segments:
        ov = _overlap_us(target_range, _segment_range(seg))
        if ov > 0 and (best is None or ov > best[0]):
            best = (ov, seg)
    return best[1] if best else None


def inject_sticker(state: WorkflowState) -> dict:
    """读取草稿 → 为每条字幕解析贴纸 resource_id → 注入贴纸 material → 原子写回。

    Week 3 补全:贴纸挂载到与字幕 timerange 时间重叠度最大的 video segment;
    无重叠时退回 segment[0](Week 3 兼容路径)+ error_log 提示。
    """
    draft_path = Path(state["draft_path"])
    draft = _load_draft(draft_path)

    materials = draft.setdefault("materials", {})
    stickers_material = materials.setdefault("stickers", [])
    video_track = _find_video_track(draft)
    segments = video_track.get("segments", [])

    texts = draft.get("materials", {}).get("texts", [])
    error_log = list(state.get("error_log", []) or [])

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

        # Week 3 补全:按 timerange 找时间重叠 segment
        target_tr = text_obj.get("target_timerange", {}) or {}
        target_seg = _find_overlapping_segment(segments, target_tr)
        if target_seg is None:
            # Week 3 兼容:无重叠时退回 segment[0] + error_log
            if segments:
                segments[0].setdefault("extra_material_refs", []).append(sticker_id)
                error_log.append(
                    f"[node_11] 字幕 timerange {target_tr} 无重叠 segment,"
                    f"贴纸 {sticker_id} 退回 segment[0](Week 3 兼容)"
                )
        else:
            target_seg.setdefault("extra_material_refs", []).append(sticker_id)

    video_track["segments"] = segments
    # Week 5:参数从 draft_path 提升为 draft_path.parent,safe_write_draft 双写
    write_result = safe_write_draft(draft_path.parent, draft)

    log = list(state.get("status_log", []) or []) + ["node_11_inject_sticker_done"]
    if write_result.get("jianying_running"):
        log.append("[node_11] 剪映进程在跑,写入仍继续(告警不阻断)")
    return {**state, "status_log": log, "error_log": error_log}