"""``video_edit_capabilities.media_operation`` 单测。

对照计划 §7.1 单元测试矩阵覆盖的 3 条用例:

- ``video_basic_operation``:
  - ``operation="unknown"`` → ``[ERROR]``
  - ``shutil.which("ffmpeg")`` 返回 None → ``[ERROR]``
  - ``speed=0.05`` → ``[ERROR]`` (低于 SPEED_MIN = 0.1)
- 加几条:trim 跑通真视频,reject_input/output collision,filter 助手等。

真实素材用仓库 ``inputs/30s.mp4``(5s/3 MB),ffmpeg 必须可用。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest import mock

import pytest

from assembly_capabilities.run_context import RunContext

from video_edit_capabilities.media_operation import (
    VIDEO_OPERATIONS,
    build_crop_filter,
    build_flip_filter,
    build_rotate_filter,
    build_scale_filter,
    number_arg,
    op_trim,
    video_basic_operation,
)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return RunContext()


@pytest.fixture
def sample_video() -> Path:
    root = Path(__file__).resolve().parents[3]
    p = root / "inputs" / "30s.mp4"
    if not p.is_file():
        pytest.skip(f"sample video missing: {p}")
    return p


# ---------------------------------------------------------------------------
# §7.1 row 3: video_basic_operation
# ---------------------------------------------------------------------------
def test_basic_operation_unknown_returns_error(ctx):
    res = video_basic_operation({"operation": "totally_unknown", "input_path": "x.mp4"}, ctx)
    assert res.text.startswith("[ERROR]")
    assert "operation must be one of" in res.text


def test_basic_operation_speed_below_min_returns_error(ctx, sample_video):
    res = video_basic_operation(
        {"operation": "speed", "input_path": str(sample_video), "speed": 0.05},
        ctx,
    )
    assert "[ERROR]" in res.text
    assert "speed" in res.text


def test_basic_operation_speed_above_max_returns_error(ctx, sample_video):
    res = video_basic_operation(
        {"operation": "speed", "input_path": str(sample_video), "speed": 100.0},
        ctx,
    )
    assert "[ERROR]" in res.text
    assert "speed" in res.text


def test_basic_operation_no_ffmpeg_returns_error(ctx, monkeypatch, sample_video):
    """``shutil.which`` 返回 None → 应在 main dispatcher 早期就 ``[ERROR]``。"""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    res = video_basic_operation(
        {"operation": "trim", "input_path": str(sample_video), "start_time": 0.0, "end_time": 1.0},
        ctx,
    )
    assert res.text.startswith("[ERROR]")
    assert "ffmpeg" in res.text.lower()


def test_basic_operation_missing_input_returns_error(ctx):
    res = video_basic_operation(
        {"operation": "trim", "start_time": 0.0, "end_time": 1.0},
        ctx,
    )
    assert "[ERROR]" in res.text
    assert "input_path" in res.text


def test_basic_operation_input_not_found_returns_error(ctx):
    res = video_basic_operation(
        {"operation": "trim", "input_path": "nope.mp4", "start_time": 0.0, "end_time": 1.0},
        ctx,
    )
    assert "[ERROR]" in res.text
    assert "File not found" in res.text


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_basic_operation_trim_produces_video_with_report(ctx, sample_video):
    """trim 是 8 个 op 中最纯粹的 ffmpeg 调用:成功路径跑通,产物必有 video 流,
    ``*.basic_operation.json`` 报告存在且 operation 字段对。"""
    res = video_basic_operation(
        {
            "operation": "trim",
            "input_path": str(sample_video),
            "start_time": 0.0,
            "end_time": 2.0,
        },
        ctx,
    )
    assert not res.text.startswith("[ERROR]"), res.text
    out_path = Path(res.video_paths[0])
    assert out_path.is_file()
    report_path = out_path.with_suffix(".basic_operation.json")
    assert report_path.is_file()
    report = __import__("json").loads(report_path.read_text(encoding="utf-8"))
    assert report["operation"] == "trim"
    assert report["output_sha256"]
    assert report["implementation"].startswith("ffmpeg deterministic")


# ---------------------------------------------------------------------------
# filter builder tests
# ---------------------------------------------------------------------------
def test_build_crop_filter_minimal():
    f = build_crop_filter(
        {"width": 200, "height": 100, "x": 10.0, "y": 5.0}
    )
    assert f.startswith("crop=200:100:10.000000:5.000000")


def test_build_scale_filter_fit_fill_stretch():
    assert "force_original_aspect_ratio=decrease" in build_scale_filter(
        {"output_width": 100, "output_height": 50, "mode": "fit"}
    )
    assert "force_original_aspect_ratio=increase" in build_scale_filter(
        {"output_width": 100, "output_height": 50, "mode": "fill"}
    )
    assert "scale=100:50,setsar=1" in build_scale_filter(
        {"output_width": 100, "output_height": 50, "mode": "stretch"}
    )


def test_build_scale_filter_invalid_mode_raises():
    with pytest.raises(ValueError, match="fit, fill, or stretch"):
        build_scale_filter({"output_width": 100, "output_height": 50, "mode": "weird"})


def test_build_rotate_filter_special_angles():
    assert "transpose=1" in build_rotate_filter({"degrees": 90.0})
    assert "hflip,vflip" in build_rotate_filter({"degrees": 180.0})
    assert "transpose=2" in build_rotate_filter({"degrees": 270.0})
    assert "rotate=" in build_rotate_filter({"degrees": 45.0})


def test_build_flip_filter_unknown_direction_raises():
    with pytest.raises(ValueError, match="horizontal, vertical, or both"):
        build_flip_filter({"direction": "diagonal"})


# ---------------------------------------------------------------------------
# number_arg validation
# ---------------------------------------------------------------------------
def test_number_arg_min_max():
    with pytest.raises(ValueError, match="must be >="):
        number_arg({"x": -1.0}, "x", default=1.0, minimum=0.0)
    with pytest.raises(ValueError, match="must be <="):
        number_arg({"x": 11.0}, "x", default=1.0, maximum=10.0)
    with pytest.raises(ValueError, match="must be numeric"):
        number_arg({"x": "abc"}, "x", default=1.0)


# ---------------------------------------------------------------------------
# misc: VIDEO_OPERATIONS expected set
# ---------------------------------------------------------------------------
def test_video_operations_set_is_complete():
    expected = {
        "trim", "splice", "speed", "crop",
        "scale", "rotate", "flip", "freeze_frame",
    }
    assert expected == VIDEO_OPERATIONS


# ---------------------------------------------------------------------------
# op_trim: input/output collision
# ---------------------------------------------------------------------------
def test_op_trim_input_eq_output_raises(ctx, sample_video):
    """``reject_input_output_collision`` 应阻断 input 与 output 相同路径。

    input_path 与 output_path 都设为 sample_video 的解析后绝对路径,确保
    ``ctx.resolve`` 两端都拿到 *相同的* Path。
    """
    args = {
        "input_path": str(sample_video),
        "output_path": str(sample_video),
        "start_time": 0.0,
        "end_time": 1.0,
    }
    with pytest.raises(ValueError, match="refusing to write output"):
        op_trim(args, ctx)


# ---------------------------------------------------------------------------
# Using mock for deterministic ffmpeg behavior
# ---------------------------------------------------------------------------
def test_basic_operation_handles_ffmpeg_failure_gracefully(ctx, sample_video, monkeypatch):
    """ffmpeg 失败 → main dispatcher 把 ValueError / RuntimeError 转 [ERROR]。

    用 ``unittest.mock.patch`` 替换 ``run_ffmpeg``(本仓本文件内 helper),触
    发 ``RuntimeError``,以证明错误信息会落到 ``res.text`` 而不是 raise 出来。
    """
    from video_edit_capabilities import media_operation

    def boom(cmd, message):
        raise RuntimeError("ffmpeg pipe blew up")

    monkeypatch.setattr(media_operation, "run_ffmpeg", boom)
    res = video_basic_operation(
        {
            "operation": "trim",
            "input_path": str(sample_video),
            "start_time": 0.0,
            "end_time": 1.0,
        },
        ctx,
    )
    assert res.text.startswith("[ERROR]")
    assert "ffmpeg pipe blew up" in res.text
