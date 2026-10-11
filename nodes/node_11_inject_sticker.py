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

from pathlib import Path

from jy_common.sticker_resolver import resolve_sticker_resource_id
from nodes._draft_io import apply_and_write, load_draft
from nodes._dual_draft import ZH, dual_draft_targets, missing_en_note
from state import WorkflowState


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


def _upsert_by_id(items: list[dict], item: dict) -> bool:
    """按 ``id`` 覆盖写入;返回 True 表示本次是新增。

    2026-10 K2=B 幂等加固:本节点现在横跨关卡②,resume 路径变多,纯 append 会让
    ``materials.stickers`` 每次重放都多一份。
    """
    item_id = item.get("id")
    for idx, existing in enumerate(items):
        if existing.get("id") == item_id:
            items[idx] = item
            return False
    items.append(item)
    return True


def _append_ref_once(refs: list, sticker_id: str) -> None:
    """往 segment 的 ``extra_material_refs``(剪映格式:字符串 id 列表)里补引用。

    重放时不重复追加(幂等)——不改列表类型,只判存在。
    """
    if sticker_id not in refs:
        refs.append(sticker_id)


def _sticker_id_for(text_obj: dict, index: int) -> str:
    """贴纸 id 由字幕 id 派生(确定性),重放得到同一个 id → 可按 id 覆盖,幂等。

    2026-10 之前用 ``uuid4()``,重放会叠出重复贴纸。字幕没有 id 时退回序号兜底
    (``node_08`` / ``node_08b`` 产出的 text 一定带 id,兜底只防手工构造的草稿)。
    """
    text_id = text_obj.get("id")
    return f"sticker-{text_id}" if text_id else f"sticker-idx-{index}"


def _inject_sticker_into(draft: dict, tag: str) -> list[str]:
    """把贴纸注入**一份**草稿(就地改 ``draft``),返回告警列表。

    中英两份草稿共用这一个实现。注意英文草稿的 ``materials.texts`` 是英文文本,
    贴纸关键词命中情况可能与中文不同 —— 属预期,不做跨语言关键词映射。
    """
    error_log: list[str] = []

    materials = draft.setdefault("materials", {})
    stickers_material = materials.setdefault("stickers", [])
    video_track = _find_video_track(draft)
    segments = video_track.get("segments", [])

    texts = materials.get("texts", []) or []

    for index, text_obj in enumerate(texts):
        content = text_obj.get("content", "")
        resource_id = resolve_sticker_resource_id(content)
        if not resource_id:
            continue

        sticker_id = _sticker_id_for(text_obj, index)
        sticker_obj = {
            "id": sticker_id,
            "resource_id": resource_id,
            "target_timerange": dict(text_obj.get("target_timerange", {}) or {}),
        }
        _upsert_by_id(stickers_material, sticker_obj)

        # Week 3 补全:按 timerange 找时间重叠 segment
        target_tr = text_obj.get("target_timerange", {}) or {}
        target_seg = _find_overlapping_segment(segments, target_tr)
        if target_seg is None:
            # Week 3 兼容:无重叠时退回 segment[0] + error_log
            if segments:
                _append_ref_once(segments[0].setdefault("extra_material_refs", []), sticker_id)
                error_log.append(
                    f"[{tag}] 字幕 timerange {target_tr} 无重叠 segment,"
                    f"贴纸 {sticker_id} 退回 segment[0](Week 3 兼容)"
                )
        else:
            _append_ref_once(target_seg.setdefault("extra_material_refs", []), sticker_id)

    video_track["segments"] = segments
    return error_log


def inject_sticker(state: WorkflowState) -> dict:
    """读取草稿 → 为每条字幕解析贴纸 resource_id → 注入贴纸 material → 原子写回。

    Week 3 补全:贴纸挂载到与字幕 timerange 时间重叠度最大的 video segment;
    无重叠时退回 segment[0](Week 3 兼容路径)+ error_log 提示。

    K2=B(2026-10):对**中文主草稿 + 英文分支草稿**各注入一次(见 ``nodes/_dual_draft.py``)。
    两份草稿在同一节点内顺序处理,不跨超步,返回值契约不变(整份 state)。
    """
    status_log = list(state.get("status_log", []) or [])
    error_log = list(state.get("error_log", []) or [])

    targets = dual_draft_targets(state)
    if not targets:
        raise FileNotFoundError(f"[node_11] state.draft_path 缺失,无法注入贴纸")

    has_en_dir = bool(state.get("draft_dir_en_branch"))
    for tag, draft_file in targets:
        draft = load_draft(draft_file)
        error_log.extend(_inject_sticker_into(draft, f"node_11_{tag}"))
        # Week 5:参数从 draft_path 提升为 draft_path.parent,safe_write_draft 双写
        status_log.extend(
            apply_and_write(
                draft_file,
                draft,
                "node_11",
                done_tag=(
                    "node_11_inject_sticker_done" if tag == ZH else "node_11_inject_sticker_en_done"
                ),
            )
        )

    if len(targets) == 1 and has_en_dir:
        error_log.append(missing_en_note(state))

    return {**state, "status_log": status_log, "error_log": error_log}