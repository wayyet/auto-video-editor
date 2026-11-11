"""AI 转场媒体工具单测(真实 ffmpeg 小样本)。

覆盖:视频精确抽帧(exact/end 两种口径)、图片素材走 Pillow、产物 ffprobe
实测描述、非视频产物拒绝、宽高比守卫阈值。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from storyline_capabilities.ai_transition_media import (
    MAX_ASPECT_RATIO_FACTOR,
    check_aspect_compatible,
    describe_video_file,
    extract_frame,
    is_image,
)


def _make_mp4(tmp_path: Path, name: str = "src.mp4", *, seconds: float = 1.0, size: str = "64x64") -> Path:
    out = tmp_path / name
    cp = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c=blue:s={size}:d={seconds}:r=30",
            "-pix_fmt", "yuv420p", str(out),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    return out


# ---------------------------------------------------------------------------
# 抽帧
# ---------------------------------------------------------------------------
def test_extract_frame_exact_from_video(tmp_path: Path) -> None:
    src = _make_mp4(tmp_path)
    out = extract_frame(src, at_ms=0, output_path=tmp_path / "f0.png", position="exact")
    assert out.is_file() and out.suffix == ".png"
    with Image.open(out) as im:
        im.verify()


def test_extract_frame_end_position_from_video(tmp_path: Path) -> None:
    """position="end":at_ms=duration(片尾)也要能取到帧(回退一帧周期)。"""
    src = _make_mp4(tmp_path)
    out = extract_frame(src, at_ms=1000, output_path=tmp_path / "fend.png", position="end")
    assert out.is_file()
    with Image.open(out) as im:
        im.verify()


def test_extract_frame_missing_video_raises_loud(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="抽帧失败"):
        extract_frame(tmp_path / "nope.mp4", at_ms=0, output_path=tmp_path / "x.png")


def test_extract_frame_from_image_uses_pillow(tmp_path: Path) -> None:
    src = tmp_path / "pic.jpg"
    Image.new("RGB", (32, 16), color=(1, 2, 3)).save(src, format="JPEG")
    assert is_image(src)
    out = extract_frame(src, at_ms=99999, output_path=tmp_path / "pic.png")
    assert out.is_file()
    with Image.open(out) as im:
        assert im.size == (32, 16)


# ---------------------------------------------------------------------------
# 产物复检
# ---------------------------------------------------------------------------
def test_describe_video_file_measures_real_values(tmp_path: Path) -> None:
    src = _make_mp4(tmp_path, "clip.mp4", seconds=1.0, size="64x64")
    info = describe_video_file(src)
    assert 800 <= info["duration_ms"] <= 1300
    assert (info["width"], info["height"]) == (64, 64)
    assert info["fps"] > 0
    assert len(info["sha256"]) == 64


def test_describe_video_file_rejects_non_video(tmp_path: Path) -> None:
    """垃圾字节 → ffprobe 直接失败;合法的纯音频文件 → 无视频流分支。两者都必须显式拒绝。"""
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"this is not a video")
    with pytest.raises(RuntimeError, match="ffprobe 失败"):
        describe_video_file(fake)

    wav = tmp_path / "audio_only.mp4"
    cp = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:d=0.5", str(wav)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    with pytest.raises(RuntimeError, match="视频流"):
        describe_video_file(wav)


# ---------------------------------------------------------------------------
# 宽高比守卫
# ---------------------------------------------------------------------------
def test_aspect_guard_accepts_same_ratio() -> None:
    assert check_aspect_compatible((1080, 1920), (720, 1280))


def test_aspect_guard_rejects_beyond_factor() -> None:
    assert not check_aspect_compatible((100, 100), (100, 300))


def test_aspect_guard_boundary_is_1_1() -> None:
    a = (100, 100)
    b = (100, 110)  # 比值恰为 1.1
    assert MAX_ASPECT_RATIO_FACTOR == 1.1
    assert check_aspect_compatible(a, b)
    assert not check_aspect_compatible(a, (100, 111))


def test_aspect_guard_unknown_dimensions_pass() -> None:
    """宽高未知(0)放行,由产物 ffprobe 复检兜底。"""
    assert check_aspect_compatible((0, 0), (100, 300))
