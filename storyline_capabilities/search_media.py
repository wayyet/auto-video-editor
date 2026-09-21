"""本地化 search_media(plan_v4 §5 阶段 1 / A 类)。

Pexels HTTP API client,接受 query / orientation / photo_number / video_number /
min/max_video_duration,返回下载到本地 ``media_dir`` 的路径列表。

设计纪律(plan §5 阶段 1 + §6.5):
- 显式接收 ``pexels_api_key``(无 key 时返回空 + error_code,而不是抛异常)
- 重试三次指数退避,MAX_RETRIES=3 / RETRY_INITIAL_DELAY=1.0s / MAX_DELAY=10s
- 与 vendored copy ``search_media.py`` 关键字段对齐,便于阶段 6 切真实 vendored
  时对比 diff。

API Key 处理顺序(plan §6.5 决策 TTS/VLM/LLM key 名规范已在阶段 4 开工前定稿,
search_media 这里也沿用):1) 入参 → 2) ``PEXELS_API_KEY`` 环境变量 → 3) None。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import requests  # type: ignore[import-untyped]
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


# ---------------------------------------------------------------------------
# 常量(对齐 vendored)
# ---------------------------------------------------------------------------
SEARCH_RESULT_PER_PAGE = 40
MAX_PHOTO_NUMBER = 10
MAX_VIDEO_NUMBER = 10
MIN_VIDEO_DURATION = 1
MAX_VIDEO_DURATION = 30
DEFAULT_RESULT_NUMBER_PER_PAGE = 50
DEFAULT_PAGE = 1
TARGET_LONG_EDGE_PX = 1080
VALID_ORIENTATIONS = {"landscape", "portrait"}
VIDEO_QUALITY_RANK = {"sd": 0, "hd": 1, "uhd": 2}
MAX_RETRIES = 3
RETRY_INITIAL_DELAY = 1.0
RETRY_MAX_DELAY = 10.0
RETRY_BACKOFF_FACTOR = 2.0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _sleep_backoff(attempt: int) -> None:
    delay = min(
        RETRY_INITIAL_DELAY * (RETRY_BACKOFF_FACTOR ** attempt), RETRY_MAX_DELAY
    )
    time.sleep(delay)


def _pick_video_file(video_files: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not video_files:
        return None
    # 选择 quality 最高的 link
    return max(
        video_files,
        key=lambda f: VIDEO_QUALITY_RANK.get(f.get("quality") or "", -1),
    )


def _download(url: str, out_path: Path) -> bool:
    if not _HAS_REQUESTS:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, stream=True, timeout=30)
            r.raise_for_status()
            with out_path.open("wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception:  # noqa: BLE001
            if attempt + 1 < MAX_RETRIES:
                _sleep_backoff(attempt)
    return False


# ---------------------------------------------------------------------------
# Pexels API 调用
# ---------------------------------------------------------------------------
def get_photo_media_from_pexels(
    *,
    pexels_api_key: str,
    query: str,
    media_dir: Path,
    photo_number: int = MAX_PHOTO_NUMBER,
    orientation: str = "",
) -> tuple[list[str], list[str]]:
    """Pexels /v1/search 拉图片;返回 ``(preview_urls, saved_paths)``。"""
    if not pexels_api_key:
        return ([], [])
    if not _HAS_REQUESTS:
        return ([], [])

    headers = {"Authorization": pexels_api_key}
    params: dict[str, Any] = {
        "query": query,
        "per_page": min(photo_number, MAX_PHOTO_NUMBER),
    }
    if orientation in VALID_ORIENTATIONS:
        params["orientation"] = orientation

    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers=headers,
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json() or {}
    except Exception:  # noqa: BLE001
        return ([], [])

    photos = data.get("photos") or []
    preview_urls: list[str] = []
    saved_paths: list[str] = []
    for idx, photo in enumerate(photos[:photo_number], start=1):
        url = (photo.get("src") or {}).get("large") or (photo.get("src") or {}).get(
            "original"
        )
        if not url:
            continue
        out_path = media_dir / f"photo_{idx:03d}.jpg"
        preview_urls.append(photo.get("url") or url)
        if _download(url, out_path):
            saved_paths.append(str(out_path))
    return preview_urls, saved_paths


def get_video_media_from_pexels(
    *,
    pexels_api_key: str,
    query: str,
    media_dir: Path,
    video_number: int = MAX_VIDEO_NUMBER,
    orientation: str = "",
    min_video_duration: int = MIN_VIDEO_DURATION,
    max_video_duration: int = MAX_VIDEO_DURATION,
) -> tuple[list[str], list[str]]:
    """Pexels /videos/search 拉视频;duration 在 [min, max] 内才下载。"""
    if not pexels_api_key:
        return ([], [])
    if not _HAS_REQUESTS:
        return ([], [])

    headers = {"Authorization": pexels_api_key}
    params: dict[str, Any] = {
        "query": query,
        "per_page": min(video_number, MAX_VIDEO_NUMBER) * 2,
    }
    if orientation in VALID_ORIENTATIONS:
        params["orientation"] = orientation

    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers=headers,
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json() or {}
    except Exception:  # noqa: BLE001
        return ([], [])

    videos = data.get("videos") or []
    preview_urls: list[str] = []
    saved_paths: list[str] = []
    kept = 0
    for video in videos:
        if kept >= video_number:
            break
        dur = int(video.get("duration") or 0)
        if dur < min_video_duration or dur > max_video_duration:
            continue
        picked = _pick_video_file(video.get("video_files") or [])
        if picked is None:
            continue
        url = picked.get("link")
        if not url:
            continue
        out_path = media_dir / f"video_{kept + 1:03d}.mp4"
        preview_urls.append(video.get("url") or url)
        if _download(url, out_path):
            saved_paths.append(str(out_path))
            kept += 1
    return preview_urls, saved_paths


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def search_media(
    *,
    pexels_api_key: Optional[str] = None,
    query: str = "",
    media_dir: Path,
    photo_number: int = MAX_PHOTO_NUMBER,
    video_number: int = MAX_VIDEO_NUMBER,
    orientation: str = "",
    min_video_duration: int = MIN_VIDEO_DURATION,
    max_video_duration: int = MAX_VIDEO_DURATION,
) -> dict[str, Any]:
    """Pexels 搜索主入口。

    API key 解析顺序(plan §6.5):入参 > ``PEXELS_API_KEY`` 环境变量。

    Returns dict::
        {
            "search_media": [saved_paths...],
            "preview_urls": [preview_urls...],
            "error_code": None | "MISSING_KEY" | "NO_RESULTS",
        }

    调用方根据 ``error_code`` 决定是否把 video 路径喂 ``load_media``(merge)。
    """
    # key 解析
    key = (
        pexels_api_key
        or os.environ.get("PEXELS_API_KEY", "")
        or ""
    )
    key = key.strip()

    if not key:
        return {
            "search_media": [],
            "preview_urls": [],
            "error_code": "MISSING_KEY",
            "message": "Pexels API key not detected; configure PEXELS_API_KEY.",
        }

    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)

    preview: list[str] = []
    saved: list[str] = []

    if video_number > 0:
        prev, paths = get_video_media_from_pexels(
            pexels_api_key=key,
            query=query,
            media_dir=media_dir,
            video_number=video_number,
            orientation=orientation,
            min_video_duration=min_video_duration,
            max_video_duration=max_video_duration,
        )
        preview.extend(prev)
        saved.extend(paths)

    if photo_number > 0:
        prev, paths = get_photo_media_from_pexels(
            pexels_api_key=key,
            query=query,
            media_dir=media_dir,
            photo_number=photo_number,
            orientation=orientation,
        )
        preview.extend(prev)
        saved.extend(paths)

    return {
        "search_media": saved,
        "preview_urls": preview,
        "error_code": None if saved else "NO_RESULTS",
        "message": "" if saved else "Pexels returned no matches.",
    }


__all__ = [
    "MAX_PHOTO_NUMBER",
    "MAX_VIDEO_NUMBER",
    "MIN_VIDEO_DURATION",
    "MAX_VIDEO_DURATION",
    "get_photo_media_from_pexels",
    "get_video_media_from_pexels",
    "search_media",
]
