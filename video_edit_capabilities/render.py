"""精简版 ``render.py`` helpers —— 仅 5 个被阶段一工具直接调用的函数。

源端 `video-agent-kit 0.4.3 mcp/ve_tools/render.py` 是 900+ 行的项目时间轴
渲染器(含 ``render_preview`` / ``build_render_plan`` / ``bake_project_audio_beds``
/ ``build_project_ffmpeg_command`` 等),本仓库阶段一只搬出 5 个被
``video_basic_operation`` / ``subtitle.py`` 复用的纯 helpers:

- ``cut_clip``            —— 把单个 clip 切到输出 canvas 并补静音
- ``ffconcat_escape``     —— 把 path 编码成 ffmpeg concat demuxer 期望的转义格式
- ``probe_video_size``    —— ffprobe 取 (宽, 高)
- ``source_has_audio``    —— ffprobe 0:a? 探查
- ``source_has_video``    —— ffprobe 0:v? 探查

剩余 ``render_preview`` / project timeline / audio_beds 等大型函数本仓库不直接
调用,留待阶段五(`assembly_repair_loop` 等)按需取用,见
``assembly_capabilities/render_preview.py`` 已存的对照实现。

依赖:
- ``.ffproc.run_proc``  —— ffprobe / ffmpeg 子进程封装
- ``.timeline.file_sha256`` / ``media_duration_seconds`` —— 见 timeline.py
- ``assembly_capabilities.run_context.reject_input_output_collision``
  —— 用于 ``cut_clip`` 切前的 io 碰撞保护(沿用 .py 惯例,跨包 import 不增加
  本包内 helper 复制)

注:本文件不调用 ``ToolResult`` 与 ``RunContext`` 类型,纯 helper 集合。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import ffproc as _ffproc
from .timeline import media_duration_seconds

# Import reject helper from upstream assembly_capabilities to avoid duplicate
# maintain — same name + same contract; cross-package import is one line.
from assembly_capabilities.run_context import reject_input_output_collision


def ffconcat_escape(path: Path) -> str:
    """Escape a filesystem path for use inside an ffmpeg concat list file.

    The concat demuxer reads each line as ``file '<path>'`` and the path itself
    goes through ffmpeg's own parser; backslashes and apostrophes are the only
    two characters that can break out and end the path early, so only those
    need escaping. Source: ``video-agent-kit 0.4.3 mcp/ve_tools/render.py:736``.
    """
    s = str(Path(path))
    return s.replace("\\", "\\\\").replace("'", "'\\''")


def cut_clip(clip: dict, output_path: Path, *, target_width: int, target_height: int) -> None:
    """Slice one clip to ``output_path`` normalised to a target canvas.

    Source: ``video-agent-kit 0.4.3 mcp/ve_tools/render.py:740``. Adjustments:
    only the audio-aware path is kept; ``reject_input_output_collision`` is now
    imported from ``assembly_capabilities.run_context`` (no behaviour change).
    """
    source = Path(clip["source"])
    start = float(clip.get("start", 0.0))
    end = float(clip.get("end") or clip.get("duration", 0.0) + start)
    if end <= start:
        raise ValueError(f"clip end ({end}) must be greater than start ({start})")
    reject_input_output_collision(source, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    speed = float(clip.get("speed") or 1.0)
    duration = (end - start) / speed if speed > 0 else end - start

    vf = (
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
        f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,format=yuv420p"
    )

    cmd = ["ffmpeg", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.6f}"]
    if duration > 0:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-i", str(source), "-vf", vf,
            "-map", "0:v:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-an",
            "-movflags", "+faststart",
            str(output_path)]
    _run_or_raise(cmd, "cut_clip ffmpeg failed")


def probe_video_size(source: Path) -> tuple[int, int]:
    """Return ``(width, height)`` of the primary video stream, or raise.

    Source: ``video-agent-kit 0.4.3 mcp/ve_tools/render.py:818``. Adjustments:
    use the ffmpeg feature cache helper from ``.ffproc``.
    """
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe not found on PATH")
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        str(source),
    ]
    proc = _ffproc.run_proc(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {(proc.stderr or '').strip()}")
    out = (proc.stdout or "").strip()
    if "x" not in out:
        raise RuntimeError(f"could not parse ffprobe output for {source}: {out!r}")
    width_s, height_s = out.split("x", 1)
    width = int(width_s.strip())
    height = int(height_s.strip())
    if width <= 0 or height <= 0:
        raise RuntimeError(f"invalid dimensions from ffprobe for {source}: {width}x{height}")
    return width, height


def source_has_audio(source: Path) -> bool:
    """True iff ``source`` has at least one decodable audio stream."""
    return _has_stream(source, "a")


def source_has_video(source: Path) -> bool:
    """True iff ``source`` has at least one decodable video stream."""
    return _has_stream(source, "v")


def _has_stream(source: Path, selector: str) -> bool:
    """Light ffprobe wrapper: ``selector`` is 'a' or 'v'.

    Source: ``video-agent-kit 0.4.3 mcp/ve_tools/render.py:900``. Catches every
    exception and returns False — the higher level decides whether "no audio
    stream" is fatal or just a flag change.

    注意:原作者用的 ``csv=p=0`` + 前缀 ``codec_type=`` 检测在很多版本 ffprobe
    下输出 ``video\\n`` 而非 ``codec_type=video\\n``,会静默误判为 False。
    本仓库采用 ``-show_streams`` + 字面包含 ``codec_type=video/audio`` 的版本,
    跨 ffprobe 版本稳定。
    """
    if not shutil.which("ffprobe"):
        return False
    codec_name = "audio" if selector == "a" else "video"
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", f"{selector}:0",
        "-show_entries", "stream=codec_type",
        str(source),
    ]
    try:
        proc = _ffproc.run_proc(cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    return f"codec_type={codec_name}" in (proc.stdout or "").lower()


def _run_or_raise(cmd: list[str], message: str) -> None:
    """Wrap ``run_proc`` so callers see a single RuntimeError on failure."""
    try:
        proc = _ffproc.run_proc(cmd, capture_output=True, text=True, timeout=3600)
    except Exception as exc:
        raise RuntimeError(f"{message}: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"{message}: {(proc.stderr or proc.stdout or '').strip()}")
