"""``qc_preview`` 的纯逻辑 + 入参校验测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from assembly_capabilities.qc_preview import (
    audio_duration_from_media,
    clip_render_duration_for_qc,
    edit_boundary_black_issues,
    expected_from_timeline,
    first_video_stream,
    parse_db_value,
    parse_filter_ranges,
    parse_volumedetect,
    qc_preview,
    render_binding_issues,
    timeline_duration,
    timeline_expectation_issues,
    timeline_video_cut_times,
)
from assembly_capabilities.result import ToolResult


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------
def test_qc_preview_requires_video_path(run_context):
    r = qc_preview({}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "video_path is required" in r.text


def test_qc_preview_rejects_missing_video(run_context, tmp_path):
    r = qc_preview({"video_path": str(tmp_path / "missing.mp4")}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "video not found" in r.text


def test_qc_preview_rejects_missing_timeline(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    r = qc_preview({
        "video_path": str(src),
        "timeline_path": str(tmp_path / "missing.json"),
    }, run_context)
    assert r.text.startswith("[ERROR]")
    assert "timeline not found" in r.text


# ---------------------------------------------------------------------------
# parse_volumedetect / parse_db_value
# ---------------------------------------------------------------------------
def test_parse_volumedetect_extracts_mean_and_max():
    log = """
[Parsed_volumedetect_0 @ 0x1234] mean_volume: -23.4 dB
[Parsed_volumedetect_0 @ 0x1234] max_volume: -1.2 dB
"""
    stats = parse_volumedetect(log)
    assert stats["mean_volume_db"] == -23.4
    assert stats["max_volume_db"] == -1.2


def test_parse_volumedetect_handles_empty():
    assert parse_volumedetect("") == {}


def test_parse_db_value_basic():
    assert parse_db_value("mean_volume: -23.4 dB") == -23.4
    assert parse_db_value("random: text") is None
    assert parse_db_value("") is None


# ---------------------------------------------------------------------------
# audio_duration_from_media / first_video_stream
# ---------------------------------------------------------------------------
def test_audio_duration_from_media_picks_max():
    media = {"audio_streams": [{"duration": "5"}, {"duration": "8"}, {"duration": "3"}]}
    assert audio_duration_from_media(media) == 8.0


def test_audio_duration_from_media_empty_returns_none():
    assert audio_duration_from_media({}) is None
    assert audio_duration_from_media({"audio_streams": []}) is None
    assert audio_duration_from_media({"audio_streams": [{}]}) is None


def test_first_video_stream_picks_first():
    s1 = {"width": 1920}
    s2 = {"width": 1280}
    assert first_video_stream({"video_streams": [s1, s2]}) == s1


def test_first_video_stream_empty():
    assert first_video_stream({}) == {}
    assert first_video_stream({"video_streams": []}) == {}


# ---------------------------------------------------------------------------
# parse_filter_ranges
# ---------------------------------------------------------------------------
def test_parse_filter_ranges_basic():
    log = """
[blackdetect @ 0x1] black_start:1.500
[blackdetect @ 0x1] black_end:3.200 black_duration:1.700
"""
    ranges = parse_filter_ranges(log, "black")
    assert len(ranges) == 1
    assert ranges[0]["start"] == 1.5
    assert ranges[0]["end"] == 3.2


def test_parse_filter_ranges_empty():
    assert parse_filter_ranges("", "black") == []


def test_parse_filter_ranges_skips_invalid():
    # 没有 start 也没有 duration 推算 → 跳过
    log = "[blackdetect @ 0x1] black_end:3.200"
    assert parse_filter_ranges(log, "black") == []


# ---------------------------------------------------------------------------
# clip_render_duration_for_qc
# ---------------------------------------------------------------------------
def test_clip_render_duration_for_qc_end_minus_start():
    assert clip_render_duration_for_qc({"start": 0, "end": 2}) == 2.0


def test_clip_render_duration_for_qc_uses_duration():
    assert clip_render_duration_for_qc({"start": 1, "duration": 3}) == 3.0


def test_clip_render_duration_for_qc_with_speed():
    assert clip_render_duration_for_qc({"start": 0, "end": 4, "speed": 2.0}) == 2.0


def test_clip_render_duration_for_qc_zero_speed_returns_none():
    # 原版 `safe_float(clip.get("speed")) or 1.0` 会把 0.0 当成 falsy 取 1.0,
    # 所以 speed=0 不会触发 "if speed <= 0: return None" 分支(忠实于 video-agent-kit 原行为)
    assert clip_render_duration_for_qc({"start": 0, "end": 1, "speed": 0.0}) == 1.0
    # speed=-1 才被判定非法
    assert clip_render_duration_for_qc({"start": 0, "end": 1, "speed": -1}) is None


def test_clip_render_duration_for_qc_no_end_no_duration():
    assert clip_render_duration_for_qc({"start": 0}) is None


# ---------------------------------------------------------------------------
# timeline_duration / timeline_video_cut_times
# ---------------------------------------------------------------------------
def test_timeline_duration_top_level_clips():
    data = {"clips": [{"start": 0, "end": 2}, {"start": 2, "end": 5}]}
    assert timeline_duration(data) == 5.0


def test_timeline_duration_tracks_with_video():
    data = {"tracks": [{"type": "video", "clips": [
        {"start": 0, "end": 2},
        {"start": 2, "end": 4},
    ]}]}
    assert timeline_duration(data) == 4.0


def test_timeline_duration_empty_returns_none():
    assert timeline_duration({}) is None


def test_timeline_video_cut_times_tracks_video_only():
    data = {"tracks": [
        {"type": "video", "clips": [
            {"start": 0, "end": 5, "timeline_start": 0},
            {"start": 5, "end": 10, "timeline_start": 5},
        ]},
        {"type": "audio", "clips": [{"start": 0, "end": 10}]},
    ]}
    cuts = timeline_video_cut_times(_with_tmp(data))
    assert 5.0 in cuts


def test_timeline_video_cut_times_empty_when_no_video_track():
    data = {"tracks": [{"type": "audio", "clips": [{"start": 0, "end": 5}]}]}
    cuts = timeline_video_cut_times(_with_tmp(data))
    assert cuts == []


def test_timeline_video_cut_times_top_level_clips():
    data = {"clips": [{"start": 0, "end": 5}, {"start": 5, "end": 10}], "duration": 15}
    cuts = timeline_video_cut_times(_with_tmp(data))
    # duration 没写在 data 顶层,会 fallback 到 tracks 不存在 → 计算 top-level
    assert 5.0 in cuts


def _with_tmp(data: dict) -> Path:
    import tempfile, json
    p = Path(tempfile.mktemp(suffix=".json"))
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# expected_from_timeline
# ---------------------------------------------------------------------------
def test_expected_from_timeline_from_output_canvas(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({
        "sequence": {"duration": 10, "fps": 30},
        "output_canvas": {"width": 1920, "height": 1080, "fps": 30},
    }), encoding="utf-8")
    out = expected_from_timeline(p)
    assert out["duration_seconds"] == 10.0
    assert out["width"] == 1920
    assert out["height"] == 1080
    assert out["fps"] == 30.0


def test_expected_from_timeline_returns_error_for_broken_file(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("not json", encoding="utf-8")
    out = expected_from_timeline(p)
    assert "error" in out


# ---------------------------------------------------------------------------
# timeline_expectation_issues
# ---------------------------------------------------------------------------
def test_timeline_expectation_issues_passes_when_matches():
    issues = timeline_expectation_issues(
        {"duration_seconds": 10.0, "video_streams": [{"width": 1080, "height": 1920, "avg_frame_rate": "30/1"}]},
        {"duration_seconds": 10.0, "width": 1080, "height": 1920, "fps": 30.0},
    )
    assert issues == []


def test_timeline_expectation_issues_flags_duration_mismatch():
    issues = timeline_expectation_issues(
        {"duration_seconds": 10.0, "video_streams": [{"width": 1080, "height": 1920, "avg_frame_rate": "30/1"}]},
        {"duration_seconds": 12.0, "width": 1080, "height": 1920, "fps": 30.0},
    )
    assert any("duration does not match" in i["message"] for i in issues)


def test_timeline_expectation_issues_flags_resolution_mismatch():
    issues = timeline_expectation_issues(
        {"duration_seconds": 10.0, "video_streams": [{"width": 720, "height": 1280, "avg_frame_rate": "30/1"}]},
        {"duration_seconds": 10.0, "width": 1080, "height": 1920, "fps": 30.0},
    )
    assert any("resolution does not match" in i["message"] for i in issues)


def test_timeline_expectation_issues_flags_fps_drift():
    issues = timeline_expectation_issues(
        {"duration_seconds": 10.0, "video_streams": [{"width": 1080, "height": 1920, "avg_frame_rate": "24/1"}]},
        {"duration_seconds": 10.0, "width": 1080, "height": 1920, "fps": 30.0},
    )
    assert any("fps differs" in i["message"] for i in issues)


# ---------------------------------------------------------------------------
# edit_boundary_black_issues
# ---------------------------------------------------------------------------
def test_edit_boundary_black_issues_empty_for_no_ranges(tmp_path):
    timeline = tmp_path / "t.json"
    timeline.write_text(json.dumps({"clips": [{"start": 0, "end": 5}]}), encoding="utf-8")
    assert edit_boundary_black_issues(timeline, "") == []


def test_edit_boundary_black_issues_flags_hit_near_cut(tmp_path):
    timeline = tmp_path / "t.json"
    timeline.write_text(json.dumps({
        "duration": 10,
        "clips": [
            {"start": 0, "end": 5, "timeline_start": 0},
            {"start": 5, "end": 10, "timeline_start": 5},
        ]
    }), encoding="utf-8")
    # 黑帧从 4.95 开始 → 在 cut 5.0 附近
    log = "[blackdetect @ 0x1] black_start:4.950\n[blackdetect @ 0x1] black_end:5.100 black_duration:0.150"
    issues = edit_boundary_black_issues(timeline, log)
    assert len(issues) == 1
    assert "edit boundary" in issues[0]["message"]


# ---------------------------------------------------------------------------
# render_binding_issues
# ---------------------------------------------------------------------------
def test_render_binding_issues_flags_missing_report(tmp_path):
    video = tmp_path / "preview.mp4"
    video.write_bytes(b"\x00" * 1024)
    issues = render_binding_issues(video, "abc", "def")
    assert len(issues) == 1
    assert "render report is missing" in issues[0]["message"]


def test_render_binding_issues_flags_sha_mismatch(tmp_path):
    video = tmp_path / "preview.mp4"
    video.write_bytes(b"\x00" * 1024)
    report_path = tmp_path / "preview.render_report.json"
    report_path.write_text(json.dumps({
        "output_sha256": "DIFFERENT",
        "timeline_sha256": "abc",
    }), encoding="utf-8")
    issues = render_binding_issues(video, "actual-sha", "abc")
    assert any("does not describe the current preview" in i["message"] for i in issues)


def test_render_binding_issues_flags_timeline_sha_mismatch(tmp_path):
    video = tmp_path / "preview.mp4"
    video.write_bytes(b"\x00" * 1024)
    sha = "actual-sha"
    report_path = tmp_path / "preview.render_report.json"
    report_path.write_text(json.dumps({
        "output_sha256": sha,
        "timeline_sha256": "DIFFERENT",
    }), encoding="utf-8")
    issues = render_binding_issues(video, sha, "current-timeline-sha")
    assert any("different timeline" in i["message"] for i in issues)


def test_render_binding_issues_passes_when_match(tmp_path):
    video = tmp_path / "preview.mp4"
    video.write_bytes(b"\x00" * 1024)
    sha = "actual-sha"
    tsha = "timeline-sha"
    report_path = tmp_path / "preview.render_report.json"
    report_path.write_text(json.dumps({
        "output_sha256": sha,
        "timeline_sha256": tsha,
    }), encoding="utf-8")
    issues = render_binding_issues(video, sha, tsha)
    assert issues == []
