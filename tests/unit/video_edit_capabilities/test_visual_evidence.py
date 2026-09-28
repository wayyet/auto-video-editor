"""``video_edit_capabilities.visual_evidence`` 单测。

对照计划 §7.1 单元测试矩阵覆盖的 4 条用例:

- ``video_watch_segment``:
  - 不传 ``video_path`` → ``[ERROR]`` (因 RunContext 不再有 active-video,
    必须显式传,见决策③)
  - 合法路径 + fps=4 → 返回 image_paths 列表(并把观察段写进 ledger)
- ``video_read_frames``:
  - ``upscale=10.0`` 限制到 4.0 → ``[ERROR] upscale must be between 1 and 4``
  - ``region={"left":0.5,"top":0,"right":0.5,"bottom":1}`` 等 region 区域裁剪
    → 返回裁剪后的图像(尺寸 = (0, h-ish))

真实素材用仓库 ``inputs/30s.mp4``(5s / 3 MB),ffmpeg 必须可用。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from assembly_capabilities.run_context import RunContext

from video_edit_capabilities.visual_evidence import (
    REGION_PRESETS,
    _parse_watch_segments,
    video_read_frames,
    video_watch_segment,
)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """RunContext rooted in tmp_path so .video_agent + out live isolated."""
    monkeypatch.chdir(tmp_path)
    return RunContext()


@pytest.fixture
def sample_video() -> Path:
    """仓库真实短样本(5s,3 MB)。"""
    root = Path(__file__).resolve().parents[3]
    p = root / "inputs" / "30s.mp4"
    if not p.is_file():
        pytest.skip(f"sample video missing: {p}")
    return p


# ---------------------------------------------------------------------------
# video_watch_segment
# ---------------------------------------------------------------------------
def test_watch_segment_without_video_path_returns_error(ctx):
    """决策③:精简 RunContext 没有 active_video_path,必须显式传。"""
    res = video_watch_segment({"fps": 4.0, "start_time": 0.0, "end_time": 1.0}, ctx)
    assert res.text.startswith("[ERROR]")
    assert "implicit active-video path removed" in res.text


def test_watch_segment_missing_fps_returns_error(ctx, sample_video):
    res = video_watch_segment({"video_path": str(sample_video), "start_time": 0.0, "end_time": 1.0}, ctx)
    assert res.text.startswith("[ERROR]")
    assert "fps" in res.text


def test_watch_segment_fps_zero_returns_error(ctx, sample_video):
    res = video_watch_segment(
        {"video_path": str(sample_video), "fps": 0.0, "start_time": 0.0, "end_time": 1.0}, ctx
    )
    assert "[ERROR]" in res.text
    assert "fps" in res.text


def test_watch_segment_invalid_segment_returns_error(ctx, sample_video):
    res = video_watch_segment(
        {"video_path": str(sample_video), "fps": 4.0, "start_time": 2.0, "end_time": 1.0},
        ctx,
    )
    assert "[ERROR]" in res.text
    assert "greater than" in res.text.lower()


def test_watch_segment_video_not_found_returns_error(ctx, tmp_path):
    ghost = tmp_path / "nope.mp4"
    res = video_watch_segment(
        {"video_path": str(ghost), "fps": 4.0, "start_time": 0.0, "end_time": 1.0}, ctx
    )
    assert "[ERROR]" in res.text
    assert "not found" in res.text.lower()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_watch_segment_returns_image_list(ctx, sample_video):
    """合法路径 + fps=4 → 返回 image_paths 列表 + 写 ledger。"""
    res = video_watch_segment(
        {
            "video_path": str(sample_video),
            "fps": 4.0,
            "start_time": 0.0,
            "end_time": 1.0,
        },
        ctx,
    )
    assert not res.text.startswith("[ERROR]"), res.text
    assert res.image_paths, f"expected image paths, got {res.image_paths}"
    assert all(Path(p).is_file() for p in res.image_paths)
    assert res.data["tool"] == "video_watch_segment"


# ---------------------------------------------------------------------------
# _parse_watch_segments — exhaust input-validation paths
# ---------------------------------------------------------------------------
def test_parse_watch_segments_rejects_missing_window():
    with pytest.raises(ValueError, match="start_time/end_time"):
        _parse_watch_segments({})


def test_parse_watch_segments_rejects_too_many_windows():
    with pytest.raises(ValueError, match=r"at most 8"):
        _parse_watch_segments({"segments": [{"start": 0.0, "end": 1.0}] * 9})


def test_parse_watch_segments_rejects_total_too_long():
    # 7 段 × 30 s = 210 s;每段 30s 低于 MAX_SEGMENT_SECONDS=60,所以 total 拦截生效
    with pytest.raises(ValueError, match=r"total watch duration"):
        _parse_watch_segments(
            {"segments": [{"start": i * 30.0, "end": i * 30.0 + 30.0} for i in range(7)]}
        )


def test_parse_watch_segments_accepts_minimal_call():
    segs = _parse_watch_segments({"start_time": 0.0, "end_time": 5.0})
    assert segs == [(0.0, 5.0)]


# ---------------------------------------------------------------------------
# video_read_frames
# ---------------------------------------------------------------------------
def test_read_frames_upscale_above_max_returns_error(ctx, sample_video):
    """计划 §7.1 第 2 条:upscale 超过 4.0 应该限制到 4.0(原工具返回 [ERROR])。"""
    res = video_read_frames(
        {
            "video_path": str(sample_video),
            "timestamps": [0.0],
            "upscale": 10.0,
        },
        ctx,
    )
    assert res.text.startswith("[ERROR]")
    assert "upscale" in res.text


def test_read_frames_unknown_region_returns_error(ctx, sample_video):
    res = video_read_frames(
        {"video_path": str(sample_video), "timestamps": [0.0], "region": "nope"},
        ctx,
    )
    assert res.text.startswith("[ERROR]")
    assert "region" in res.text.lower()


def test_read_frames_missing_video_path_returns_error(ctx):
    res = video_read_frames({"timestamps": [0.0]}, ctx)
    assert res.text.startswith("[ERROR]")
    assert "video_path" in res.text


def test_read_frames_caps_max_frames(ctx, sample_video):
    """尝试请求 100 个时间戳,应该被 max_frames=48 截断。"""
    timestamps = [i * 0.05 for i in range(100)]  # 0..4.95s
    res = video_read_frames(
        {"video_path": str(sample_video), "timestamps": timestamps, "max_frames": 4},
        ctx,
    )
    assert not res.text.startswith("[ERROR]"), res.text
    assert len(res.image_paths) <= 4


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_read_frames_region_crop_changes_size(ctx, sample_video):
    """用一组小数 0.5 占位实现"区域裁剪":left=right(零宽度)→ 宽度 ~1px。"""
    res = video_read_frames(
        {
            "video_path": str(sample_video),
            "timestamps": [0.0],
            "region": {"left": 0.0, "top": 0.0, "right": 0.5, "bottom": 1.0},
        },
        ctx,
    )
    assert not res.text.startswith("[ERROR]"), res.text
    sizes = [tuple(f["size"]) for f in res.data["frames"]]
    # 裁剪后宽度约为原图的 50%,高度约为原图
    assert sizes, "no frames delivered"
    for w, h in sizes:
        assert w >= 1
        assert h >= 1


def test_read_frames_low_max_width_rejected(ctx, sample_video):
    res = video_read_frames(
        {"video_path": str(sample_video), "timestamps": [0.0], "max_width": 32},
        ctx,
    )
    assert res.text.startswith("[ERROR]")
    assert "max_width" in res.text


# ---------------------------------------------------------------------------
# module-level sanity
# ---------------------------------------------------------------------------
def test_region_presets_contain_expected_keys():
    expected = {"name_plate", "lower_third", "bottom_left", "center", "full"}
    assert expected <= set(REGION_PRESETS)
