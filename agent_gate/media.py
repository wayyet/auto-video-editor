"""任务附带的图片:图片原样引用,视频段用 ffmpeg 均匀抽帧。"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def is_image(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


def probe_duration(path: str, *, ffprobe: str = "ffprobe", timeout: float = 30.0) -> float:
    cp = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=timeout,
    )
    if cp.returncode != 0:
        raise RuntimeError(f"ffprobe 失败:{cp.stderr.strip()[:200]}")
    return float(cp.stdout.strip())


def extract_frames(
    video: str,
    in_sec: Optional[float],
    out_sec: Optional[float],
    out_dir: Path,
    *,
    n: int = 6,
    max_edge: int = 640,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    timeout: float = 60.0,
) -> list[str]:
    """在 [in_sec, out_sec] 内均匀取 n 帧,存成 jpg,返回成功写出的路径列表。

    out_sec 为空时取到片尾。取不出帧不抛错(返回空列表),任务仍然会发出去。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    start = float(in_sec or 0.0)
    end = float(out_sec) if out_sec is not None else probe_duration(video, ffprobe=ffprobe)
    if end <= start:
        end = start + 0.1
    paths: list[str] = []
    for i in range(n):
        t = start + (i + 0.5) * (end - start) / n
        dst = out_dir / f"f{i + 1:02d}_{t:.2f}s.jpg"
        cp = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale='min({max_edge},iw)':-2",
             "-q:v", "4", str(dst)],
            capture_output=True, text=True, timeout=timeout,
        )
        if cp.returncode == 0 and dst.exists():
            paths.append(str(dst))
    return paths
