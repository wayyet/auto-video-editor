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
from jy_common.translate_client import TranslationError
from storyline_capabilities.search_web_topic import (
    build_request_hash,
    mediacrawler_request_provider,
    xpoz_request_provider,
)

# 审查修订版 P2-01/P2-03:节点从 config 读引擎/代理/调试开关,单测统一显式
# 固定这三项(不依赖 config 模块默认值,防环境变量泄漏影响断言)。
_TEST_ENGINE = "google_html"
_TEST_PROXY = "socks5h://127.0.0.1:1088"

# 2026-11-20 审查修订版阶段2:双线节点的英文线标签(单测环境下 API key 未配置
# → 英文线恒 skipped;所有 zh 断言都必须带上这枚尾随标签)。
_EN_SKIPPED = "storyline_search_web_topic_en_skipped"
_EN_DONE = "storyline_search_web_topic_en_done"
_EN_NO_RESULTS = "storyline_search_web_topic_en_no_results"
_EN_FAILED = "storyline_search_web_topic_en_failed"


def _patch_en_config(
    monkeypatch,
    *,
    enabled: bool = True,
    api_key: str = "",
    platforms: tuple[str, ...] = ("tiktok", "twitter", "reddit", "instagram"),
    max_per_platform: int = 5,
    timeout: int = 300,
) -> None:
    """英文线 config 显式钉桩(不依赖 config 模块默认值,防环境变量泄漏)。"""
    monkeypatch.setattr(node_mod.config, "STORYLINE_ENABLE_WEB_TOPIC_EN", enabled)
    monkeypatch.setattr(node_mod.config, "STORYLINE_WEB_TOPIC_XPOZ_API_KEY", api_key)
    monkeypatch.setattr(
        node_mod.config, "STORYLINE_WEB_TOPIC_XPOZ_PLATFORMS", platforms
    )
    monkeypatch.setattr(
        node_mod.config,
        "STORYLINE_WEB_TOPIC_XPOZ_MAX_PER_PLATFORM",
        max_per_platform,
    )
    monkeypatch.setattr(node_mod.config, "STORYLINE_WEB_TOPIC_XPOZ_TIMEOUT", timeout)


def _patch_engine_config(
    monkeypatch, engine: str = _TEST_ENGINE, debug: bool = False
) -> None:
    monkeypatch.setattr(node_mod.config, "STORYLINE_WEB_TOPIC_ENGINE", engine)
    monkeypatch.setattr(
        node_mod.config, "STORYLINE_WEB_TOPIC_SOCKS5_PROXY", _TEST_PROXY
    )
    monkeypatch.setattr(node_mod.config, "STORYLINE_WEB_TOPIC_DEBUG", debug)
    _patch_en_config(monkeypatch)  # 英文线恒 skipped(key 为空),zh 断言确定性


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
    _patch_engine_config(monkeypatch)

    out = storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_understanding_artifact=str(understanding),
            storyline_groups_artifact=str(groups),
        )
    )

    assert captured["query"] == "夏季户外徒步，强日照。、户外防晒"
    assert captured["max_results"] == 10
    # 审查修订版 §8.2-1/2/3:provider/proxy/debug 均来自配置(不再写死 DDG)
    assert captured["provider"] == "google_html"
    assert captured["proxy_url"] == _TEST_PROXY
    assert captured["debug_dir"] is None  # debug 默认关 → 不传诊断目录
    # 桩返回空结果(error_code=NO_RESULTS)→ 阶段2 起映射为 no_results 标签
    assert out["status_log"] == ["storyline_search_web_topic_no_results", _EN_SKIPPED]
    assert Path(out["storyline_web_topic_artifact"]).exists()


def test_node_manual_query_wins_and_stays_verbatim(
    tmp_path: Path, monkeypatch
) -> None:
    """手动 query 存在且无产物时原样透传(与旧行为逐字节一致)。"""
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))
    _patch_engine_config(monkeypatch)

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    assert captured["query"] == "夏季防晒"
    assert captured["provider"] == "google_html"
    # 桩返回空结果(error_code=NO_RESULTS)→ 阶段2 起映射为 no_results 标签
    assert out["status_log"] == ["storyline_search_web_topic_no_results", _EN_SKIPPED]


def test_node_passes_debug_dir_when_config_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    """STORYLINE_WEB_TOPIC_DEBUG 开启 → 节点把任务 storyline 目录传给能力层(§5.5)。"""
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))
    _patch_engine_config(monkeypatch, debug=True)

    storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    expected = Path(tmp_path / "out" / "storyline")
    assert captured["debug_dir"] == expected


# ---------------------------------------------------------------------------
# 第八期阶段2:【检索】/【主题】标记接入(设计文档 §6.2,修订版 §5.3 优先级)
# ---------------------------------------------------------------------------


def test_node_query_prefers_search_directive_over_theme(
    tmp_path: Path, monkeypatch
) -> None:
    """【检索】优先于【主题】;手动 query 仍在最前;概述/主题顺延后移。"""
    understanding = tmp_path / "understanding.json"
    understanding.write_text(
        json.dumps({"overall": "素材整体呈现了一段海滨建筑群游览。"}, ensure_ascii=False),
        encoding="utf-8",
    )
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))
    _patch_en_config(monkeypatch)

    out = storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_targets={
                "user_request": (
                    "【主题】马六甲海上清真寺旅行Vlog\n"
                    "【检索】海上清真寺旅游攻略\n"
                    "【标题】马六甲之旅"
                )
            },
            storyline_understanding_artifact=str(understanding),
        )
    )

    query = captured["query"]
    assert query.startswith("海上清真寺旅游攻略")  # 【检索】优先于【主题】
    assert "马六甲海上清真寺旅行Vlog" not in query  # 有【检索】时【主题】不再重复拼接
    assert "素材整体呈现了一段海滨建筑群游览" in query  # 概述首句顺延保留(句尾句号已剥离)


def test_node_query_falls_back_to_theme_when_search_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """【检索】缺省时回退【主题】;两者都无 → 旧组件链(概述首句先行)。"""
    understanding = tmp_path / "understanding.json"
    understanding.write_text(
        json.dumps({"overall": "素材整体呈现了一段海滨建筑群游览。"}, ensure_ascii=False),
        encoding="utf-8",
    )
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))
    _patch_en_config(monkeypatch)

    storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_targets={"user_request": "【主题】马六甲海上清真寺旅行Vlog"},
            storyline_understanding_artifact=str(understanding),
        )
    )
    assert captured["query"].startswith("马六甲海上清真寺旅行Vlog")

    captured2: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured2))
    storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_targets={"user_request": "自由文本,无任何标记"},
            storyline_understanding_artifact=str(understanding),
        )
    )
    # 无标记 = 严格旧行为(P1-06):自由文本不进入检索词(句尾句号已剥离)
    assert captured2["query"].startswith("素材整体呈现了一段海滨建筑群游览")
    assert "自由文本" not in captured2["query"]


def test_node_query_manual_query_still_beats_directives(
    tmp_path: Path, monkeypatch
) -> None:
    """手动 query(创建台冻结契约位)保持最前,【检索】随后。"""
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))
    _patch_en_config(monkeypatch)

    storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_targets={
                "query": "手动检索词",
                "user_request": "【检索】海上清真寺旅游攻略",
            },
        )
    )
    assert captured["query"] == "手动检索词、海上清真寺旅游攻略"


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

    assert out["status_log"] == ["storyline_search_web_topic_no_results", _EN_SKIPPED]
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

    assert out["status_log"] == ["storyline_search_web_topic_failed", _EN_SKIPPED]
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

    assert out["status_log"] == ["storyline_search_web_topic_done", _EN_SKIPPED]


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
    _patch_en_config(monkeypatch)
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
    assert out2["status_log"] == ["storyline_search_web_topic_done", _EN_SKIPPED]


def test_cache_miss_when_query_changes_researches_and_overwrites(
    tmp_path: Path, monkeypatch
) -> None:
    """QA 修复改了分组/概述 → query_hash 变化 → 重新检索并覆盖本线程产物。"""
    calls = {"n": 0}

    def _counting(**kwargs: Any) -> dict:
        calls["n"] += 1
        return _ok_result(kwargs["query"])

    monkeypatch.setattr(node_mod, "cap_topic", _counting)
    _patch_en_config(monkeypatch)
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
    _patch_engine_config(monkeypatch)
    cached = _ok_result("夏季防晒")
    cached.update({"status": "failed", "error_code": "NETWORK_ERROR", "message": "down"})
    # 手工构造产物必须带节点同款 request_hash 才算同语义缓存(§4.5)
    cached["request_hash"] = build_request_hash(
        query="夏季防晒", provider=_TEST_ENGINE, max_results=10
    )
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
    assert out["status_log"] == ["storyline_search_web_topic_failed", _EN_SKIPPED]


def test_cache_hit_of_no_results_keeps_soft_tag(tmp_path: Path, monkeypatch) -> None:
    """cache 命中无结果产物:标签仍归 no_results(软态可对账),不打网络。"""

    def _no_network(**_kw: Any) -> dict:  # pragma: no cover - 不应被调用
        raise AssertionError("cache_hit 路径不应请求互联网")

    monkeypatch.setattr(node_mod, "cap_topic", _no_network)
    _patch_engine_config(monkeypatch)
    cached = _ok_result("夏季防晒")
    cached.update({"status": "no_results", "error_code": "NO_RESULTS"})
    cached["request_hash"] = build_request_hash(
        query="夏季防晒", provider=_TEST_ENGINE, max_results=10
    )
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
    assert out["status_log"] == ["storyline_search_web_topic_no_results", _EN_SKIPPED]
    assert "error_log" not in out


def test_node_cache_miss_when_provider_changes(tmp_path: Path, monkeypatch) -> None:
    """同 query 换引擎:request_hash 不同 → 缓存必须 miss 并重新检索(§4.5/P2-02)。"""
    calls = {"n": 0}

    def _counting(**kwargs: Any) -> dict:
        calls["n"] += 1
        return _ok_result(kwargs["query"])

    monkeypatch.setattr(node_mod, "cap_topic", _counting)
    _patch_engine_config(monkeypatch, engine="duckduckgo_html")
    state = _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    out1 = storyline_search_web_topic_node(state)
    assert calls["n"] == 1

    # 切换引擎(DDG → Google):同 query 也必须重新检索
    _patch_engine_config(monkeypatch, engine="google_html")
    state2 = {
        **state,
        "storyline_web_topic_artifact": out1["storyline_web_topic_artifact"],
    }
    storyline_search_web_topic_node(state2)
    assert calls["n"] == 2


def test_node_cache_miss_for_artifact_without_request_hash(
    tmp_path: Path, monkeypatch
) -> None:
    """老产物有 query_hash 但没有 request_hash → 一律视为未命中(P2-06)。"""
    calls = {"n": 0}

    def _counting(**kwargs: Any) -> dict:
        calls["n"] += 1
        return _ok_result(kwargs["query"])

    monkeypatch.setattr(node_mod, "cap_topic", _counting)
    _patch_engine_config(monkeypatch)
    cached = _ok_result("夏季防晒")
    cached.pop("request_hash", None)  # 模拟老格式:只有 query_hash,没有 request_hash
    p = tmp_path / "out" / "storyline" / "search_web_topic.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")

    storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_targets={"query": "夏季防晒"},
            storyline_web_topic_artifact=str(p),
        )
    )
    assert calls["n"] == 1  # 未命中:重新检索


def test_node_force_refresh_bypasses_cache(tmp_path: Path, monkeypatch) -> None:
    """web_topic_force_refresh=true → 显式绕过缓存重新检索(§4.5 最后一行)。"""
    calls = {"n": 0}

    def _counting(**kwargs: Any) -> dict:
        calls["n"] += 1
        return _ok_result(kwargs["query"])

    monkeypatch.setattr(node_mod, "cap_topic", _counting)
    _patch_engine_config(monkeypatch)
    state = _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    out1 = storyline_search_web_topic_node(state)
    assert calls["n"] == 1

    state2 = {
        **state,
        "storyline_web_topic_artifact": out1["storyline_web_topic_artifact"],
        "web_topic_force_refresh": True,
    }
    storyline_search_web_topic_node(state2)
    assert calls["n"] == 2  # 缓存被绕过


def test_node_maps_blocked_and_proxy_errors_to_failed_tag(
    tmp_path: Path, monkeypatch
) -> None:
    """PROVIDER_BLOCKED / PROXY_ERROR / HTTP_ERROR → failed 标签 + error_log(P2-04)。"""

    def _stub_error(error_code: str):
        def _fake(**kwargs: Any) -> dict:
            return {
                "search_web_topic": [],
                "preview_urls": [],
                "error_code": error_code,
                "message": f"stub {error_code}",
            }

        return _fake

    _patch_engine_config(monkeypatch)
    for error_code in (
        "PROVIDER_BLOCKED",
        "PROXY_ERROR",
        "HTTP_ERROR",
        "IRRELEVANT_RESULTS",
    ):
        monkeypatch.setattr(node_mod, "cap_topic", _stub_error(error_code))
        out = storyline_search_web_topic_node(
            _state(tmp_path, storyline_targets={"query": "夏季防晒"})
        )
        assert out["status_log"] == ["storyline_search_web_topic_failed", _EN_SKIPPED]
        assert any(
            f"[storyline:search_web_topic] {error_code}" in e
            for e in out["error_log"]
        )


def test_cache_ignores_legacy_artifact_without_query_hash(
    tmp_path: Path, monkeypatch
) -> None:
    """旧格式产物(无 query_hash 字段)不参与缓存 → 照常重新检索。"""
    calls = {"n": 0}

    def _counting(**kwargs: Any) -> dict:
        calls["n"] += 1
        return _ok_result(kwargs["query"])

    monkeypatch.setattr(node_mod, "cap_topic", _counting)
    _patch_en_config(monkeypatch)
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


# ---------------------------------------------------------------------------
# 2026-11-19 真机事故修订:检索词清洗(指令括号段 / 句尾句号)
# ---------------------------------------------------------------------------
def test_node_query_strips_instruction_parentheticals(
    tmp_path: Path, monkeypatch
) -> None:
    """【检索】里的指令括号段不进入检索词;真实检索诉求原样保留。"""
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))
    _patch_en_config(monkeypatch)

    storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_targets={
                "user_request": "【检索】海上清真寺旅游攻略，开放时间(勿写平台名)"
            },
        )
    )

    query = captured["query"]
    assert "海上清真寺旅游攻略" in query
    assert "开放时间" in query
    assert "勿写平台名" not in query


def test_node_query_strips_trailing_period_from_overall_sentence(
    tmp_path: Path, monkeypatch
) -> None:
    """概述首句句尾句号不进入检索词(真机事故检索词即以「过程。」收尾)。"""
    understanding = tmp_path / "understanding.json"
    understanding.write_text(
        json.dumps(
            {"overall": "素材记录了一次阴天参访清真寺的过程。"}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))
    _patch_en_config(monkeypatch)

    storyline_search_web_topic_node(
        _state(tmp_path, storyline_understanding_artifact=str(understanding))
    )

    assert captured["query"] == "素材记录了一次阴天参访清真寺的过程"


# ---------------------------------------------------------------------------
# 2026-11-20 审查修订版阶段4:mediacrawler 引擎(参数组装/hash 协同/回退标签)
# ---------------------------------------------------------------------------
_MC_PROJECT_ROOT = "C:/fake/MediaCrawler"
_MC_REVISION = "09ed1ec62d8"


def _patch_mc_engine_config(
    monkeypatch,
    *,
    max_notes: int = 7,
    timeout: int = 600,
    headless: bool = False,
    revision: str = _MC_REVISION,
) -> None:
    _patch_engine_config(monkeypatch, engine="mediacrawler")
    monkeypatch.setattr(node_mod.config, "STORYLINE_WEB_TOPIC_MC_PLATFORM", "xhs")
    monkeypatch.setattr(
        node_mod.config, "STORYLINE_WEB_TOPIC_MC_PROJECT_ROOT", _MC_PROJECT_ROOT
    )
    monkeypatch.setattr(node_mod.config, "STORYLINE_WEB_TOPIC_MC_MAX_NOTES", max_notes)
    monkeypatch.setattr(node_mod.config, "STORYLINE_WEB_TOPIC_MC_TIMEOUT", timeout)
    monkeypatch.setattr(node_mod.config, "STORYLINE_WEB_TOPIC_MC_HEADLESS", headless)
    monkeypatch.setattr(node_mod, "detect_revision", lambda root: revision)
    _patch_en_config(monkeypatch)  # 英文线恒 skipped(key 为空),zh 断言确定性


def test_node_mc_assembles_provider_options_and_effective_hash(
    tmp_path: Path, monkeypatch
) -> None:
    """MC 引擎:provider_options 从 config 组装;hash 用平台+revision+条数。"""
    captured: dict = {}
    monkeypatch.setattr(
        node_mod, "cap_topic", _fake_cap_topic(captured, items=[{"url": "https://xhs.com/1", "title": "t", "snippet": "s", "source": "mediacrawler"}])
    )
    _patch_mc_engine_config(monkeypatch, max_notes=7, headless=True)

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    assert captured["provider"] == "mediacrawler"
    assert captured["max_results"] == 7
    opts = captured["provider_options"]
    assert opts["platform"] == "xhs"
    assert opts["project_root"] == _MC_PROJECT_ROOT
    assert opts["timeout_seconds"] == 600
    assert opts["headless"] is True
    assert opts["mc_revision"] == _MC_REVISION
    assert opts["save_root"].endswith("web_topic_mc")
    # 节点计算的 request_hash 与能力层同函数(平台+revision 编入,两侧一致才命中缓存)
    doc = json.loads(Path(out["storyline_web_topic_artifact"]).read_text(encoding="utf-8"))
    assert doc["request_hash"] == build_request_hash(
        query="夏季防晒",
        provider=mediacrawler_request_provider("xhs", _MC_REVISION),
        max_results=7,
    )
    assert out["status_log"] == ["storyline_search_web_topic_done", _EN_SKIPPED]


def test_node_mc_normalizes_commas_in_query(tmp_path: Path, monkeypatch) -> None:
    """MC 引擎:检索词先过 normalize_query(英文逗号→空格),hash 用规整后文本。"""
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))
    _patch_mc_engine_config(monkeypatch)

    storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "海上清真寺,旅游攻略"})
    )

    assert captured["query"] == "海上清真寺 旅游攻略"


def test_node_mc_query_all_commas_becomes_skip(tmp_path: Path, monkeypatch) -> None:
    """规整后为空(全逗号/空白)→ 与空检索词同款软跳过,不请求网络。"""

    def _no_network(**_kw):  # pragma: no cover - 不应被调用
        raise AssertionError("空检索词不应请求网络")

    monkeypatch.setattr(node_mod, "cap_topic", _no_network)
    _patch_mc_engine_config(monkeypatch)

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": ", ,"})
    )
    assert out["status_log"] == ["storyline_search_web_topic_skipped", _EN_SKIPPED]


def test_node_mc_fallback_success_keeps_done_plus_fallback_tag(
    tmp_path: Path, monkeypatch
) -> None:
    """MC 回退 Bing 成功=检索成功,追加 fallback 标签留痕(审查阶段4.7)。"""
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))
    _patch_mc_engine_config(monkeypatch)

    # 伪造回退产物:ok + fallback 键(真实回退语义由能力层测试锁定)
    state = _state(tmp_path, storyline_targets={"query": "夏季防晒"})

    def fake_with_fallback(**kwargs: Any) -> dict:
        captured.update(kwargs)
        result = _ok_result(kwargs["query"])
        result["fallback"] = {
            "from_provider": "mediacrawler",
            "from_platform": "xhs",
            "to_provider": "bing_html",
            "primary_error_code": "NETWORK_ERROR",
            "primary_message": "down",
        }
        return result

    monkeypatch.setattr(node_mod, "cap_topic", fake_with_fallback)
    out = storyline_search_web_topic_node(state)
    assert out["status_log"] == [
        "storyline_search_web_topic_done",
        "storyline_search_web_topic_fallback_bing",
        _EN_SKIPPED,
    ]
    assert "error_log" not in out
    assert captured["provider"] == "mediacrawler"


def test_node_mc_cache_miss_when_revision_changes(
    tmp_path: Path, monkeypatch
) -> None:
    """MC 上游 revision 变化 → request_hash 变化 → 缓存 miss 重新检索(P2-01)。"""
    calls = {"n": 0}

    def _counting(**kwargs: Any) -> dict:
        calls["n"] += 1
        return _ok_result(kwargs["query"])

    monkeypatch.setattr(node_mod, "cap_topic", _counting)
    _patch_mc_engine_config(monkeypatch, revision=_MC_REVISION)
    state = _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    out1 = storyline_search_web_topic_node(state)
    assert calls["n"] == 1

    state2 = {
        **state,
        "storyline_web_topic_artifact": out1["storyline_web_topic_artifact"],
    }
    _patch_mc_engine_config(monkeypatch, revision="bbbbbbbbbbbb")
    storyline_search_web_topic_node(state2)
    assert calls["n"] == 2  # revision 变化 → 缓存失效


def test_node_mc_cache_hit_when_everything_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    """同 query/平台/revision/条数 → 命中缓存,不再起子进程。"""
    calls = {"n": 0}

    def _counting(**kwargs: Any) -> dict:
        calls["n"] += 1
        result = _ok_result(kwargs["query"])
        # 伪造能力层 MC 成功产物:request_hash 与节点同函数
        result["request_hash"] = build_request_hash(
            query=kwargs["query"],
            provider=mediacrawler_request_provider("xhs", _MC_REVISION),
            max_results=7,
        )
        return result

    monkeypatch.setattr(node_mod, "cap_topic", _counting)
    _patch_mc_engine_config(monkeypatch, max_notes=7)
    state = _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    out1 = storyline_search_web_topic_node(state)
    assert calls["n"] == 1

    state2 = {
        **state,
        "storyline_web_topic_artifact": out1["storyline_web_topic_artifact"],
    }
    out2 = storyline_search_web_topic_node(state2)
    assert calls["n"] == 1  # cache_hit
    doc = json.loads(Path(out2["storyline_web_topic_artifact"]).read_text(encoding="utf-8"))
    assert doc["status"] == "cache_hit"


# ---------------------------------------------------------------------------
# 2026-11-20 审查修订版阶段2:英文线(xpoz)节点行为
# ---------------------------------------------------------------------------
_EN_QUERY = "summer sun protection travel"


def _patch_en_ready(monkeypatch, *, api_key: str = "test-key") -> None:
    """英文线就绪态:key 已配 + 真实(非 Mock)翻译客户端 + 固定译文。"""
    _patch_en_config(monkeypatch, api_key=api_key)
    # get_default_client 返回非 Mock 对象 → 节点 stub 守卫放行
    monkeypatch.setattr(node_mod, "get_default_client", lambda: object())
    monkeypatch.setattr(
        node_mod,
        "translate_segments",
        lambda segments: [
            {"index": 0, "start_ms": 0, "end_ms": 0, "text_en": _EN_QUERY}
        ],
    )


def _dual_fake(
    captured_zh: dict,
    captured_en: dict,
    *,
    en_result: dict | None = None,
    en_raise: Exception | None = None,
) -> Callable:
    """按 provider 分流的 cap_topic 桩:zh 走 ok 产物,en 走注入的 fake。"""

    def fake(**kwargs: Any) -> dict:
        if kwargs.get("provider") == "xpoz":
            captured_en.update(kwargs)
            if en_raise is not None:
                raise en_raise
            return en_result or {
                "search_web_topic": [],
                "preview_urls": [],
                "error_code": "NO_RESULTS",
                "message": "",
            }
        captured_zh.update(kwargs)
        return _ok_result(kwargs["query"])

    return fake


def _en_ok_result(query: str) -> dict:
    return {
        "status": "ok",
        "search_web_topic": [
            {
                "url": "https://www.tiktok.com/@creator/video/1",
                "title": "summer sun protection tips",
                "snippet": "beach travel",
                "source": "TikTok",
                "platform": "tiktok",
                "language_status": "en",
                "content_kind": "video",
                "has_video": True,
            }
        ],
        "preview_urls": ["https://www.tiktok.com/@creator/video/1"],
        "error_code": None,
        "message": "",
        "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "searched_at": "2026-11-20T00:00:00+00:00",
    }


def test_en_line_happy_path_uses_xpoz_provider_and_writes_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    """英文线就绪:query 由翻译产出、provider=xpoz、产物独立落盘 + en_done 标签。"""
    captured_zh: dict = {}
    captured_en: dict = {}
    monkeypatch.setattr(
        node_mod,
        "cap_topic",
        _dual_fake(captured_zh, captured_en, en_result=_en_ok_result(_EN_QUERY)),
    )
    _patch_engine_config(monkeypatch)  # 中文线引擎/代理钉桩(google_html)
    _patch_en_ready(monkeypatch)  # 英文线就绪态(key 覆盖为 test-key)

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    assert captured_zh["provider"] == "google_html"  # 中文线不受英文线影响
    assert captured_en["provider"] == "xpoz"
    assert captured_en["query"] == _EN_QUERY  # 中文检索词经翻译进入英文线
    assert captured_en["max_results"] == 10
    opts = captured_en["provider_options"]
    assert opts["api_key"] == "test-key"
    assert opts["platforms"] == ["tiktok", "twitter", "reddit", "instagram"]
    assert opts["max_per_platform"] == 5
    assert opts["operation_timeout"] == 300
    assert out["status_log"] == ["storyline_search_web_topic_done", _EN_DONE]
    assert out["storyline_web_topic_en_artifact"]
    doc = json.loads(
        Path(out["storyline_web_topic_en_artifact"]).read_text(encoding="utf-8")
    )
    assert doc["request_hash"] == build_request_hash(
        query=_EN_QUERY,
        provider=xpoz_request_provider(
            ("tiktok", "twitter", "reddit", "instagram"), 5
        ),
        max_results=10,
    )


def test_en_line_directive_english_wins_without_translation(
    tmp_path: Path, monkeypatch
) -> None:
    """【检索】标记值是英文 → 直接用作英文检索词,不调翻译(拍板 2)。"""
    captured_zh: dict = {}
    captured_en: dict = {}
    monkeypatch.setattr(
        node_mod,
        "cap_topic",
        _dual_fake(captured_zh, captured_en, en_result=_en_ok_result("tokyo travel")),
    )
    _patch_en_ready(monkeypatch)
    translated: list = []

    def _record_translate(segments):  # pragma: no cover - 被调用即失败
        translated.append(segments)
        raise AssertionError("标记英文直用时不应调翻译")

    monkeypatch.setattr(node_mod, "translate_segments", _record_translate)

    storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_targets={
                "query": "夏季防晒",
                "user_request": "【检索】tokyo travel guide",
            },
        )
    )

    assert captured_en["query"] == "tokyo travel guide"
    assert translated == []


def test_en_line_stub_translator_skips(tmp_path: Path, monkeypatch) -> None:
    """stub 翻译器(Mock 单例)→ 英文线软跳过,防 "[EN] " 前缀污染检索词。"""
    captured_zh: dict = {}
    captured_en: dict = {}
    monkeypatch.setattr(
        node_mod, "cap_topic", _dual_fake(captured_zh, captured_en)
    )
    _patch_en_config(monkeypatch, api_key="test-key")  # key 已配,但翻译器是 Mock

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    assert captured_en == {}  # 英文线未触达能力层
    assert out["status_log"] == ["storyline_search_web_topic_done", _EN_SKIPPED]
    assert out["storyline_web_topic_en_artifact"] is None


def test_en_line_translation_failure_marks_failed(
    tmp_path: Path, monkeypatch
) -> None:
    """翻译失败 → en_failed 标签 + error_log 留痕;中文线不受影响。"""
    captured_zh: dict = {}
    captured_en: dict = {}
    monkeypatch.setattr(
        node_mod, "cap_topic", _dual_fake(captured_zh, captured_en)
    )
    _patch_en_ready(monkeypatch)

    def _boom(segments):
        raise TranslationError("LLM backend down")

    monkeypatch.setattr(node_mod, "translate_segments", _boom)

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    assert out["status_log"] == ["storyline_search_web_topic_done", _EN_FAILED]
    assert any(
        "[storyline:search_web_topic_en] TRANSLATION_FAILED" in e
        for e in out["error_log"]
    )
    assert out["storyline_web_topic_en_artifact"] is None


def test_en_line_capability_failure_isolated(
    tmp_path: Path, monkeypatch
) -> None:
    """英文线能力层异常被局部捕获:zh 产物正常,英文线 failed + error_log。"""
    captured_zh: dict = {}
    captured_en: dict = {}
    monkeypatch.setattr(
        node_mod,
        "cap_topic",
        _dual_fake(captured_zh, captured_en, en_raise=RuntimeError("xpoz down")),
    )
    _patch_en_ready(monkeypatch)

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    assert out["status_log"] == ["storyline_search_web_topic_done", _EN_FAILED]
    assert any(
        "[storyline:search_web_topic_en] TOOL_EXECUTION_FAILED" in e
        for e in out["error_log"]
    )
    assert Path(out["storyline_web_topic_artifact"]).exists()  # zh 产物正常
    assert out["storyline_web_topic_en_artifact"] is None


def test_en_line_no_results_keeps_soft_tag(tmp_path: Path, monkeypatch) -> None:
    """英文线无结果是软态:en_no_results 标签,不进 error_log,产物照写。"""
    captured_zh: dict = {}
    captured_en: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _dual_fake(captured_zh, captured_en))
    _patch_en_ready(monkeypatch)

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    assert out["status_log"] == ["storyline_search_web_topic_done", _EN_NO_RESULTS]
    assert "error_log" not in out
    assert Path(out["storyline_web_topic_en_artifact"]).exists()


def test_en_line_skip_clears_stale_state_ref(tmp_path: Path, monkeypatch) -> None:
    """英文线 skip 时显式清 State 旧引用(审查 §4.4),防复用同 thread 旧路径。"""
    captured_zh: dict = {}
    captured_en: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _dual_fake(captured_zh, captured_en))
    _patch_en_config(monkeypatch, enabled=True, api_key="")  # key 缺失 → skip

    out = storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_targets={"query": "夏季防晒"},
            storyline_web_topic_en_artifact="/old/stale/en.json",
        )
    )

    assert out["storyline_web_topic_en_artifact"] is None
    assert out["status_log"] == ["storyline_search_web_topic_done", _EN_SKIPPED]


def test_en_line_switch_off_skips_and_clears_ref(tmp_path: Path, monkeypatch) -> None:
    """EN 开关关闭 → 英文线跳过(带 skipped 标签)并清旧引用。"""
    captured_zh: dict = {}
    captured_en: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _dual_fake(captured_zh, captured_en))
    _patch_en_config(monkeypatch, enabled=False)

    out = storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_targets={"query": "夏季防晒"},
            storyline_web_topic_en_artifact="/old/stale/en.json",
        )
    )

    assert out["status_log"] == ["storyline_search_web_topic_done", _EN_SKIPPED]
    assert out["storyline_web_topic_en_artifact"] is None


def test_en_line_cache_hit_skips_network(tmp_path: Path, monkeypatch) -> None:
    """英文线缓存与中文线同款语义:同 query_en+request_hash → cache_hit 复用。"""
    en_calls = {"n": 0}

    def _counting_en(**kwargs: Any) -> dict:
        if kwargs.get("provider") == "xpoz":
            en_calls["n"] += 1
            return _en_ok_result(kwargs["query"])
        return _ok_result(kwargs["query"])

    monkeypatch.setattr(node_mod, "cap_topic", _counting_en)
    _patch_en_ready(monkeypatch)
    state = _state(tmp_path, storyline_targets={"query": "夏季防晒"})

    out1 = storyline_search_web_topic_node(state)
    assert en_calls["n"] == 1

    state2 = {
        **state,
        "storyline_web_topic_en_artifact": out1["storyline_web_topic_en_artifact"],
    }
    out2 = storyline_search_web_topic_node(state2)
    assert en_calls["n"] == 1  # 英文线命中缓存
    doc = json.loads(
        Path(out2["storyline_web_topic_en_artifact"]).read_text(encoding="utf-8")
    )
    assert doc["status"] == "cache_hit"


def test_en_line_force_refresh_bypasses_cache(tmp_path: Path, monkeypatch) -> None:
    """web_topic_force_refresh=true → 英文线缓存同样被显式绕过。"""
    en_calls = {"n": 0}

    def _counting_en(**kwargs: Any) -> dict:
        if kwargs.get("provider") == "xpoz":
            en_calls["n"] += 1
            return _en_ok_result(kwargs["query"])
        return _ok_result(kwargs["query"])

    monkeypatch.setattr(node_mod, "cap_topic", _counting_en)
    _patch_en_ready(monkeypatch)
    state = _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    out1 = storyline_search_web_topic_node(state)
    assert en_calls["n"] == 1

    state2 = {
        **state,
        "storyline_web_topic_en_artifact": out1["storyline_web_topic_en_artifact"],
        "web_topic_force_refresh": True,
    }
    storyline_search_web_topic_node(state2)
    assert en_calls["n"] == 2  # 缓存被绕过
