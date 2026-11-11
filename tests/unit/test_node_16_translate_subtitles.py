"""node_16_translate_subtitles 纯函数单测 — SRT 序列化、layout 校验、草稿重读、关卡③ payload。

2026-11-12 noop 治理阶段4:原文件中的 shim 级用例(直接调用未注册进图的
``node_16_translate_subtitles()``)已随 shim 一起删除;其覆盖的节点行为由
``tests/unit/test_node_16a_translate_and_check.py``(13 例)与
``tests/unit/test_node_checkpoint3_layout_review.py``(5 例)承接。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_16_translate_subtitles import (
    _build_interrupt_payload,
    _ms_to_srt_time,
    _read_segments_from_draft,
    _segments_to_srt,
    validate_layout,
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


def test_validate_layout_no_font_fallback() -> None:
    """无字体时回退到 PIL load_default,而非报错(便于联调跑通)。

    实际生产应在缺失字体时返回 missing_font issue;此处覆盖"默认能跑"。
    """
    issues = validate_layout(
        segments=[{"index": 0, "text_en": "Hi"}],
        font_path=None,
        font_size=48,
        max_width_px=10000,  # 故意很大,确保不触发 text_too_wide
    )
    # 不应有 missing_font(PIL load_default 总是可用)
    assert all("missing_font" not in i["reason"] for i in issues)


def test_validate_layout_missing_font_via_invalid_path() -> None:
    """指定不存在的 font_path + 无系统字体时 → missing_font issue。"""
    # 这个测试在 Windows 上无法构造(PIL load_default 永远可用),跳过
    pytest.skip("Windows 上 PIL load_default 永远可用,无法触发 missing_font issue")


def test_validate_layout_text_too_wide(tmp_path: Path) -> None:
    """超宽英文 → text_too_wide。"""
    # 找一个真实可用的字体
    candidates = [
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\simhei.ttf",
    ]
    font_path = next((c for c in candidates if Path(c).exists()), None)
    if font_path is None:
        pytest.skip("无可用字体文件")
    long_text = "VeryLongEnglishWord " * 20  # 故意超宽
    issues = validate_layout(
        segments=[{"index": 0, "text_en": long_text}],
        font_path=font_path,
        font_size=48,
        max_width_px=100,  # 故意压小
    )
    assert any(i.get("reason") == "text_too_wide" for i in issues)


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


def test_build_interrupt_payload() -> None:
    payload = _build_interrupt_payload(
        {"draft_dir_en_branch": "/path/to/en"},
        [{"index": 0, "reason": "text_too_wide"}],
    )
    assert payload["checkpoint"] == "③"
    assert "英文字幕排版异常" in payload["reason"]
    assert payload["draft_dir"] == "/path/to/en"
    assert len(payload["issues"]) == 1
