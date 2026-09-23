"""``render_preview`` 的纯逻辑 + 入参校验测试(默认不调 ffmpeg)。

真渲染测试由 ``ASSEMBLY_TEST_REAL_FFMPEG=1`` 开关单独跑。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from assembly_capabilities.render_preview import (
    atempo_chain,
    audio_bed_filter,
    bake_project_audio_beds,
    between_expr,
    cut_clip,
    edit_decisions_from_plan,
    even_dimension,
    filter_number,
    filter_path,
    filter_string,
    normalize_project_clip,
    probe_video_size,
    render_preview,
    renderable_video_clips,
    renderable_timeline_clips,
    render_audio_bed,
    render_project_timeline,
    resolve_render_size,
    source_has_audio,
    source_has_video,
    transition_fade_filter,
    video_clip_filter,
    video_end_pad,
    overlay_clip_filter,
    public_render_plan,
    ffconcat_escape,
    drawtext_filter,
    resolve_project_fps,
    resolve_project_duration,
    resolve_project_render_size,
)
from assembly_capabilities.result import ToolResult


# ---------------------------------------------------------------------------
# render_preview 入参校验
# ---------------------------------------------------------------------------
def test_render_preview_requires_timeline_path(run_context):
    r = render_preview({}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "timeline_path is required" in r.text


def test_render_preview_rejects_missing_file(run_context, tmp_path):
    r = render_preview({"timeline_path": str(tmp_path / "missing.json")}, run_context)
    assert r.text.startswith("[ERROR]")
    # 原版 render_preview 先检 ffmpeg/ffprobe;若环境没有则报 "ffmpeg/ffprobe not found",
    # 否则报 "timeline not found"
    assert ("timeline not found" in r.text) or ("ffmpeg" in r.text)


def test_render_preview_rejects_invalid_json(run_context, tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("not json", encoding="utf-8")
    r = render_preview({"timeline_path": str(p)}, run_context)
    assert r.text.startswith("[ERROR]")
    assert ("invalid JSON" in r.text) or ("ffmpeg" in r.text)


# ---------------------------------------------------------------------------
# even_dimension
# ---------------------------------------------------------------------------
def test_even_dimension_rounds_down_odd():
    assert even_dimension(2) == 2
    assert even_dimension(3) == 2
    assert even_dimension(1921) == 1920
    assert even_dimension(1920) == 1920


def test_even_dimension_rejects_bool():
    with pytest.raises(RuntimeError, match="integers, not booleans"):
        even_dimension(True)


def test_even_dimension_rejects_zero():
    with pytest.raises(RuntimeError, match="finite positive"):
        even_dimension(0)
    with pytest.raises(RuntimeError, match="finite positive"):
        even_dimension(-1)


def test_even_dimension_handles_small_values():
    assert even_dimension(1) == 2  # 1 < 2 → 返回 2


# ---------------------------------------------------------------------------
# resolve_render_size
# ---------------------------------------------------------------------------
def test_resolve_render_size_requires_both_dims():
    with pytest.raises(RuntimeError, match="must be provided together"):
        resolve_render_size({"output_width": 100}, [])
    with pytest.raises(RuntimeError, match="must be provided together"):
        resolve_render_size({"output_height": 100}, [])


def test_resolve_render_size_rejects_bool():
    # 原版先抛 ValueError("render dimensions must not be booleans"),
    # 然后被外层 except 转 RuntimeError("output_width/output_height must be integers")
    with pytest.raises(RuntimeError, match="must be integers"):
        resolve_render_size({"output_width": True, "output_height": True}, [])


def test_resolve_render_size_rejects_negative():
    with pytest.raises(RuntimeError, match="positive"):
        resolve_render_size({"output_width": -1, "output_height": -1}, [])


def test_resolve_render_size_evenizes():
    w, h = resolve_render_size({"output_width": 1921, "output_height": 1081}, [])
    assert w == 1920
    assert h == 1080


# ---------------------------------------------------------------------------
# renderable_video_clips / renderable_timeline_clips
# ---------------------------------------------------------------------------
def test_renderable_video_clips_splits_by_track_type():
    clips = [
        {"track_type": "video"},
        {"track_type": "audio"},
        {"track_type": "subtitle"},
    ]
    renderable, unsupported = renderable_video_clips(clips)
    assert len(renderable) == 1
    assert renderable[0]["track_type"] == "video"
    assert len(unsupported) == 2


def test_renderable_video_clips_empty_track_type_unsupported():
    """未指定 track_type 的 clip 不能被默认当 video clip 渲染(契约与 validate 一致)。"""
    clips = [{"track_type": ""}]
    renderable, unsupported = renderable_video_clips(clips)
    assert renderable == []
    assert len(unsupported) == 1


def test_renderable_timeline_clips_top_level_clips_returns_all():
    data = {"clips": [{"a": 1}, {"b": 2}]}
    clips, unsupported = renderable_timeline_clips(data)
    assert len(clips) == 2
    assert unsupported == []


def test_renderable_timeline_clips_tracks_filters_by_type():
    data = {"tracks": [{"type": "video", "clips": [{"a": 1}]}, {"type": "audio", "clips": [{"b": 2}]}]}
    clips, unsupported = renderable_timeline_clips(data)
    assert len(clips) == 1
    assert clips[0]["a"] == 1
    assert len(unsupported) == 1


# ---------------------------------------------------------------------------
# normalize_project_clip
# ---------------------------------------------------------------------------
def test_normalize_project_clip_resolves_source(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    track = {"type": "video"}
    raw = {"source": str(src), "start": 1.0, "end": 3.0}
    out = normalize_project_clip(raw, run_context, track, 0, 0, 0.0)
    assert out["start"] == 1.0
    assert out["end"] == 3.0
    assert out["source_duration"] == 2.0
    assert out["render_duration"] == 2.0
    assert out["track_type"] == "video"
    assert out["track_index"] == 0


def test_normalize_project_clip_uses_duration_when_end_missing(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    track = {"type": "video"}
    raw = {"source": str(src), "start": 1.0, "duration": 4.0}
    out = normalize_project_clip(raw, run_context, track, 0, 0, 0.0)
    assert out["end"] == 5.0
    assert out["render_duration"] == 4.0


def test_normalize_project_clip_rejects_negative_timeline_start(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    track = {"type": "video"}
    raw = {"source": str(src), "start": 0.0, "end": 1.0, "timeline_start": -1.0}
    with pytest.raises(RuntimeError, match="timeline_start must be finite and >= 0"):
        normalize_project_clip(raw, run_context, track, 0, 0, 0.0)


def test_normalize_project_clip_rejects_zero_speed(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    track = {"type": "video"}
    # 原版 `float(raw.get("speed") or 1.0)`:speed=0.0 falsy → 取 1.0 → 不抛
    # speed=-1 才是 falsy 之外的真负数 → 触发 "speed must be finite and > 0"
    raw_neg = {"source": str(src), "start": 0.0, "end": 1.0, "speed": -1.0}
    with pytest.raises(RuntimeError, match="speed must be finite and > 0"):
        normalize_project_clip(raw_neg, run_context, track, 0, 0, 0.0)


# ---------------------------------------------------------------------------
# resolve_project_fps / duration / render_size
# ---------------------------------------------------------------------------
def test_resolve_project_fps_from_output_canvas():
    data = {"output_canvas": {"fps": 60}}
    assert resolve_project_fps(data) == 60.0


def test_resolve_project_fps_from_sequence():
    data = {"sequence": {"fps": 24}}
    assert resolve_project_fps(data) == 24.0


def test_resolve_project_fps_defaults_to_30():
    assert resolve_project_fps({}) == 30.0


def test_resolve_project_duration_from_sequence():
    data = {"sequence": {"duration": 35.0}}
    assert resolve_project_duration(data) == 35.0


def test_resolve_project_duration_from_clips():
    data = {"sequence": {}}
    clips = [{"timeline_end": 5.0}, {"timeline_end": 8.0}]
    assert resolve_project_duration(data, clips) == 8.0


def test_resolve_project_render_size_from_output_canvas():
    data = {"output_canvas": {"width": 1921, "height": 1081}}
    w, h = resolve_project_render_size({}, data, [])
    assert w == 1920
    assert h == 1080


def test_resolve_project_render_size_requires_video_when_no_canvas():
    with pytest.raises(RuntimeError, match="cannot infer canvas"):
        resolve_project_render_size({}, {}, [])


# ---------------------------------------------------------------------------
# filter helpers
# ---------------------------------------------------------------------------
def test_filter_number_handles_invalid():
    assert filter_number(None, "0") == "0"
    assert filter_number("not a number", "0") == "0"
    assert filter_number(float("inf"), "0") == "0"
    assert filter_number(3.14, "0") == "3.140000"


def test_filter_path_escapes_specials():
    p = Path("C:\\test\\file.mp4")
    out = filter_path(p)
    # 反斜杠必须先转义
    assert "\\\\" in out or "C:" in out  # Windows 路径


def test_filter_string_escapes_specials():
    assert filter_string("a:b") == "a\\:b"
    assert filter_string("a'b") == "a\\'b"
    assert filter_string("a,b") == "a\\,b"


def test_atempo_chain_handles_normal_speed():
    assert atempo_chain(1.0) == "atempo=1.00000000"


def test_atempo_chain_handles_fast_speed():
    chain = atempo_chain(4.0)
    # 4.0 > 2.0 → atempo=2.0, atempo=2.0
    assert "atempo=2.0" in chain


def test_atempo_chain_handles_slow_speed():
    chain = atempo_chain(0.25)
    # 0.25 < 0.5 → atempo=0.5, atempo=0.5
    assert "atempo=0.5" in chain


def test_transition_fade_filter_no_transition():
    clip = {"render_duration": 5.0}
    assert transition_fade_filter(clip) == ""


def test_transition_fade_filter_with_fade_in():
    clip = {"render_duration": 5.0, "transition_in": {"type": "fade", "duration": 1.0}}
    out = transition_fade_filter(clip)
    assert "fade=t=in" in out


def test_transition_fade_filter_with_fade_out():
    clip = {"render_duration": 5.0, "transition_out": {"type": "crossfade", "duration": 1.0}}
    out = transition_fade_filter(clip)
    assert "fade=t=out" in out
    # start should be render_duration - duration = 4.0
    assert "st=4.000000" in out


def test_video_end_pad_in_range():
    assert 0.04 <= video_end_pad({"fps": 30.0}) <= 0.12
    assert 0.04 <= video_end_pad({"fps": 60.0}) <= 0.12


def test_video_end_pad_handles_bad_fps():
    assert video_end_pad({}) == 0.1
    assert video_end_pad({"fps": 0}) == 0.1
    assert video_end_pad({"fps": float("inf")}) == 0.1


def test_between_expr():
    clip = {"timeline_start": 1.0, "timeline_end": 5.0}
    out = between_expr(clip)
    assert "between(t,1.000000,5.000000)" in out


def test_between_expr_with_end_pad():
    clip = {"timeline_start": 1.0, "timeline_end": 5.0}
    out = between_expr(clip, end_pad=0.1)
    assert "between(t,1.000000,5.100000)" in out


# ---------------------------------------------------------------------------
# public_render_plan / edit_decisions_from_plan
# ---------------------------------------------------------------------------
def test_public_render_plan_strips_cmd_filter_complex():
    plan = {"a": 1, "cmd": ["ffmpeg"], "filter_complex": "..."}
    out = public_render_plan(plan)
    assert "cmd" not in out
    assert "filter_complex" not in out
    assert out["a"] == 1


def test_edit_decisions_from_plan():
    plan = {
        "timeline_path": "/tmp/t.json",
        "timeline_sha256": "abc",
        "width": 1080,
        "height": 1920,
        "fps": 30.0,
        "duration": 35.0,
        "video_clips": [
            {"track_name": "v1", "source": "/tmp/a.mp4", "start": 0, "end": 2,
             "timeline_start": 0, "timeline_end": 2, "reason": "intro", "beat": "beat1"},
        ],
        "text_clips": [],
        "overlay_clips": [],
        "audio_clips": [],
        "unsupported_tracks": [],
    }
    out = edit_decisions_from_plan(plan)
    assert out["sequence"] == {"width": 1080, "height": 1920, "fps": 30.0, "duration": 35.0}
    assert len(out["decisions"]) == 1
    assert out["decisions"][0]["reason"] == "intro"


# ---------------------------------------------------------------------------
# ffconcat_escape
# ---------------------------------------------------------------------------
def test_ffconcat_escape_handles_quotes():
    assert ffconcat_escape(Path("/tmp/file's.mp4")) == "/tmp/file'\\''s.mp4"


# ---------------------------------------------------------------------------
# audio_bed_filter / video_clip_filter / overlay_clip_filter
# ---------------------------------------------------------------------------
def test_audio_bed_filter_basic():
    out = audio_bed_filter(0, "aud0")
    assert out == "[0:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[aud0]"


def test_video_clip_filter_basic_shape(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    track = {"type": "video"}
    raw = normalize_project_clip(
        {"source": str(src), "start": 0, "end": 5}, run_context, track, 0, 0, 0.0
    )
    plan = {"width": 1080, "height": 1920, "fps": 30.0}
    out = video_clip_filter(0, raw, plan, "vclip0")
    assert "[0:v]" in out
    assert "[vclip0]" in out
    assert "scale=1080:1920" in out
    assert "fps=30.0" in out


def test_overlay_clip_filter_with_width_height(run_context, tmp_path):
    src = tmp_path / "src.png"
    src.write_bytes(b"\x00" * 1024)
    track = {"type": "image"}
    raw = normalize_project_clip(
        {"source": str(src), "start": 0, "end": 5, "width": 100, "height": 100},
        run_context, track, 0, 0, 0.0,
    )
    plan = {"width": 1080, "height": 1920, "fps": 30.0}
    out = overlay_clip_filter(0, raw, plan, "ov0")
    assert "[0:v]" in out
    assert "scale=100:100" in out


def test_drawtext_filter_uses_default_position_when_missing(run_context, tmp_path):
    clip = {"timeline_start": 1.0, "timeline_end": 5.0}
    out = drawtext_filter("vbase", "vtext0", clip, tmp_path / "text.txt", None)
    assert "drawtext" in out
    assert "textfile=" in out
    # x/y 用默认表达式
    assert "(w-text_w)/2" in out
