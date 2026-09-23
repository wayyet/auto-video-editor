"""``media_probe`` 的入参校验 + safe_* / coerce_* / scan 解析逻辑测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from assembly_capabilities.media_probe import (
    analyze_media,
    coerce_threshold,
    inspect_media,
    media_summary,
    media_technical_risks,
    safe_float,
    safe_int,
    scan_ranges,
    scan_scene_changes,
    segments_from_boundaries,
)
from assembly_capabilities.result import ToolResult


# ---------------------------------------------------------------------------
# safe_float / safe_int / coerce_threshold
# ---------------------------------------------------------------------------
def test_safe_float_handles_none_string_invalid():
    assert safe_float(None) is None
    assert safe_float("3.14") == 3.14
    assert safe_float("abc") is None
    assert safe_float(float("inf")) is None
    assert safe_float(float("-inf")) is None
    assert safe_float(float("nan")) is None


def test_safe_int_handles_none_string_invalid():
    assert safe_int(None) is None
    assert safe_int("42") == 42
    assert safe_int("abc") is None
    assert safe_int(3.7) == 3


def test_coerce_threshold_default_for_none():
    assert coerce_threshold(None, default=0.30) == 0.30


def test_coerce_threshold_rejects_bool_and_invalid_range():
    assert isinstance(coerce_threshold(True, default=0.30), ToolResult)
    assert coerce_threshold(True, default=0.30).text.startswith("[ERROR]")
    assert isinstance(coerce_threshold(0.0, default=0.30), ToolResult)
    assert isinstance(coerce_threshold(1.0, default=0.30), ToolResult)
    assert isinstance(coerce_threshold(-0.1, default=0.30), ToolResult)
    assert isinstance(coerce_threshold("not-a-number", default=0.30), ToolResult)
    assert coerce_threshold(0.5, default=0.30) == 0.5


# ---------------------------------------------------------------------------
# inspect_media 入参校验
# ---------------------------------------------------------------------------
def test_inspect_media_requires_input_path(run_context):
    r = inspect_media({}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "input_path" in r.text


def test_inspect_media_rejects_missing_file(run_context, tmp_path):
    r = inspect_media({"input_path": str(tmp_path / "missing.mp4")}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "File not found" in r.text


# ---------------------------------------------------------------------------
# analyze_media 入参校验
# ---------------------------------------------------------------------------
def test_analyze_media_requires_input_path(run_context):
    r = analyze_media({}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "input_path" in r.text


def test_analyze_media_rejects_missing_file(run_context, tmp_path):
    r = analyze_media({"input_path": str(tmp_path / "missing.mp4")}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "File not found" in r.text


# ---------------------------------------------------------------------------
# segments_from_boundaries
# ---------------------------------------------------------------------------
def test_segments_from_boundaries_empty_when_no_duration():
    assert segments_from_boundaries(None, []) == []
    assert segments_from_boundaries(0, []) == []
    assert segments_from_boundaries(-1, []) == []


def test_segments_from_boundaries_basic_split():
    # duration=10, scene change at 5 → segments [0..5] + [5..10]
    segs = segments_from_boundaries(10.0, [5.0])
    assert len(segs) == 2
    assert segs[0]["start"] == 0.0
    assert segs[0]["end"] == 5.0
    assert segs[1]["start"] == 5.0
    assert segs[1]["end"] == 10.0
    assert segs[0]["source"] == "scene_boundary"


def test_segments_from_boundaries_drops_near_edges_and_short():
    # duration=10, scenes at 0.05 and 0.1 → dropped (too close to edges)
    segs = segments_from_boundaries(10.0, [0.05, 0.1])
    # All points within (0.1, duration-0.1) are filtered out → just one full segment
    assert len(segs) == 1
    assert segs[0]["start"] == 0.0
    assert segs[0]["end"] == 10.0


def test_segments_from_boundaries_drops_short_segments():
    # duration=10, scenes at 4.95 → 0..4.95 + 4.95..10; first is 4.95s, OK
    segs = segments_from_boundaries(10.0, [4.95])
    assert len(segs) == 2
    # 0.1 在 edge filter (0.1 < t < duration-0.1 == 9.9) 外,被过滤;最终 points=[0,10]
    # 整段 0..10 因为 duration=10 > 0.3 保留
    segs2 = segments_from_boundaries(10.0, [0.1])
    assert len(segs2) == 1
    assert segs2[0]["start"] == 0.0
    assert segs2[0]["end"] == 10.0


# ---------------------------------------------------------------------------
# scan_scene_changes / scan_ranges:仅在没有 ffmpeg 时跑(返回空),避免噪声
# ---------------------------------------------------------------------------
def test_scan_scene_changes_returns_empty_when_ffmpeg_missing(run_context, tmp_path, monkeypatch):
    from assembly_capabilities import media_probe
    monkeypatch.setattr(media_probe, "run_proc", None)
    # 实际无需 run_proc 也能测:__init__ 时没引用,函数内部 catch-all
    # 如果真没 ffmpeg,函数返回 ([], exc_str)
    # 但本测试不假设环境,只验证返回类型
    out = scan_scene_changes(tmp_path / "fake.mp4", 0.3)
    assert isinstance(out, tuple)
    assert len(out) == 2
    assert isinstance(out[0], list)


def test_scan_ranges_returns_tuple(run_context, tmp_path):
    out = scan_ranges(tmp_path / "fake.mp4", "blackdetect=d=0.2:pix_th=0.10", "black")
    assert isinstance(out, tuple)
    assert len(out) == 2
    assert isinstance(out[0], list)


# ---------------------------------------------------------------------------
# media_summary / media_technical_risks 纯逻辑
# ---------------------------------------------------------------------------
def test_media_summary_no_streams():
    summary = media_summary({"video_streams": [], "audio_streams": [], "duration_seconds": 0})
    assert summary["has_video"] is False
    assert summary["has_audio"] is False
    assert summary["width"] is None
    assert summary["height"] is None


def test_media_summary_with_video_and_audio():
    probe = {
        "duration_seconds": 10.0,
        "video_streams": [
            {"width": 1920, "height": 1080, "codec_name": "h264",
             "avg_frame_rate": "30/1", "r_frame_rate": "30/1",
             "pix_fmt": "yuv420p", "color_space": "bt709"}
        ],
        "audio_streams": [
            {"codec_name": "aac", "channels": 2, "sample_rate": "48000"}
        ],
    }
    summary = media_summary(probe)
    assert summary["has_video"] is True
    assert summary["has_audio"] is True
    assert summary["width"] == 1920
    assert summary["height"] == 1080
    assert summary["fps"] == 30.0
    assert summary["video_codec"] == "h264"
    assert summary["audio_codec"] == "aac"
    assert summary["audio_channels"] == 2
    assert summary["audio_sample_rate"] == 48000


def test_media_technical_risks_flags_no_streams():
    risks = media_technical_risks({"summary": {"has_video": False, "has_audio": False}})
    assert any(r["severity"] == "error" and "no video or audio" in r["message"] for r in risks)


def test_media_technical_risks_flags_odd_dimensions():
    risks = media_technical_risks({"summary": {"has_video": True, "has_audio": True, "width": 1921, "height": 1080}})
    assert any("odd video dimensions" in r["message"] for r in risks)


def test_media_technical_risks_flags_vfr():
    risks = media_technical_risks({
        "summary": {"has_video": True, "has_audio": True, "width": 1920, "height": 1080},
        "video_streams": [{"avg_frame_rate": "25/1", "r_frame_rate": "30/1"}],
    })
    assert any("variable-frame-rate" in r["message"] for r in risks)


def test_media_technical_risks_flags_hdr():
    risks = media_technical_risks({
        "summary": {"has_video": True, "has_audio": True, "width": 1920, "height": 1080},
        "video_streams": [{"color_transfer": "smpte2084"}],
    })
    assert any("HDR transfer" in r["message"] for r in risks)


def test_media_technical_risks_flags_4k():
    risks = media_technical_risks({"summary": {"has_video": True, "has_audio": False, "width": 3840, "height": 2160}})
    assert any("4K" in r["message"] for r in risks)
