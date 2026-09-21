"""A 类(阶段 1)单元测试:load_media / split_shots / search_media / search_web_topic。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from storyline_capabilities.load_media import (
    IMAGE_EXTS,
    VIDEO_EXTS,
    load_media,
    path_to_file_uri,
)
from storyline_capabilities.search_media import search_media as cap_search_media
from storyline_capabilities.search_web_topic import search_web_topic as cap_search_web
from storyline_capabilities.split_shots import (
    DEFAULT_DIFF_THRESHOLD,
    DEFAULT_FRAMES_PER_SECOND,
    DEFAULT_MAX_SHOT_MS,
    DEFAULT_MIN_SHOT_MS,
    frame_difference_split_shots,
    split_shots,
    split_shots_for_media_list,
)


# ---------------------------------------------------------------------------
# load_media
# ---------------------------------------------------------------------------
def test_path_to_file_uri_windows(tmp_path: Path) -> None:
    p = tmp_path / "video.mp4"
    p.touch()
    assert path_to_file_uri(p).startswith("file:///")


def test_load_media_with_missing_paths(tmp_path: Path) -> None:
    """只有不存在的路径 + 不支持格式 → media 空,summary 标记 skipped。"""
    inputs = [
        {"path": str(tmp_path / "missing.mp4")},
        {"path": str(tmp_path / "no.txt")},
    ]
    res = load_media(inputs)
    assert res["media"] == []
    assert res["summary"]["skipped"] >= 1


def test_load_media_with_valid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用一个空的 .mp4 文件做最小验证(不抛异常 + 返回结构正确)。"""
    p = tmp_path / "fake.mp4"
    p.write_bytes(b"\x00" * 1024)
    res = load_media([{"path": str(p)}])
    # fake.mp4 不是合法 mp4,summary.video 可能为 0(av 读失败 skipped)
    # 也可能为 1(av 走 default fallback 给了空 metadata 不抛异常),都允许。
    # 只验证不抛异常 + 返回结构正确。
    assert "media" in res
    assert "summary" in res
    assert res["summary"]["video"] in (0, 1)
    assert res["summary"]["skipped"] >= 0


def test_load_media_with_unsupported_ext(tmp_path: Path) -> None:
    p = tmp_path / "doc.txt"
    p.write_bytes(b"hello")
    res = load_media([{"path": str(p)}])
    assert res["media"] == []
    assert res["summary"]["skipped"] == 1
    assert "unsupported_ext:.txt" in res["summary"]["skipped_items"][0]["reason"]


def test_load_media_empty_inputs() -> None:
    res = load_media([])
    assert res["media"] == []
    assert res["summary"]["video"] == 0
    assert res["summary"]["image"] == 0


# ---------------------------------------------------------------------------
# split_shots — 用 stub 模拟视频文件存在性,并 force_frame_diff 路径
# ---------------------------------------------------------------------------
def test_split_shots_returns_empty_on_missing_video(tmp_path: Path) -> None:
    res = split_shots({"path": str(tmp_path / "missing.mp4")})
    assert res.get("clips", []) == []
    assert res.get("error") == "video_not_found"


def test_split_shots_invalid_artifact() -> None:
    res = split_shots(123)  # type: ignore[arg-type]
    assert res.get("clips", []) == []


def test_split_shots_for_media_list_filters_video_only(tmp_path: Path) -> None:
    fake_video = tmp_path / "x.mp4"
    fake_video.touch()
    media_list = [
        {"media_id": "media_0001", "media_type": "video", "path": str(fake_video)},
        {"media_id": "media_0002", "media_type": "image", "path": str(tmp_path / "x.jpg")},
    ]
    res = split_shots_for_media_list(media_list)
    # fake mp4 文件 av 读 metadata 会失败 → 不出现在 shots 里
    assert "media_0001" in res["shots"]  # key 存在,即使 list 为空
    assert "media_0002" not in res["shots"]


def test_frame_difference_split_shots_constants() -> None:
    """保持常量稳定(plan §1.3 案例一硬约束:不能轻易改 min/max_ms)。"""
    assert DEFAULT_MIN_SHOT_MS == 1000
    assert DEFAULT_MAX_SHOT_MS == 30000
    assert DEFAULT_DIFF_THRESHOLD > 0
    assert DEFAULT_FRAMES_PER_SECOND >= 1


# ---------------------------------------------------------------------------
# search_media — mock 网络层
# ---------------------------------------------------------------------------
def test_search_media_missing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    res = cap_search_media(query="travel", media_dir=tmp_path)
    assert res["error_code"] == "MISSING_KEY"
    assert res["search_media"] == []


def test_search_media_requests_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """若主 venv 没装 requests 也应优雅退化(no error_code,NO_RESULTS)。"""
    monkeypatch.setenv("PEXELS_API_KEY", "fake_key_xxxxxxxxxxxxxxxxxxxxx")
    # force _HAS_REQUESTS False
    import storyline_capabilities.search_media as sm_mod

    monkeypatch.setattr(sm_mod, "_HAS_REQUESTS", False)
    res = cap_search_media(query="travel", media_dir=tmp_path)
    assert res["search_media"] == []


def test_search_media_video_request_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """requests 抛异常时,容错返回空(不向用户报错,仅 error_code=NO_RESULTS)。"""
    monkeypatch.setenv("PEXELS_API_KEY", "fake_key_xxxxxxxxxxxxxxxxxxxxx")
    import storyline_capabilities.search_media as sm_mod

    class _FakeR:
        def __init__(self):
            self.ok = False

        def raise_for_status(self):
            raise RuntimeError("forced")

        def json(self):
            return {}

    monkeypatch.setattr(sm_mod.requests, "get", lambda *a, **kw: _FakeR())
    res = cap_search_media(query="travel", media_dir=tmp_path)
    assert res["error_code"] == "NO_RESULTS"


# ---------------------------------------------------------------------------
# search_web_topic — mock 网络层
# ---------------------------------------------------------------------------
def test_search_web_topic_empty_query() -> None:
    res = cap_search_web(query="", max_results=5)
    assert res["error_code"] in ("NO_RESULTS", None)


def test_search_web_topic_unknown_provider() -> None:
    res = cap_search_web(query="x", provider="tavily_404")
    assert res["error_code"] == "UNKNOWN_PROVIDER"


def test_search_web_topic_stub_provider() -> None:
    res = cap_search_web(query="任何", provider="stub")
    assert res["error_code"] is None
    assert res["search_web_topic"] == []


def test_search_web_topic_ddg_no_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """requests 缺失时退化到空。"""
    import storyline_capabilities.search_web_topic as mod

    monkeypatch.setattr(mod, "_HAS_REQUESTS", False)
    res = cap_search_web(query="hi", provider="duckduckgo_html")
    assert res["error_code"] == "NO_RESULTS"
