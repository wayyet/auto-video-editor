"""单测:storyline_search_web_topic_en 节点(2026-11-20 第九期,审查修订版 §10.3)。

锁定:英文线独立开关与产物 / 英文检索词翻译守卫(stub/空/回显) / 独立缓存
hash / force_refresh / 每轮必写状态产物(skip 也覆盖旧文件,P0-05) /
异常收敛不穿透 / ``_last_wins`` 的 tombstone 清除语义(P0-04)。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import nodes.storyline.node_search_web_topic_en as node_mod
from nodes.storyline.node_search_web_topic_en import (
    storyline_search_web_topic_en_node,
)
from state import _last_wins
from storyline_capabilities.reach_client import reach_request_provider
from storyline_capabilities.search_web_topic import build_request_hash

_REACH_ENV = {
    "STORYLINE_ENABLE_WEB_TOPIC_EN": True,
    "STORYLINE_WEB_TOPIC_REACH_ENDPOINT": "http://127.0.0.1:9/mcp",
    "STORYLINE_WEB_TOPIC_REACH_SOURCES": ("web", "youtube"),
    "STORYLINE_WEB_TOPIC_REACH_TIMEOUT": 90,
    "STORYLINE_WEB_TOPIC_REACH_MAX_PER_SOURCE": 5,
    "STORYLINE_WEB_TOPIC_REACH_DAYS": 30,
    "STORYLINE_WEB_TOPIC_REACH_MAX_RESULTS": 10,
    "STORYLINE_WEB_TOPIC_REACH_MAX_CHARS_PER_ITEM": 500,
}


def _patch_en_config(monkeypatch, *, enabled: bool = True) -> None:
    for key, value in _REACH_ENV.items():
        monkeypatch.setattr(node_mod.config, key, enabled if key.endswith("_EN") else value)


def _fake_translate(text_en: str | None) -> Callable:
    def fake(segments):
        return [
            {
                "index": 0,
                "start_ms": 0,
                "end_ms": 0,
                "text_en": text_en if text_en is not None else "",
            }
        ]

    return fake


def _fake_cap(captured: dict, result: dict | None = None, exc: Exception | None = None) -> Callable:
    def fake(**kwargs: Any) -> dict:
        captured.update(kwargs)
        if exc is not None:
            raise exc
        return result or {}

    return fake


def _ok_cap_result(query_en: str) -> dict:
    return {
        "status": "ok",
        "schema_version": 2,
        "engine": "ReachMCP(海外检索)",
        "provider": "reach_mcp",
        "query": query_en,
        "search_web_topic": [
            {
                "url": "https://www.youtube.com/watch?v=abc",
                "title": "Portable power station review",
                "snippet": "camping power",
                "source": "youtube",
                "reach_source": "youtube",
                "date": None,
                "engagement_views": 100,
            }
        ],
        "preview_urls": ["https://www.youtube.com/watch?v=abc"],
        "error_code": None,
        "message": "",
        "query_hash": hashlib.sha256(query_en.encode("utf-8")).hexdigest(),
        "searched_at": "2026-11-20T00:00:00+00:00",
        "diagnostics": {"adapter_version": "1"},
    }


def _state(tmp_path: Path, **extra: Any) -> dict:
    state = {
        "storyline_outputs_root": str(tmp_path / "out"),
        "storyline_targets": {"query": "portable power station review"},
        "status_log": [],
    }
    state.update(extra)
    return state


_EN_QUERY = "Portable power station review"


def _read_artifact(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "out" / "storyline" / "search_web_topic_en.json").read_text(
            encoding="utf-8"
        )
    )


def _expected_request_hash() -> str:
    return build_request_hash(
        query=_EN_QUERY,
        provider=reach_request_provider(
            sources=list(_REACH_ENV["STORYLINE_WEB_TOPIC_REACH_SOURCES"]),
            max_per_source=5,
            days=30,
            max_chars_per_item=500,
        ),
        max_results=10,
    )


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------

def test_node_en_success_writes_artifact_and_state(tmp_path, monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(node_mod, "translate_segments", _fake_translate(_EN_QUERY))
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap(captured, _ok_cap_result(_EN_QUERY)))
    _patch_en_config(monkeypatch)

    out = storyline_search_web_topic_en_node(_state(tmp_path))

    assert captured["query"] == _EN_QUERY
    assert captured["provider"] == "reach_mcp"
    assert captured["provider_options"]["sources"] == ["web", "youtube"]
    doc = _read_artifact(tmp_path)
    assert doc["status"] == "ok" and len(doc["search_web_topic"]) == 1
    assert doc["request_hash"] == _expected_request_hash()
    assert doc["query"] == _EN_QUERY
    assert doc["query_translation"]["query_zh"] == "portable power station review"
    assert out["storyline_web_topic_en_artifact"] == str(
        tmp_path / "out" / "storyline" / "search_web_topic_en.json"
    )
    assert out["status_log"][-1] == "storyline_search_web_topic_en_done"
    # 中文线零改动:不写中文键/中文标签
    assert "storyline_web_topic_artifact" not in out
    assert not any("search_web_topic_skipped" in t for t in out["status_log"])


# ---------------------------------------------------------------------------
# 跳过路径(每轮必写 skipped 产物,P0-05)
# ---------------------------------------------------------------------------

def test_node_en_disabled_writes_skipped_artifact(tmp_path, monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap(captured))
    _patch_en_config(monkeypatch, enabled=False)

    out = storyline_search_web_topic_en_node(_state(tmp_path))

    assert captured == {}, "开关关闭不得发起检索"
    doc = _read_artifact(tmp_path)
    assert doc["status"] == "skipped"
    assert out["status_log"][-1] == "storyline_search_web_topic_en_skipped"


def test_node_en_stub_translation_skips(tmp_path, monkeypatch) -> None:
    """Mock 翻译器([EN] 前缀)→ skipped,不冒充真实检索(审查 §6.2)。"""
    captured: dict = {}
    monkeypatch.setattr(node_mod, "translate_segments", _fake_translate(f"[EN] 夏季防晒"))
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap(captured))
    _patch_en_config(monkeypatch)

    out = storyline_search_web_topic_en_node(_state(tmp_path))

    assert captured == {}
    doc = _read_artifact(tmp_path)
    assert doc["status"] == "skipped"
    assert "QUERY_TRANSLATION_STUB" in doc["message"]
    assert out["status_log"][-1] == "storyline_search_web_topic_en_skipped"


def test_node_en_empty_translation_skips(tmp_path, monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(node_mod, "translate_segments", _fake_translate(""))
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap(captured))
    _patch_en_config(monkeypatch)
    out = storyline_search_web_topic_en_node(_state(tmp_path))
    assert captured == {}
    assert _read_artifact(tmp_path)["status"] == "skipped"


def test_node_en_echo_translation_skips(tmp_path, monkeypatch) -> None:
    """译文与中文原文相同(回显)→ skipped。"""
    captured: dict = {}
    monkeypatch.setattr(node_mod, "translate_segments", _fake_translate("夏季防晒"))
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap(captured))
    _patch_en_config(monkeypatch)
    out = storyline_search_web_topic_en_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )
    assert captured == {}
    assert out["status_log"][-1] == "storyline_search_web_topic_en_skipped"


def test_node_en_translation_failure_is_failed_with_error_log(tmp_path, monkeypatch) -> None:
    def fake_translate(segments):
        raise RuntimeError("llm down")

    monkeypatch.setattr(node_mod, "translate_segments", fake_translate)
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap({}))
    _patch_en_config(monkeypatch)

    out = storyline_search_web_topic_en_node(_state(tmp_path))

    doc = _read_artifact(tmp_path)
    assert doc["status"] == "failed"
    assert doc["error_code"] == "QUERY_TRANSLATION_FAILED"
    assert out["status_log"][-1] == "storyline_search_web_topic_en_failed"
    assert any("QUERY_TRANSLATION_FAILED" in row for row in out["error_log"])


# ---------------------------------------------------------------------------
# 缓存(独立 hash;force_refresh 绕过)
# ---------------------------------------------------------------------------

def _seed_ok_cache(tmp_path: Path) -> Path:
    out_dir = tmp_path / "out" / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "search_web_topic_en.json"
    path.write_text(
        json.dumps(
            {
                "status": "ok",
                "query_hash": hashlib.sha256(_EN_QUERY.encode("utf-8")).hexdigest(),
                "request_hash": _expected_request_hash(),
                "search_web_topic": [{"url": "https://cached/1"}],
                "error_code": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_node_en_cache_hit(tmp_path, monkeypatch) -> None:
    path = _seed_ok_cache(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(node_mod, "translate_segments", _fake_translate(_EN_QUERY))
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap(captured, _ok_cap_result(_EN_QUERY)))
    _patch_en_config(monkeypatch)

    out = storyline_search_web_topic_en_node(
        _state(tmp_path, storyline_web_topic_en_artifact=str(path))
    )

    assert captured == {}, "缓存命中不得重新检索"
    doc = _read_artifact(tmp_path)
    assert doc["status"] == "cache_hit"
    assert doc["search_web_topic"][0]["url"] == "https://cached/1"
    assert out["status_log"][-1] == "storyline_search_web_topic_en_done"


def test_node_en_force_refresh_bypasses_cache(tmp_path, monkeypatch) -> None:
    path = _seed_ok_cache(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(node_mod, "translate_segments", _fake_translate(_EN_QUERY))
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap(captured, _ok_cap_result(_EN_QUERY)))
    _patch_en_config(monkeypatch)

    out = storyline_search_web_topic_en_node(
        _state(tmp_path, storyline_web_topic_en_artifact=str(path), web_topic_force_refresh=True)
    )

    assert captured, "force_refresh 必须绕过缓存重新检索"
    doc = _read_artifact(tmp_path)
    assert doc["status"] == "ok"


def test_node_en_param_change_misses_cache(tmp_path, monkeypatch) -> None:
    """request_hash 任一参数变化(此处换 days)不得命中旧缓存(P0-06)。"""
    path = _seed_ok_cache(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(node_mod, "translate_segments", _fake_translate(_EN_QUERY))
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap(captured, _ok_cap_result(_EN_QUERY)))
    _patch_en_config(monkeypatch)
    monkeypatch.setattr(node_mod.config, "STORYLINE_WEB_TOPIC_REACH_DAYS", 60)

    storyline_search_web_topic_en_node(
        _state(tmp_path, storyline_web_topic_en_artifact=str(path))
    )
    assert captured, "days 变化 → request_hash 变化 → 缓存必须 miss"


# ---------------------------------------------------------------------------
# 失败隔离
# ---------------------------------------------------------------------------

def test_node_en_capability_failure_writes_failed_and_error_log(tmp_path, monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(node_mod, "translate_segments", _fake_translate(_EN_QUERY))
    monkeypatch.setattr(
        node_mod,
        "cap_topic",
        _fake_cap(captured, {"status": "failed", "error_code": "NETWORK_ERROR", "message": "endpoint down", "search_web_topic": []}),
    )
    _patch_en_config(monkeypatch)

    out = storyline_search_web_topic_en_node(_state(tmp_path))

    doc = _read_artifact(tmp_path)
    assert doc["status"] == "failed" and doc["error_code"] == "NETWORK_ERROR"
    assert out["status_log"][-1] == "storyline_search_web_topic_en_failed"
    assert any("search_web_topic_en" in row for row in out["error_log"])
    assert "storyline_web_topic_artifact" not in out, "英文线失败不得触碰中文键"


def test_node_en_capability_exception_never_raises(tmp_path, monkeypatch) -> None:
    def boom(**kwargs):
        raise RuntimeError("mcp exploded")

    monkeypatch.setattr(node_mod, "translate_segments", _fake_translate(_EN_QUERY))
    monkeypatch.setattr(node_mod, "cap_topic", boom)
    _patch_en_config(monkeypatch)

    out = storyline_search_web_topic_en_node(_state(tmp_path))

    doc = _read_artifact(tmp_path)
    assert doc["status"] == "failed" and doc["error_code"] == "TOOL_EXECUTION_FAILED"
    assert any("TOOL_EXECUTION_FAILED" in row for row in out["error_log"])


def test_node_en_artifact_write_failure_returns_tombstone(tmp_path, monkeypatch) -> None:
    """产物写失败 → State 置空字符串 tombstone + ARTIFACT_WRITE_FAILED 留痕(审查 P0-04/§6.2)。"""
    monkeypatch.setattr(node_mod, "translate_segments", _fake_translate(_EN_QUERY))
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap({}, _ok_cap_result(_EN_QUERY)))
    _patch_en_config(monkeypatch)
    # 让产物路径变成目录 → write_text 抛 IsADirectoryError(OSError 子类)
    (tmp_path / "out" / "storyline" / "search_web_topic_en.json").mkdir(parents=True)

    out = storyline_search_web_topic_en_node(_state(tmp_path))

    assert out["storyline_web_topic_en_artifact"] == ""
    assert out["status_log"][-1] == "storyline_search_web_topic_en_done"
    assert any("ARTIFACT_WRITE_FAILED" in row for row in out["error_log"])


# ---------------------------------------------------------------------------
# reducer tombstone 语义(P0-04)
# ---------------------------------------------------------------------------

def test_last_wins_tombstone_semantics() -> None:
    assert _last_wins("old_path", None) == "old_path", "None 不清除旧值"
    assert _last_wins("old_path", "") == "", "空字符串 tombstone 清除旧值"
    assert _last_wins("old_path", "new_path") == "new_path"
    assert _last_wins(None, "new_path") == "new_path"
