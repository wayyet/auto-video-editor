"""本地化 load_media(plan_v4 §5 阶段 1 / A 类)。

从 vendored OpenStoryline copy ``open_storyline.nodes.core_nodes.load_media``
抽出核心 Python(去 NodeState 依赖),接受纯路径输入,返回符合
``storyline.contract.SourceMedia`` 的 dict 列表。

实现策略(plan §5 阶段 1 第 4~5 行):
1. 视频用 ``av`` 取 metadata(无 torch;``av`` 已在 vendored venv 与主 venv 通用)。
2. 图片用 ``Pillow`` 取 width/height + EXIF rotation。
3. 返回 dict 同时兼容 mapper.canonical_to_draft 读取的字段
   (``file_uri`` ``duration_ms`` ``width`` ``height`` ``fps`` ``media_type``)。
4. ``LoadMediaResult`` Pydantic 模型仅作类型契约(校验返回 dict 时方便调试);
   不强制 — 节点壳子拿 dict 写 state 即可。

不在本模块范围(plan §5 阶段 1 决策):
- **TransNetV2 / torch 推理** — 仍走 vendored venv(见 ``split_shots.py``)。
- **FireRed NodeState 集成** — 本目录纯函数化,NodeState 由节点壳子处理。
"""
from __future__ import annotations

import hashlib
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Optional

# ``av`` (PyAV) 与 Pillow 都在 vendored requirements.txt,可能未装到主 venv。
# 容错 import:缺失时退化到 ``ffprobe`` 子进程 + 不读 EXIF。
try:
    import av  # type: ignore[import-untyped]  # noqa: F401
    _HAS_AV = True
except ImportError:
    _HAS_AV = False

try:
    from PIL import Image, ImageOps  # type: ignore[import-untyped]
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}


def path_to_file_uri(p: Path) -> str:
    """``E:/foo/bar.mp4`` → ``file:///E:/foo/bar.mp4``(Windows 规范)。

    与 ``storyline.mapper.file_uri_to_path`` 互逆:Windows 平台把 ``/E:/...``
    还原为 ``E:\\...``。本函数保留前导 ``file://``,Mapper 端会反向解析。
    """
    p = Path(p).resolve()
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        return f"file:///{s}"
    return f"file://{s}"


def _file_uri_default() -> str:
    return "file:///unknown"


# ---------------------------------------------------------------------------
# 单文件 metadata
# ---------------------------------------------------------------------------
def _video_metadata(path: Path) -> dict[str, Any]:
    """读视频 metadata,失败抛 ``OSError`` 或返回空 dict。

    Returns dict keys:
        ``duration_ms`` / ``width`` / ``height`` / ``fps`` /
        ``has_audio`` / ``audio_sample_rate_hz``
    """
    if not _HAS_AV:
        # 退化到 ffprobe 子进程
        return _video_metadata_via_ffprobe(path)

    container = av.open(str(path))

    # duration_sec(微秒→秒)
    try:
        duration_sec = 0.0
        if container.duration is not None:
            duration_sec = float(container.duration) / 1_000_000
        else:
            video_stream = next(
                (s for s in container.streams if s.type == "video"), None
            )
            if (
                video_stream is not None
                and video_stream.duration is not None
                and video_stream.time_base is not None
            ):
                duration_sec = float(
                    video_stream.duration * video_stream.time_base
                )
    except Exception:  # noqa: BLE001
        duration_sec = 0.0

    duration_ms = int(round(duration_sec * 1000))

    video_stream = next(
        (s for s in container.streams if s.type == "video"), None
    )
    if video_stream is None:
        container.close()
        raise ValueError(f"No video stream found: {path}")

    w = int(video_stream.codec_context.width or 0)
    h = int(video_stream.codec_context.height or 0)

    # fps
    fps = 0.0
    try:
        if video_stream.average_rate:
            fps = float(video_stream.average_rate)
        elif video_stream.base_rate:
            fps = float(video_stream.base_rate)
    except Exception:  # noqa: BLE001
        fps = 0.0

    audio_stream = next(
        (s for s in container.streams if s.type == "audio"), None
    )
    has_audio = audio_stream is not None
    sample_rate = int(audio_stream.rate) if audio_stream and audio_stream.rate else 0

    container.close()
    return {
        "duration_ms": duration_ms,
        "width": w,
        "height": h,
        "fps": fps,
        "has_audio": has_audio,
        "audio_sample_rate_hz": sample_rate,
    }


def _video_metadata_via_ffprobe(path: Path) -> dict[str, Any]:
    """无 av 时的退化路径 — 用 ffprobe 子进程抽 metadata。"""
    import json as _json
    import subprocess

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=width,height,r_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {
            "duration_ms": 0,
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "has_audio": False,
            "audio_sample_rate_hz": 0,
        }

    if result.returncode != 0:
        return {
            "duration_ms": 0,
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "has_audio": False,
            "audio_sample_rate_hz": 0,
        }

    try:
        data = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        return {
            "duration_ms": 0,
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "has_audio": False,
            "audio_sample_rate_hz": 0,
        }

    fmt = (data.get("format") or {}).get("duration")
    duration_ms = 0
    if fmt is not None:
        try:
            duration_ms = int(round(float(fmt) * 1000))
        except (TypeError, ValueError):
            duration_ms = 0

    streams = data.get("streams") or []
    w = 0
    h = 0
    fps = 0.0
    has_audio = False
    sample_rate = 0
    for s in streams:
        if (s.get("codec_type") or "").lower() == "video":
            w = int(s.get("width") or 0)
            h = int(s.get("height") or 0)
            rfr = s.get("r_frame_rate")
            if isinstance(rfr, str) and "/" in rfr:
                try:
                    fps = float(Fraction(rfr))
                except (TypeError, ValueError, ZeroDivisionError):
                    fps = 0.0
        elif (s.get("codec_type") or "").lower() == "audio":
            has_audio = True
            sample_rate = int(s.get("sample_rate") or 0)

    return {
        "duration_ms": duration_ms,
        "width": w,
        "height": h,
        "fps": fps,
        "has_audio": has_audio,
        "audio_sample_rate_hz": sample_rate,
    }


def _image_metadata(path: Path) -> dict[str, Any]:
    """读图片 width/height(Pillow + EXIF);失败时返回 0/0。"""
    if not _HAS_PIL:
        return {"width": 0, "height": 0}

    try:
        with Image.open(path) as img:
            try:
                img2 = ImageOps.exif_transpose(img)
                w, h = img2.size
            except Exception:  # noqa: BLE001
                w, h = img.size
        return {"width": int(w), "height": int(h)}
    except Exception:  # noqa: BLE001
        return {"width": 0, "height": 0}


def _sha256_file(path: Path) -> str:
    """SHA256 of file contents(可空;Used for idempotency 键)。"""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 对外主入口(纯函数)
# ---------------------------------------------------------------------------
def load_media(
    inputs: Iterable[dict[str, Any]],
    *,
    outputs_dir: Optional[Path] = None,
    include_hash: bool = False,
) -> dict[str, Any]:
    """本地化 load_media(plan_v4 §5 阶段 1)。

    ``inputs`` 接受 iter of dict(与 vendored LoadMediaInput 兼容),每个 dict
    至少含 ``path``;其他字段(``orig_path`` / ``orig_md5``)可选,见 vendored
    ``load_media.py``。

    Returns dict 形如 ``{"media": [SourceMedia 兼容 dict, ...]}``;Phase 1
    的 19 节点壳子把它写到 ``storyline_media_artifact`` JSON 路径。
    """
    media: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for media_idx, enc_media in enumerate(inputs or [], start=1):
        path_raw = enc_media.get("path") or ""
        if not path_raw:
            continue
        path = Path(path_raw)
        if not path.exists():
            skipped.append(
                {"orig_path": enc_media.get("orig_path", ""), "reason": "not_found"}
            )
            continue

        suffix = path.suffix.lower()
        if suffix in VIDEO_EXTS:
            try:
                meta = _video_metadata(path)
            except Exception as e:  # noqa: BLE001
                skipped.append(
                    {
                        "orig_path": str(path),
                        "reason": f"video_meta_failed: {e!r}",
                    }
                )
                continue
            media_type = "video"
        elif suffix in IMAGE_EXTS:
            meta = _image_metadata(path)
            media_type = "image"
        else:
            skipped.append(
                {"orig_path": str(path), "reason": f"unsupported_ext:{suffix}"}
            )
            continue

        media_id = f"media_{media_idx:04d}"
        item: dict[str, Any] = {
            "media_id": media_id,
            "path": str(path),
            "file_uri": path_to_file_uri(path),
            "media_type": media_type,
            "metadata": meta,
            "orig_path": enc_media.get("orig_path", str(path)),
            "orig_md5": enc_media.get("orig_md5"),
        }
        if include_hash:
            item["sha256"] = _sha256_file(path)
        media.append(item)

    # 与 vendored 行为一致:返回 Counter 摘要
    summary = Counter(
        (m.get("media_type") or "").strip().lower()
        for m in media
        if isinstance(m, dict)
    )
    result: dict[str, Any] = {
        "media": media,
        "summary": {
            "video": summary.get("video", 0),
            "image": summary.get("image", 0),
            "skipped": len(skipped),
            "skipped_items": skipped,
        },
    }
    return result


__all__ = [
    "VIDEO_EXTS",
    "IMAGE_EXTS",
    "path_to_file_uri",
    "load_media",
]
