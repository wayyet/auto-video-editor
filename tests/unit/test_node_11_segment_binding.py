"""节点 11 时间范围绑 segment 单测 — Week 3 补全验证。

覆盖:
(a) 字幕 timerange 与 seg-2 重叠 → 挂到 seg-2
(b) 字幕 timerange 与 seg-1 重叠 → 挂到 seg-1
(c) 多段重叠 → 取交集面积最大的段
(d) 无重叠 → 退回 seg-0 + error_log
(e) 既有零偏差 timerange 复制测试仍通过
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_11_inject_sticker import (
    _find_overlapping_segment,
    _overlap_us,
    _segment_range,
    _timerange_range,
    inject_sticker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _seg(seg_id: str, start_us: int, duration_us: int) -> dict:
    return {"id": seg_id, "target_timerange": {"start": start_us, "duration": duration_us}}


def _timerange(start_us: int, duration_us: int) -> dict:
    return {"start": start_us, "duration": duration_us}


# ---------------------------------------------------------------------------
# 工具函数单元测试
# ---------------------------------------------------------------------------
def test_overlap_us_basic() -> None:
    """完全重叠 → 等于段长。"""
    assert _overlap_us((0, 1_000_000), (0, 1_000_000)) == 1_000_000
    # 部分重叠
    assert _overlap_us((0, 500_000), (300_000, 800_000)) == 200_000
    # 不重叠
    assert _overlap_us((0, 100_000), (200_000, 300_000)) == 0


def test_segment_range_and_timerange_range() -> None:
    """`_segment_range` 与 `_timerange_range` 行为一致。"""
    seg = {"target_timerange": {"start": 1_000_000, "duration": 2_000_000}}
    assert _segment_range(seg) == (1_000_000, 3_000_000)
    assert _timerange_range({"start": 1_000_000, "duration": 2_000_000}) == (1_000_000, 3_000_000)


def test_find_overlapping_segment_picks_largest_overlap() -> None:
    """多段重叠时,取交集面积最大的段。"""
    # seg-1 重叠 1s, seg-2 重叠 2s → 应选 seg-2
    segments = [
        _seg("seg-1", 0, 2_000_000),
        _seg("seg-2", 3_000_000, 5_000_000),
    ]
    timerange = _timerange(1_500_000, 3_000_000)  # 与 seg-1 重叠 0.5s,与 seg-2 重叠 1.5s
    # 重新计算:seg-1 = [0, 2s],seg-2 = [3s, 8s];timerange = [1.5s, 4.5s]
    # seg-1 重叠 = [1.5s, 2s] = 0.5s
    # seg-2 重叠 = [3s, 4.5s] = 1.5s
    target = _find_overlapping_segment(segments, timerange)
    assert target is not None
    assert target["id"] == "seg-2"


def test_find_overlapping_segment_returns_none_when_no_overlap() -> None:
    """无重叠时返回 None。"""
    segments = [_seg("seg-1", 0, 1_000_000), _seg("seg-2", 5_000_000, 1_000_000)]
    timerange = _timerange(2_000_000, 1_000_000)  # [2s, 3s] — 与两段都不重叠
    assert _find_overlapping_segment(segments, timerange) is None


def test_find_overlapping_segment_empty_list() -> None:
    """空 segments 列表返回 None。"""
    assert _find_overlapping_segment([], _timerange(0, 1_000_000)) is None


# ---------------------------------------------------------------------------
# (a)+(b)+(c)+(d) 节点 11 行为
# ---------------------------------------------------------------------------
def _build_draft_with_video_segments(segments: list[dict]) -> dict:
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {
            "texts": [
                {
                    "id": "t1",
                    "content": "真棒,给你点赞",  # 关键词 "赞" → 命中
                    "target_timerange": {"start": 4_000_000, "duration": 2_000_000},
                },
            ],
        },
        "tracks": [{"type": "video", "segments": segments}],
    }


def _state(draft_path: Path) -> dict:
    return {"draft_path": str(draft_path), "status_log": [], "error_log": []}


def test_inject_sticker_attaches_to_overlapping_segment(tmp_path: Path) -> None:
    """字幕 timerange 与 seg-2 重叠 → 贴纸挂到 seg-2(不是 seg-0)。"""
    draft_dict = _build_draft_with_video_segments([
        _seg("seg-0", 0, 3_000_000),
        _seg("seg-1", 3_000_000, 1_000_000),       # [3s, 4s]
        _seg("seg-2", 4_000_000, 2_000_000),       # [4s, 6s] — 与字幕 [4s, 6s] 完全重叠
    ])
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft_dict, ensure_ascii=False, indent=2), encoding="utf-8")

    out = inject_sticker(_state(p))
    draft = json.loads(p.read_text(encoding="utf-8"))

    # 找含 seg-2 的 segment,验证贴纸挂载
    segments = draft["tracks"][0]["segments"]
    seg_2_refs = next(s for s in segments if s["id"] == "seg-2").get("extra_material_refs", [])
    seg_0_refs = next(s for s in segments if s["id"] == "seg-0").get("extra_material_refs", [])

    assert len(seg_2_refs) >= 1, "贴纸应挂到 seg-2"
    assert len(seg_0_refs) == 0, "贴纸不应挂到 seg-0"
    assert "node_11_inject_sticker_done" in out["status_log"]


def test_inject_sticker_falls_back_to_first_segment_when_no_overlap(tmp_path: Path) -> None:
    """字幕 timerange 与任何 segment 都不重叠 → 退回 seg-0 + error_log。"""
    draft_dict = _build_draft_with_video_segments([
        _seg("seg-0", 0, 1_000_000),               # [0s, 1s]
        _seg("seg-1", 8_000_000, 1_000_000),       # [8s, 9s] — 字幕 [4s, 6s] 与两段都不重叠
    ])
    # 字幕 timerange: [4s, 6s] — 与两段都不重叠
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft_dict, ensure_ascii=False, indent=2), encoding="utf-8")

    out = inject_sticker(_state(p))
    draft = json.loads(p.read_text(encoding="utf-8"))

    # 贴纸应退回到 seg-0
    segments = draft["tracks"][0]["segments"]
    seg_0_refs = next(s for s in segments if s["id"] == "seg-0").get("extra_material_refs", [])
    assert len(seg_0_refs) >= 1, "无重叠时退回 seg-0"

    # error_log 应有提示
    assert any("无重叠" in e or "退回" in e for e in out["error_log"])