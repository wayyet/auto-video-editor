"""probe_video_dimensions / probe_display_size 单测(2026-11-11 模糊修复阶段1)。

覆盖:角度归一化矩阵、tags.rotate 与 side_data 两种旋转来源(stream_rotation
提取规则此前零测试,一并补上)、90/270 交换宽高、display 口径封装、非法输出
fail-fast。ffprobe 通过 mock ``render._ffproc.run_proc`` 注入(仿
``test_empty_storyline_fail_closed.py:116-127`` 的 CompletedProcess 模式),
不依赖本机真实 ffprobe。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import video_edit_capabilities.render as render_mod
from assembly_capabilities.media_probe import stream_rotation


# ---------------------------------------------------------------------------
# 注入工具:mock run_proc + 屏蔽真实 ffprobe 依赖
# ---------------------------------------------------------------------------
def _fake_run_proc(stdout: str, returncode: int = 0, stderr: str = ""):
    def fake(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return fake


@pytest.fixture(autouse=True)
def _fake_ffprobe_on_path(monkeypatch):
    """单测不依赖本机 ffprobe:which('ffprobe') 恒命中。"""
    monkeypatch.setattr(
        render_mod.shutil, "which",
        lambda name: "C:/fake/ffprobe.exe" if name == "ffprobe" else None,
    )


def _stream_payload(stream: dict) -> str:
    return json.dumps({"streams": [stream]})


def _base_stream(**overrides) -> dict:
    stream = {
        "width": 1920,
        "height": 1080,
        "codec_name": "hevc",
        "pix_fmt": "yuv420p10le",
        "avg_frame_rate": "25/1",
        "duration": "30.883333",
    }
    stream.update(overrides)
    return stream


# ---------------------------------------------------------------------------
# normalize_rotation_degrees:角度归一化矩阵
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0),
        ("", 0),
        (0, 0),
        ("0", 0),
        ("0.0", 0),
        (-90, 270),
        ("-90", 270),
        ("-270", 90),
        (90, 90),
        ("90", 90),
        (180, 180),
        ("-180", 180),
        (270, 270),
        ("270.0", 270),
        (360, 0),
        (720, 0),
        ("-360", 0),
        ("45", 45),
        ("89.6", 90),
        ("abc", 0),
        (object(), 0),
    ],
)
def test_normalize_rotation_degrees_matrix(raw, expected) -> None:
    assert render_mod.normalize_rotation_degrees(raw) == expected


# ---------------------------------------------------------------------------
# stream_rotation 提取规则(media_probe,此前零测试)
# ---------------------------------------------------------------------------
def test_stream_rotation_tags_take_precedence() -> None:
    stream = {"tags": {"rotate": "90"}, "side_data_list": [{"rotation": -270}]}
    assert stream_rotation(stream) == "90"


def test_stream_rotation_from_side_data() -> None:
    stream = {"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}
    assert stream_rotation(stream) == "-90"


def test_stream_rotation_skips_zero_and_missing() -> None:
    assert stream_rotation({"side_data_list": [{"rotation": 0}]}) is None
    assert stream_rotation({"side_data_list": [{"rotation": None}]}) is None
    assert stream_rotation({}) is None
    assert stream_rotation({"tags": {}}) is None


# ---------------------------------------------------------------------------
# probe_video_dimensions:无旋转 / 90 / 270 / tags 与 side_data 两来源
# ---------------------------------------------------------------------------
def test_no_rotation_display_equals_coded(monkeypatch) -> None:
    stdout = _stream_payload(_base_stream())
    monkeypatch.setattr(render_mod._ffproc, "run_proc", _fake_run_proc(stdout))
    dims = render_mod.probe_video_dimensions(Path("any.mov"))
    assert (dims.coded_width, dims.coded_height) == (1920, 1080)
    assert dims.rotation_degrees == 0
    assert (dims.display_width, dims.display_height) == (1920, 1080)
    assert dims.codec_name == "hevc"
    assert dims.pix_fmt == "yuv420p10le"
    assert dims.fps == 25.0
    assert dims.duration_ms == 30883


def test_side_data_rotation_minus90_swaps_display(monkeypatch) -> None:
    """iPhone 竖拍典型形态:coded 1920×1080 + Display Matrix -90 → 显示 1080×1920。"""
    stream = _base_stream(side_data_list=[{"side_data_type": "Display Matrix", "rotation": -90}])
    monkeypatch.setattr(
        render_mod._ffproc, "run_proc", _fake_run_proc(_stream_payload(stream))
    )
    dims = render_mod.probe_video_dimensions(Path("iphone.mov"))
    assert (dims.coded_width, dims.coded_height) == (1920, 1080)
    assert dims.rotation_degrees == 270
    assert (dims.display_width, dims.display_height) == (1080, 1920)


def test_tags_rotation_90_swaps_display(monkeypatch) -> None:
    stream = _base_stream(tags={"rotate": "90"})
    monkeypatch.setattr(
        render_mod._ffproc, "run_proc", _fake_run_proc(_stream_payload(stream))
    )
    dims = render_mod.probe_video_dimensions(Path("rot.mov"))
    assert dims.rotation_degrees == 90
    assert (dims.display_width, dims.display_height) == (1080, 1920)


def test_side_data_zero_rotation_no_swap(monkeypatch) -> None:
    stream = _base_stream(side_data_list=[{"rotation": 0}])
    monkeypatch.setattr(
        render_mod._ffproc, "run_proc", _fake_run_proc(_stream_payload(stream))
    )
    dims = render_mod.probe_video_dimensions(Path("flat.mov"))
    assert dims.rotation_degrees == 0
    assert (dims.display_width, dims.display_height) == (1920, 1080)


def test_optional_fields_missing_tolerated(monkeypatch) -> None:
    stream = {"width": 640, "height": 360}
    monkeypatch.setattr(
        render_mod._ffproc, "run_proc", _fake_run_proc(_stream_payload(stream))
    )
    dims = render_mod.probe_video_dimensions(Path("minimal.mp4"))
    assert (dims.display_width, dims.display_height) == (640, 360)
    assert dims.duration_ms is None
    assert dims.fps is None
    assert dims.codec_name is None


# ---------------------------------------------------------------------------
# fail-fast:非零返回码 / 非法 JSON / 缺尺寸 / 无 ffprobe
# ---------------------------------------------------------------------------
def test_nonzero_returncode_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        render_mod._ffproc,
        "run_proc",
        _fake_run_proc("", returncode=1, stderr="Invalid data found"),
    )
    with pytest.raises(RuntimeError, match="Invalid data found"):
        render_mod.probe_video_dimensions(Path("bad.mov"))


def test_invalid_json_raises(monkeypatch) -> None:
    monkeypatch.setattr(render_mod._ffproc, "run_proc", _fake_run_proc("not-json"))
    with pytest.raises(RuntimeError, match="could not parse ffprobe JSON"):
        render_mod.probe_video_dimensions(Path("bad.mov"))


def test_missing_or_zero_dimensions_raise(monkeypatch) -> None:
    monkeypatch.setattr(
        render_mod._ffproc, "run_proc", _fake_run_proc(_stream_payload({"width": 0, "height": 0}))
    )
    with pytest.raises(RuntimeError, match="invalid dimensions"):
        render_mod.probe_video_dimensions(Path("bad.mov"))
    monkeypatch.setattr(
        render_mod._ffproc, "run_proc", _fake_run_proc(json.dumps({"streams": []}))
    )
    with pytest.raises(RuntimeError, match="invalid dimensions"):
        render_mod.probe_video_dimensions(Path("bad.mov"))


def test_ffprobe_missing_raises(monkeypatch) -> None:
    monkeypatch.setattr(render_mod.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="ffprobe not found"):
        render_mod.probe_video_dimensions(Path("any.mov"))


# ---------------------------------------------------------------------------
# probe_display_size:显示口径封装
# ---------------------------------------------------------------------------
def test_probe_display_size_returns_display_tuple(monkeypatch) -> None:
    stream = _base_stream(side_data_list=[{"rotation": -90}])
    monkeypatch.setattr(
        render_mod._ffproc, "run_proc", _fake_run_proc(_stream_payload(stream))
    )
    assert render_mod.probe_display_size(Path("iphone.mov")) == (1080, 1920)


def test_probe_display_size_landscape_untouched(monkeypatch) -> None:
    monkeypatch.setattr(
        render_mod._ffproc,
        "run_proc",
        _fake_run_proc(_stream_payload(_base_stream())),
    )
    assert render_mod.probe_display_size(Path("landscape.mov")) == (1920, 1080)


def test_probe_video_size_semantics_frozen(monkeypatch) -> None:
    """回归护栏:probe_video_size 保持 coded 口径,不随 display 修复漂移。

    双断言:输出仍按 CSV 解析;ffprobe 命令行本身冻结(只取 width,height,
    不得混入 side_data/rotate——混合口径正是 608×1080 误裁的根源)。
    """
    captured: dict = {}

    def fake(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="1920x1080", stderr="")

    monkeypatch.setattr(render_mod._ffproc, "run_proc", fake)
    assert render_mod.probe_video_size(Path("iphone.mov")) == (1920, 1080)
    cmd = captured["cmd"]
    entries = cmd[cmd.index("-show_entries") + 1]
    assert entries == "stream=width,height"
