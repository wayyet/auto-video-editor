"""``visual_observe`` 的入参校验 + contact sheet / sample_video_frames 错误路径测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from assembly_capabilities.visual_observe import (
    VideoSamplingDefaults,
    build_contact_sheets,
    default_full_prompt,
    observation_signature,
    read_cached_observation,
    reset_frames_dir,
    sample_video_frames,
    validate_defaults,
    video_frames_dir_id,
    video_ingest,
    with_suffix_before_ext,
)
from assembly_capabilities.result import ToolResult


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------
def test_video_ingest_requires_video_path(run_context):
    r = video_ingest({}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "video_path is required" in r.text


def test_video_ingest_rejects_missing_video(run_context, tmp_path):
    r = video_ingest({"video_path": str(tmp_path / "missing.mp4")}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "video not found" in r.text


def test_video_ingest_rejects_missing_transcript_path(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    r = video_ingest(
        {"video_path": str(src), "transcript_path": str(tmp_path / "missing.json")},
        run_context,
    )
    assert r.text.startswith("[ERROR]")
    assert "transcript not found" in r.text


# ---------------------------------------------------------------------------
# validate_defaults
# ---------------------------------------------------------------------------
def test_validate_defaults_rejects_invalid_fps():
    with pytest.raises(ValueError, match="video_fps"):
        validate_defaults(VideoSamplingDefaults(video_fps=0.0))
    with pytest.raises(ValueError, match="video_fps"):
        validate_defaults(VideoSamplingDefaults(video_fps=float("inf")))


def test_validate_defaults_rejects_bad_patch_size():
    with pytest.raises(ValueError, match="video_t_patch_size"):
        validate_defaults(VideoSamplingDefaults(video_t_patch_size=0))


def test_validate_defaults_rejects_too_many_frames():
    with pytest.raises(ValueError, match="max_video_frames"):
        validate_defaults(VideoSamplingDefaults(max_video_frames=10000))


def test_validate_defaults_rejects_bad_sheet_layout():
    with pytest.raises(ValueError, match="sheet layout"):
        validate_defaults(VideoSamplingDefaults(sheet_cols=0))
    with pytest.raises(ValueError, match="sheet layout"):
        validate_defaults(VideoSamplingDefaults(sheet_max_cells=0))


def test_validate_defaults_rejects_bad_sheet_width():
    with pytest.raises(ValueError, match="sheet_width"):
        validate_defaults(VideoSamplingDefaults(sheet_width=0))


def test_validate_defaults_rejects_bad_jpeg_quality():
    with pytest.raises(ValueError, match="jpeg_quality"):
        validate_defaults(VideoSamplingDefaults(jpeg_quality=0))
    with pytest.raises(ValueError, match="jpeg_quality"):
        validate_defaults(VideoSamplingDefaults(jpeg_quality=200))


def test_validate_defaults_accepts_defaults():
    # 默认值应该通过
    validate_defaults(VideoSamplingDefaults())


# ---------------------------------------------------------------------------
# observation_signature
# ---------------------------------------------------------------------------
def test_observation_signature_changes_with_different_video(tmp_path):
    v1 = tmp_path / "a.mp4"
    v2 = tmp_path / "b.mp4"
    v1.write_bytes(b"x" * 100)
    v2.write_bytes(b"y" * 200)
    frames_dir = tmp_path / "frames"
    sig1 = observation_signature(v1, VideoSamplingDefaults(), "prompt", "", None, None, frames_dir)
    sig2 = observation_signature(v2, VideoSamplingDefaults(), "prompt", "", None, None, frames_dir)
    assert sig1 != sig2
    assert sig1["video_size"] != sig2["video_size"]


def test_observation_signature_stable_across_calls(tmp_path):
    v = tmp_path / "a.mp4"
    v.write_bytes(b"x" * 100)
    frames_dir = tmp_path / "frames"
    sig1 = observation_signature(v, VideoSamplingDefaults(), "prompt", "text", None, None, frames_dir)
    sig2 = observation_signature(v, VideoSamplingDefaults(), "prompt", "text", None, None, frames_dir)
    assert sig1 == sig2


# ---------------------------------------------------------------------------
# read_cached_observation
# ---------------------------------------------------------------------------
def test_read_cached_observation_returns_none_for_missing_file(tmp_path):
    assert read_cached_observation(tmp_path / "missing.json", {}) is None


def test_read_cached_observation_returns_none_on_bad_json(tmp_path):
    p = tmp_path / "obs.json"
    p.write_text("not json", encoding="utf-8")
    assert read_cached_observation(p, {}) is None


def test_read_cached_observation_returns_none_when_signature_mismatches(tmp_path):
    p = tmp_path / "obs.json"
    p.write_text(json.dumps({"cache_signature": {"a": 1}, "sheet_paths": []}), encoding="utf-8")
    assert read_cached_observation(p, {"a": 2}) is None


def test_read_cached_observation_returns_none_when_sheets_missing(tmp_path):
    p = tmp_path / "obs.json"
    p.write_text(json.dumps({"cache_signature": {"a": 1}, "sheet_paths": ["/nonexistent.jpg"]}), encoding="utf-8")
    assert read_cached_observation(p, {"a": 1}) is None


def test_read_cached_observation_returns_payload_when_valid(tmp_path):
    sheet = tmp_path / "sheet.jpg"
    sheet.write_bytes(b"fake-jpg")
    p = tmp_path / "obs.json"
    sig = {"a": 1}
    p.write_text(json.dumps({"cache_signature": sig, "sheet_paths": [str(sheet)]}), encoding="utf-8")
    payload = read_cached_observation(p, sig)
    assert payload is not None
    assert payload["sheet_paths"] == [str(sheet)]


# ---------------------------------------------------------------------------
# reset_frames_dir
# ---------------------------------------------------------------------------
def test_reset_frames_dir_inside_work_dir_can_delete(run_context, tmp_path):
    """在 ctx.work_dir 下的 frames_dir 允许被自动清理。"""
    fd = run_context.work_dir / "frames_test"
    fd.mkdir(parents=True)
    (fd / "stale.jpg").write_bytes(b"x")
    err = reset_frames_dir(fd, run_context)
    assert err is None
    assert fd.is_dir()
    assert not any(fd.iterdir())


def test_reset_frames_dir_outside_work_dir_with_content_refuses(run_context, tmp_path):
    """不在 work_dir 下的 frames_dir 已存在且非空 → 拒绝删除。"""
    fd = tmp_path / "outside"
    fd.mkdir()
    (fd / "stale.jpg").write_bytes(b"x")
    err = reset_frames_dir(fd, run_context)
    assert err is not None
    assert "refusing to delete" in err
    # 内容还在
    assert any(fd.iterdir())


def test_reset_frames_dir_outside_work_dir_empty_creates(run_context, tmp_path):
    fd = tmp_path / "outside2"
    # 不存在也没内容 → 应直接 mkdir
    err = reset_frames_dir(fd, run_context)
    assert err is None
    assert fd.is_dir()


# ---------------------------------------------------------------------------
# with_suffix_before_ext
# ---------------------------------------------------------------------------
def test_with_suffix_before_ext():
    from pathlib import Path
    p = Path("out/preview.mp4")
    out = with_suffix_before_ext(p, "seg00")
    assert out == Path("out/preview_seg00.mp4")


def test_with_suffix_before_ext_for_json():
    from pathlib import Path
    p = Path("out/timeline.json")
    out = with_suffix_before_ext(p, "v1")
    assert out == Path("out/timeline_v1.json")


# ---------------------------------------------------------------------------
# video_frames_dir_id
# ---------------------------------------------------------------------------
def test_video_frames_dir_id_unique_per_path(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    # 同 stem 不同目录 → ID 必须不同(避免帧目录互删)
    assert video_frames_dir_id(a) != video_frames_dir_id(b)


# ---------------------------------------------------------------------------
# default_full_prompt
# ---------------------------------------------------------------------------
def test_default_full_prompt_mentions_sheets():
    p = default_full_prompt()
    assert "sheets" in p.lower()
    assert "video" in p.lower()


# ---------------------------------------------------------------------------
# build_contact_sheets / sample_video_frames:测错误路径(空帧列表)
# ---------------------------------------------------------------------------
def test_build_contact_sheets_empty_frames_returns_empty(tmp_path):
    """空 frames 不报错,返回空 list。"""
    out_dir = tmp_path / "sheets"
    paths = build_contact_sheets([], out_dir, VideoSamplingDefaults(sheet_max_cells=4))
    assert paths == []
    assert out_dir.is_dir()  # 仍会创建空目录


def test_sample_video_frames_missing_video_raises(tmp_path):
    with pytest.raises(Exception):
        sample_video_frames(tmp_path / "missing.mp4", VideoSamplingDefaults())


def test_sample_video_frames_rejects_invalid_time_range(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    # 真实抽帧需要 ffmpeg/cv2 → 若没有应 skip;有的话应校验时间范围
    try:
        with pytest.raises(ValueError, match="start/end must be >= 0"):
            sample_video_frames(
                src, VideoSamplingDefaults(),
                source_time_range=(-1.0, 5.0),
            )
        with pytest.raises(ValueError, match="end must be greater than start"):
            sample_video_frames(
                src, VideoSamplingDefaults(),
                source_time_range=(5.0, 3.0),
            )
    except FileNotFoundError:
        # ffmpeg/cv2 不存在时,函数会先尝试读文件再校验;可能先报找不到 → skip
        import pytest as _p
        _p.skip("ffmpeg/cv2 not available")
