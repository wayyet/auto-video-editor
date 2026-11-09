"""单测:storyline_search_web_topic 节点(2026-11-10 阶段1起)。

阶段1:query 改由 ``build_web_topic_query`` 统一构造——手动 query 优先,缺失时
由 understanding.overall + groups[].theme 自动构造(前置化设计方案 §4.1)。
当前拓扑下该节点与 split_shots 同超步执行,artifacts 尚未写盘,自动路径在图上
不可达;这里直接驱动节点函数验证。阶段3拓扑前置化后路由将与节点共用同一
builder。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import nodes.storyline.node_search_web_topic as node_mod
from nodes.storyline.node_search_web_topic import storyline_search_web_topic_node


def _fake_cap_topic(captured: dict, items: list[dict] | None = None) -> Callable:
    def fake(**kwargs: Any) -> dict:
        captured.update(kwargs)
        return {
            "search_web_topic": items if items is not None else [],
            "preview_urls": [],
            "error_code": None if items else "NO_RESULTS",
            "message": "",
        }

    return fake


def _state(tmp_path: Path, **extra: Any) -> dict:
    state = {
        "storyline_outputs_root": str(tmp_path / "out"),
        "storyline_targets": {},
        "status_log": [],
    }
    state.update(extra)
    return state


def test_node_builds_query_from_artifacts_when_manual_query_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """手动 query 缺失时,由 overall 首句 + 分组主题自动构造(阶段1核心行为)。"""
    understanding = tmp_path / "understanding.json"
    understanding.write_text(
        json.dumps(
            {"overall": "夏季户外徒步，强日照。人物长时间运动。"}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    groups = tmp_path / "groups.json"
    groups.write_text(
        json.dumps({"groups": [{"theme": "户外防晒"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))

    out = storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_understanding_artifact=str(understanding),
            storyline_groups_artifact=str(groups),
        )
    )

    assert captured["query"] == "夏季户外徒步，强日照。、户外防晒"
    assert captured["max_results"] == 10
    assert captured["provider"] == "duckduckgo_html"
    # 桩返回空结果(error_code=NO_RESULTS)→ 阶段2 起映射为 no_results 标签
    assert out["status_log"] == ["storyline_search_web_topic_no_results"]
    assert Path(out["storyline_web_topic_artifact"]).exists()


def test_node_manual_query_wins_and_stays_verbatim(
    tmp_path: Path, monkeypatch
) -> None:
    """手动 query 存在且无产物时原样透传(与旧行为逐字节一致)。"""
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    assert captured["query"] == "夏季防晒"
    # 桩返回空结果(error_code=NO_RESULTS)→ 阶段2 起映射为 no_results 标签
    assert out["status_log"] == ["storyline_search_web_topic_no_results"]


# ---------------------------------------------------------------------------
# 阶段2:能力层 error_code → 固定状态标签(设计方案 §4.5)
# ---------------------------------------------------------------------------
def test_node_tags_no_results_without_error_log(
    tmp_path: Path, monkeypatch
) -> None:
    """无结果是软态:固定标签 no_results,不进 error_log。"""
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))  # NO_RESULTS

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    assert out["status_log"] == ["storyline_search_web_topic_no_results"]
    assert "error_log" not in out
    assert Path(out["storyline_web_topic_artifact"]).exists()


def test_node_tags_capability_failure_and_logs_error(
    tmp_path: Path, monkeypatch
) -> None:
    """能力层失败(NETWORK_ERROR 等):固定标签 failed + append_error 留痕。"""

    def fake_network_error(**kwargs: Any) -> dict:
        return {
            "search_web_topic": [],
            "preview_urls": [],
            "error_code": "NETWORK_ERROR",
            "message": "DuckDuckGo request failed.",
        }

    monkeypatch.setattr(node_mod, "cap_topic", fake_network_error)

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    assert out["status_log"] == ["storyline_search_web_topic_failed"]
    assert any(
        "[storyline:search_web_topic] NETWORK_ERROR" in e for e in out["error_log"]
    )


def test_node_fake_without_new_fields_defaults_to_done(
    tmp_path: Path, monkeypatch
) -> None:
    """旧桩(返回缺 status/query_hash/searched_at)走 .get 默认 → done,兼容。"""

    def fake_legacy_shape(**kwargs: Any) -> dict:
        return {
            "search_web_topic": [
                {"url": "https://e.com/1", "title": "t", "source": "fake"}
            ],
            "preview_urls": [],
            "error_code": None,
            "message": "",
        }

    monkeypatch.setattr(node_mod, "cap_topic", fake_legacy_shape)

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    assert out["status_log"] == ["storyline_search_web_topic_done"]


# ---------------------------------------------------------------------------
# 前置化阶段3:QA 重试幂等 cache_hit(设计方案 §4.6 —— 检索节点移入 qa 重试
# 回路后,同一 query_hash 的产物必须复用,不重复请求互联网)
# ---------------------------------------------------------------------------
import hashlib


def _ok_result(query: str) -> dict:
    return {
        "status": "ok",
        "search_web_topic": [
            {"url": "https://e.com/1", "title": "t1", "snippet": "s1", "source": "fake"},
        ],
        "preview_urls": [],
        "error_code": None,
        "message": "",
        "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "searched_at": "2026-11-10T00:00:00+00:00",
    }


def test_cache_hit_same_query_hash_skips_network(tmp_path: Path, monkeypatch) -> None:
    """同 query_hash 且结构有效 → cache_hit 复用,cap_topic 不被再次调用。"""
    calls = {"n": 0}

    def _counting(**kwargs: Any) -> dict:
        calls["n"] += 1
        return _ok_result(kwargs["query"])

    monkeypatch.setattr(node_mod, "cap_topic", _counting)
    state = _state(tmp_path, storyline_targets={"query": "夏季防晒"})

    out1 = storyline_search_web_topic_node(state)
    assert calls["n"] == 1

    # QA 重试语义:state 带上第一轮的产物路径 → cache_hit,不再请求互联网
    state2 = {
        **state,
        "storyline_web_topic_artifact": out1["storyline_web_topic_artifact"],
    }
    out2 = storyline_search_web_topic_node(state2)
    assert calls["n"] == 1
    doc = json.loads(
        Path(out2["storyline_web_topic_artifact"]).read_text(encoding="utf-8")
    )
    assert doc["status"] == "cache_hit"
    assert doc["query_hash"] == hashlib.sha256("夏季防晒".encode("utf-8")).hexdigest()
    assert doc["search_web_topic"]  # 检索结果原样保留
    assert out2["status_log"] == ["storyline_search_web_topic_done"]


def test_cache_miss_when_query_changes_researches_and_overwrites(
    tmp_path: Path, monkeypatch
) -> None:
    """QA 修复改了分组/概述 → query_hash 变化 → 重新检索并覆盖本线程产物。"""
    calls = {"n": 0}

    def _counting(**kwargs: Any) -> dict:
        calls["n"] += 1
        return _ok_result(kwargs["query"])

    monkeypatch.setattr(node_mod, "cap_topic", _counting)
    state = _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    out1 = storyline_search_web_topic_node(state)
    assert calls["n"] == 1

    state2 = {
        **state,
        "storyline_targets": {"query": "冬季滑雪"},  # 检索词变了
        "storyline_web_topic_artifact": out1["storyline_web_topic_artifact"],
    }
    out2 = storyline_search_web_topic_node(state2)
    assert calls["n"] == 2
    doc = json.loads(
        Path(out2["storyline_web_topic_artifact"]).read_text(encoding="utf-8")
    )
    assert doc["status"] == "ok"  # 新检索结果,不是 cache_hit
    assert doc["query_hash"] == hashlib.sha256("冬季滑雪".encode("utf-8")).hexdigest()


def test_cache_hit_of_failed_artifact_reuses_without_network(
    tmp_path: Path, monkeypatch
) -> None:
    """failed 产物同样复用(标签如实落 failed;重试网络请开新任务,见节点 docstring)。"""

    def _no_network(**_kw: Any) -> dict:  # pragma: no cover - 不应被调用
        raise AssertionError("cache_hit 路径不应请求互联网")

    monkeypatch.setattr(node_mod, "cap_topic", _no_network)
    cached = _ok_result("夏季防晒")
    cached.update({"status": "failed", "error_code": "NETWORK_ERROR", "message": "down"})
    p = tmp_path / "out" / "storyline" / "search_web_topic.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")

    out = storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_targets={"query": "夏季防晒"},
            storyline_web_topic_artifact=str(p),
        )
    )
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["status"] == "cache_hit"
    assert doc["error_code"] == "NETWORK_ERROR"  # 失败原因原样保留,不假成功
    assert out["status_log"] == ["storyline_search_web_topic_failed"]


def test_cache_hit_of_no_results_keeps_soft_tag(tmp_path: Path, monkeypatch) -> None:
    """cache 命中无结果产物:标签仍归 no_results(软态可对账),不打网络。"""

    def _no_network(**_kw: Any) -> dict:  # pragma: no cover - 不应被调用
        raise AssertionError("cache_hit 路径不应请求互联网")

    monkeypatch.setattr(node_mod, "cap_topic", _no_network)
    cached = _ok_result("夏季防晒")
    cached.update({"status": "no_results", "error_code": "NO_RESULTS"})
    p = tmp_path / "out" / "storyline" / "search_web_topic.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")

    out = storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_targets={"query": "夏季防晒"},
            storyline_web_topic_artifact=str(p),
        )
    )
    assert out["status_log"] == ["storyline_search_web_topic_no_results"]
    assert "error_log" not in out


def test_cache_ignores_legacy_artifact_without_query_hash(
    tmp_path: Path, monkeypatch
) -> None:
    """旧格式产物(无 query_hash 字段)不参与缓存 → 照常重新检索。"""
    calls = {"n": 0}

    def _counting(**kwargs: Any) -> dict:
        calls["n"] += 1
        return _ok_result(kwargs["query"])

    monkeypatch.setattr(node_mod, "cap_topic", _counting)
    p = tmp_path / "out" / "storyline" / "search_web_topic.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"search_web_topic": [], "preview_urls": [], "error_code": None, "message": ""},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_targets={"query": "夏季防晒"},
            storyline_web_topic_artifact=str(p),
        )
    )
    assert calls["n"] == 1
