"""node_16_translate_subtitles 纯函数单测 — SRT 序列化、草稿重读。

历史:本文件曾覆盖 layout 校验(``validate_layout``)与关卡③ payload 构造
(``_build_interrupt_payload``);2026-11-15 关卡③整体删除,两者随实现一并移除。

2026-11-12 noop 治理阶段4:原文件中的 shim 级用例(直接调用未注册进图的
``node_16_translate_subtitles()``)已随 shim 一起删除;其覆盖的节点行为由
``tests/unit/test_node_16a_translate_and_check.py`` 承接。
"""

from __future__ import annotations

import json
from pathlib import Path

from nodes.node_16_translate_subtitles import (
    _ms_to_srt_time,
    _read_segments_from_draft,
    _segments_to_srt,
)


# ---------------------------------------------------------------------------
# 纯函数测试
# ---------------------------------------------------------------------------
def test_ms_to_srt_time_basic() -> None:
    assert _ms_to_srt_time(0) == "00:00:00,000"
    assert _ms_to_srt_time(1_500) == "00:00:01,500"
    assert _ms_to_srt_time(65_123) == "00:01:05,123"
    assert _ms_to_srt_time(3_661_500) == "01:01:01,500"


def test_ms_to_srt_time_negative_clamps() -> None:
    """负值应被钳到 0(防御性)。"""
    assert _ms_to_srt_time(-100) == "00:00:00,000"


def test_segments_to_srt_format() -> None:
    """_segments_to_srt 应输出标准 SRT 格式(编号 + 时间码 + 文本 + 空行)。"""
    segs = [
        {"index": 0, "start_ms": 0, "end_ms": 1_500, "text_en": "Hello"},
        {"index": 1, "start_ms": 1_500, "end_ms": 3_000, "text_en": "World"},
    ]
    srt = _segments_to_srt(segs)
    lines = srt.splitlines()
    # 段 1:1=编号,2=时间码,3=文本,4=空行;段 2:5/6/7/8
    assert lines[0] == "1"
    assert "00:00:00,000 --> 00:00:01,500" in lines[1]
    assert lines[2] == "Hello"
    assert lines[3] == ""
    assert lines[4] == "2"
    assert "00:00:01,500 --> 00:00:03,000" in lines[5]
    assert lines[6] == "World"


def test_read_segments_from_draft(tmp_path: Path) -> None:
    """从 draft_content.json 的 materials.texts 还原 SubtitleSegment。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    draft = {
        "canvas_config": {},
        "materials": {
            "texts": [
                {"content": "Hello", "target_timerange": {"start": 1_000_000, "duration": 2_000_000}},
                {"content": "World", "target_timerange": {"start": 3_000_000, "duration": 2_000_000}},
            ]
        },
    }
    (draft_dir / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    segs = _read_segments_from_draft(draft_dir)
    assert len(segs) == 2
    # start=1_000_000 us → 1000 ms; duration=2_000_000 us → 2000 ms
    assert segs[0]["start_ms"] == 1000
    assert segs[0]["end_ms"] == 3000
    assert segs[0]["text_en"] == "Hello"
