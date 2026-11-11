"""AI 转场结果缓存单测(qa 重试防重复付费,设计文档 §2.3)。

覆盖:roundtrip、缺文件判未命中、产物文件被清判未命中、损坏 JSON 容错、
缓存键输入敏感性(任一输入变化即失效)。
"""
from __future__ import annotations

import json
from pathlib import Path

from storyline_capabilities.ai_transition_media import (
    TransitionFileCache,
    build_pair_cache_key,
)


def _video(tmp_path: Path, name: str = "v.mp4") -> Path:
    p = tmp_path / name
    p.write_bytes(b"mp4-bytes")
    return p


def _entry(video: Path, **over: object) -> dict:
    d = {
        "video_path": str(video),
        "duration_ms": 4000,
        "sha256": "a" * 64,
        "status": "generated",
    }
    d.update(over)
    return d


# ---------------------------------------------------------------------------
# TransitionFileCache
# ---------------------------------------------------------------------------
def test_put_get_roundtrip(tmp_path: Path) -> None:
    cache = TransitionFileCache(tmp_path / "cache.json")
    video = _video(tmp_path)
    cache.put("k1", _entry(video))
    again = TransitionFileCache(tmp_path / "cache.json")  # 从盘上重载
    assert again.get("k1") == _entry(video)


def test_missing_file_is_miss(tmp_path: Path) -> None:
    assert TransitionFileCache(tmp_path / "absent.json").get("k") is None


def test_entry_with_deleted_video_is_miss(tmp_path: Path) -> None:
    """产物文件被清理后,缓存条目必须视为未命中(不能返回悬空路径)。"""
    video = _video(tmp_path)
    cache = TransitionFileCache(tmp_path / "cache.json")
    cache.put("k1", _entry(video))
    video.unlink()
    assert cache.get("k1") is None


def test_corrupt_cache_file_starts_empty(tmp_path: Path) -> None:
    p = tmp_path / "cache.json"
    p.write_text("{broken", encoding="utf-8")
    cache = TransitionFileCache(p)
    assert cache.get("k") is None
    cache.put("k", _entry(_video(tmp_path)))  # 写回恢复正常
    assert TransitionFileCache(p).get("k") is not None


# ---------------------------------------------------------------------------
# 缓存键
# ---------------------------------------------------------------------------
def _key(**over: object) -> str:
    kw = dict(
        from_clip_id="c1",
        to_clip_id="c2",
        from_media_sha="sha-a",
        to_media_sha="sha-b",
        from_at_ms=4000,
        to_at_ms=8000,
        prompt="Smooth continuous single shot.",
        provider="minimax",
        model="MiniMax-Hailuo-02",
        duration_ms=4000,
        resolution=(1080, 1920),
    )
    kw.update(over)
    return build_pair_cache_key(**kw)


def test_cache_key_deterministic() -> None:
    assert _key() == _key()


def test_cache_key_changes_with_any_input() -> None:
    base = _key()
    assert _key(prompt="different prompt") != base
    assert _key(from_clip_id="c9") != base
    assert _key(to_clip_id="c9") != base
    assert _key(from_media_sha="sha-x") != base
    assert _key(to_media_sha="sha-x") != base
    assert _key(from_at_ms=123) != base
    assert _key(to_at_ms=123) != base
    assert _key(provider="other") != base
    assert _key(model="other") != base
    assert _key(duration_ms=6000) != base
    assert _key(resolution=(720, 1280)) != base
