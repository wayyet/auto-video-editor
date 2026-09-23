"""共享 helper 模块的纯逻辑测试(ffproc / fonts / transcript)。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from assembly_capabilities.ffproc import (
    escape_option,
    escape_text,
    safe_expr,
)
from assembly_capabilities.transcript import (
    collect_segments,
    first_present,
    format_segment,
    read_transcript_text,
    transcript_data_to_text,
    transcript_text_for_range,
)


# ---------------------------------------------------------------------------
# ffproc
# ---------------------------------------------------------------------------
def test_escape_text_handles_backslash_and_quote():
    assert escape_text("hi") == "hi"
    assert escape_text("a\\b") == "a\\\\b"
    assert escape_text("a'b") == "a'\\''b"
    # comma / colon inside drawtext are NOT escaped (only backslash + quote)
    assert escape_text("a,b:c") == "a,b:c"


def test_escape_text_none_returns_empty():
    assert escape_text(None) == ""


def test_escape_option_escapes_filter_specials():
    # backslash must be escaped first, otherwise re-doubles
    assert escape_option("a\\b") == "a\\\\b"
    for ch in ["\\", ":", "'", ",", ";", "[", "]"]:
        out = escape_option(f"x{ch}y")
        # ch must appear as \ch
        assert f"\\{ch}" in out


def test_safe_expr_allows_basic_arithmetic():
    assert safe_expr("(w-text_w)/2", "DEFAULT") == "(w-text_w)/2"
    assert safe_expr("100+200", "DEFAULT") == "100+200"


def test_safe_expr_rejects_dangerous_chars():
    # regex 允许算术 + 括号 + 比较 + 空格 + 逗号;拒绝 : ; [ ] = 等
    assert safe_expr("$(rm -rf)", "DEFAULT") == "DEFAULT"
    assert safe_expr("a:b", "DEFAULT") == "DEFAULT"
    assert safe_expr("a;b", "DEFAULT") == "DEFAULT"
    assert safe_expr("a=b", "DEFAULT") == "DEFAULT"
    assert safe_expr("[0]", "DEFAULT") == "DEFAULT"
    # 算术表达式内合法字符应原样返回
    assert safe_expr("100,200", "DEFAULT") == "100,200"
    assert safe_expr("(a+b)/2", "DEFAULT") == "(a+b)/2"
    assert safe_expr("", "DEFAULT") == "DEFAULT"
    assert safe_expr(None, "DEFAULT") == "DEFAULT"


# ---------------------------------------------------------------------------
# transcript
# ---------------------------------------------------------------------------
def test_first_present_returns_first_non_null_key():
    seg = {"start": 1.0, "end": 2.0}
    assert first_present(seg, ["start", "end"]) == 1.0


def test_first_present_returns_none_when_all_missing_or_none():
    assert first_present({"a": 1}, ["x", "y"]) is None
    assert first_present({"x": None}, ["x"]) is None


def test_format_segment_with_timestamps_and_speaker():
    seg = {"start": 1.0, "end": 2.5, "speaker": "spk1", "text": "hello"}
    out = format_segment(seg)
    assert "[1.00s-2.50s]" in out
    assert "[spk1]" in out
    assert "hello" in out


def test_format_segment_minimal():
    assert format_segment({"text": "plain"}) == "plain"


def test_transcript_data_to_text_handles_string_dict_list():
    assert transcript_data_to_text("raw text") == "raw text"
    assert "seg1" in transcript_data_to_text([{"text": "seg1", "start": 0.0}])
    assert "hi" in transcript_data_to_text({"text": "hi"})


def test_collect_segments_handles_list_and_dict_with_segments_key():
    assert collect_segments([{"text": "a"}, {"text": "b"}]) == [{"text": "a"}, {"text": "b"}]
    assert collect_segments({"segments": [{"text": "a"}]}) == [{"text": "a"}]
    assert collect_segments({"results": [{"text": "a"}]}) == [{"text": "a"}]
    assert collect_segments({"text": "no segments key"}) == []


def test_read_transcript_text_returns_inline_when_provided():
    assert read_transcript_text(inline_text="  hi  ") == "hi"


def test_read_transcript_text_returns_empty_for_missing_file(tmp_path):
    assert read_transcript_text(tmp_path / "missing.json") == ""


def test_read_transcript_text_reads_plain_text_file(tmp_path):
    p = tmp_path / "sub.txt"
    p.write_text("line1\nline2", encoding="utf-8")
    assert "line1" in read_transcript_text(p)


def test_read_transcript_text_parses_json_with_segments(tmp_path):
    p = tmp_path / "transcript.json"
    p.write_text(json.dumps({"segments": [{"text": "seg1"}, {"text": "seg2"}]}), encoding="utf-8")
    assert "seg1" in read_transcript_text(p)
    assert "seg2" in read_transcript_text(p)


def test_transcript_text_for_range_filters_by_timestamps(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "first"},
            {"start": 5.0, "end": 6.0, "text": "second"},
        ]
    }), encoding="utf-8")
    # window 0.5-0.6 → only first
    out = transcript_text_for_range(p, 0.5, 0.6)
    assert "first" in out
    assert "second" not in out
    # window 4.5-5.5 → only second
    out = transcript_text_for_range(p, 4.5, 5.5)
    assert "second" in out
    assert "first" not in out


def test_transcript_text_for_range_empty_window_returns_marker(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "first"},
        ]
    }), encoding="utf-8")
    out = transcript_text_for_range(p, 5.0, 6.0)
    assert "no transcript segments within this time window" in out


def test_transcript_text_for_range_missing_file_returns_empty(tmp_path):
    assert transcript_text_for_range(tmp_path / "nope.json", 0, 1) == ""


def test_transcript_text_for_range_no_path_returns_empty():
    assert transcript_text_for_range(None, 0, 1) == ""


# ---------------------------------------------------------------------------
# fonts (仅纯逻辑,不动 fc-list / 文件系统)
# ---------------------------------------------------------------------------
def test_normalize_track_type_aliases():
    from assembly_capabilities.timeline_ops import normalize_track_type
    assert normalize_track_type("Main") == "main"
    assert normalize_track_type("main_video") == "main_video"
    assert normalize_track_type("") == ""
    assert normalize_track_type(None) == ""
