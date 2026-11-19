"""单测:search_web_topic 能力层的 mediacrawler provider + Bing 回退
(2026-11-20 审查修订版阶段3/6.1,artifact 语义=审查文档 §6.2,回退矩阵=§6.4)。

与 test_storyline_capabilities_search_web_topic.py 分文件:MC 分支的注入点
是 ``_mc_client.search_notes``(fake 返回原始记录)与 ``_bing_html_search``,
不涉及 HTTP/HTML 桩,单独成文件避免与存量 800 行用例互相干扰。
"""
from __future__ import annotations

import pytest

import storyline_capabilities.search_web_topic as cap_mod
from storyline_capabilities.search_web_topic import (
    build_request_hash,
    mediacrawler_request_provider,
    search_web_topic,
)

_MC_REV = "09ed1ec62d8"

_MC_OK_DIAG = {"primary": {"returncode": 0}}


def _mc_note(idx: int = 1) -> dict:
    # 标题/摘要携带检索词 token(平台搜索结果的真实形态):低相关结果会被
    # 相关性守卫拦截(独立用例覆盖),成功路径数据必须真实可过守卫。
    return {
        "note_id": str(idx),
        "xsec_token": "should_not_leak",
        "title": f"夏季防晒笔记{idx}",
        "desc": f"夏季防晒 描述{idx}",
        "note_url": f"https://www.xiaohongshu.com/explore/{idx}",
        "nickname": f"作者{idx}",
    }


def _mc_options(platform: str = "xhs", revision: str = _MC_REV) -> dict:
    return {
        "platform": platform,
        "project_root": "/mc",
        "save_root": "/mc/runs",
        "timeout_seconds": 600,
        "headless": False,
        "mc_revision": revision,
    }


def _patch_mc(monkeypatch, records, error_code=None, message="") -> None:
    """替换 _mc_client.search_notes:恒返回 (records, error_code, message, diag)。"""

    def fake(**kwargs):
        return list(records), error_code, message, dict(_MC_OK_DIAG)

    monkeypatch.setattr(cap_mod._mc_client, "search_notes", fake)


def _patch_bing(monkeypatch, items, error_code=None, diagnostics=None) -> None:
    monkeypatch.setattr(
        cap_mod, "_bing_html_search", lambda **kw: (items, error_code, diagnostics or {})
    )


def _mc_call(query: str = "夏季防晒", **overrides):
    options = _mc_options()
    kwargs = dict(
        query=query, max_results=10, provider="mediacrawler", provider_options=options
    )
    kwargs.update(overrides)
    return search_web_topic(**kwargs)


# ---------------------------------------------------------------------------
# MC 成功路径(§6.2 语义)
# ---------------------------------------------------------------------------
def test_mc_success_maps_items_and_semantics(monkeypatch) -> None:
    _patch_mc(monkeypatch, [_mc_note(1), _mc_note(2)])
    res = _mc_call()
    assert res["status"] == "ok"
    assert res["provider"] == "mediacrawler"
    assert res["requested_provider"] == "mediacrawler"
    assert res["engine"] == "MediaCrawler(小红书)"
    assert res["error_code"] is None
    items = res["search_web_topic"]
    assert len(items) == 2
    assert items[0] == {
        "url": "https://www.xiaohongshu.com/explore/1",
        "title": "夏季防晒笔记1",
        "snippet": "夏季防晒 描述1",
        "source": "mediacrawler",
    }
    # 原始记录的敏感/无关字段不得进入产物(审查 P1-06 白名单四字段)
    assert "nickname" not in items[0] and "xsec_token" not in items[0]
    # request_hash = 首选策略 hash(平台+revision 编入),与 helper 逐字一致
    assert res["request_hash"] == build_request_hash(
        query="夏季防晒",
        provider=mediacrawler_request_provider("xhs", _MC_REV),
        max_results=10,
    )
    assert "fallback" not in res


def test_mc_success_max_results_is_respected(monkeypatch) -> None:
    _patch_mc(monkeypatch, [_mc_note(i) for i in range(1, 6)])
    res = search_web_topic(
        query="q",
        max_results=3,
        provider="mediacrawler",
        provider_options=_mc_options(),
    )
    assert len(res["search_web_topic"]) == 3


# ---------------------------------------------------------------------------
# 回退路径(§6.4 回退矩阵)
# ---------------------------------------------------------------------------
def test_mc_failure_falls_back_to_bing(monkeypatch) -> None:
    """MC 失败 → 自动回退 bing_html:provider=实际产出者,fallback 留痕。"""
    _patch_mc(monkeypatch, [], error_code="NETWORK_ERROR", message="down")
    _patch_bing(
        monkeypatch,
        [
            {
                "url": "https://e.com/1",
                "title": "Bing 标题",
                "snippet": "s",
                "source": "bing_html",
            }
        ],
    )
    res = _mc_call()
    assert res["status"] == "ok"
    assert res["provider"] == "bing_html"
    assert res["requested_provider"] == "mediacrawler"
    assert res["engine"] == "Bing(MediaCrawler 回退)"
    assert res["fallback"]["from_provider"] == "mediacrawler"
    assert res["fallback"]["from_platform"] == "xhs"
    assert res["fallback"]["to_provider"] == "bing_html"
    assert res["fallback"]["primary_error_code"] == "NETWORK_ERROR"
    assert res["fallback"]["primary_message"] == "down"
    assert res["search_web_topic"][0]["source"] == "bing_html"
    assert "MediaCrawler(小红书)" in res["message"] and "已回退 Bing" in res["message"]
    # 回退成功仍保留首选策略 hash(QA 重试幂等,§6.2)
    assert res["request_hash"] == build_request_hash(
        query="夏季防晒",
        provider=mediacrawler_request_provider("xhs", _MC_REV),
        max_results=10,
    )


def test_mc_zero_valid_records_falls_back(monkeypatch) -> None:
    """MC 跑完但清洗后 0 条(全为非法 URL)→ NO_RESULTS 语义回退(§6.3)。"""
    _patch_mc(
        monkeypatch,
        [{"title": "无链接笔记", "desc": "d", "note_url": "javascript:void(0)"}],
    )
    _patch_bing(monkeypatch, [], error_code="NO_RESULTS")
    res = _mc_call(query="q")
    assert res["status"] == "no_results"
    assert res["provider"] == "bing_html"
    assert res["fallback"]["primary_error_code"] == "NO_RESULTS"


def test_mc_client_error_no_results_also_falls_back(monkeypatch) -> None:
    """client 层 NO_RESULTS(无结果文件/空列表)同样回退。"""
    _patch_mc(monkeypatch, [], error_code="NO_RESULTS", message="empty")
    _patch_bing(
        monkeypatch,
        [
            {
                "url": "https://e.com/2",
                "title": "t",
                "snippet": "",
                "source": "bing_html",
            }
        ],
    )
    res = _mc_call(query="q")
    assert res["status"] == "ok" and res["provider"] == "bing_html"


def test_mc_fail_bing_zero_results_is_soft_no_results(monkeypatch) -> None:
    """MC 失败 + Bing 0 条 → no_results(软态,§6.4 矩阵第 4 行)。"""
    _patch_mc(monkeypatch, [], error_code="NETWORK_ERROR", message="down")
    _patch_bing(monkeypatch, [], error_code="NO_RESULTS")
    res = _mc_call(query="q")
    assert res["status"] == "no_results"
    assert res["error_code"] == "NO_RESULTS"
    assert res["fallback"]["primary_error_code"] == "NETWORK_ERROR"


# ---------------------------------------------------------------------------
# 双失败(P1-05:两端错误都不丢)
# ---------------------------------------------------------------------------
def test_double_failure_keeps_both_errors(monkeypatch) -> None:
    _patch_mc(monkeypatch, [], error_code="NETWORK_ERROR", message="mc down")
    _patch_bing(
        monkeypatch, [], error_code="PROVIDER_BLOCKED", diagnostics={"challenge": True}
    )
    res = _mc_call(query="q")
    assert res["status"] == "failed"
    assert res["error_code"] == "PROVIDER_BLOCKED"
    assert res["fallback"]["primary_error_code"] == "NETWORK_ERROR"
    assert res["fallback"]["primary_message"] == "mc down"
    assert res["fallback_error"]["error_code"] == "PROVIDER_BLOCKED"
    assert "mc down" in res["message"]


# ---------------------------------------------------------------------------
# 缓存隔离(P2-01/P2-04)
# ---------------------------------------------------------------------------
def test_mc_platform_and_revision_hash_isolation() -> None:
    """平台/上游 revision 变化必得不同 hash。"""
    base = mediacrawler_request_provider("xhs", "rev1")
    assert base != mediacrawler_request_provider("wb", "rev1")
    assert base != mediacrawler_request_provider("xhs", "rev2")
    assert build_request_hash(query="q", provider=base, max_results=10) != (
        build_request_hash(
            query="q",
            provider=mediacrawler_request_provider("xhs", "rev2"),
            max_results=10,
        )
    )


def test_mc_request_hash_differs_from_plain_engine_key() -> None:
    """mc 动态键 hash ≠ 基础 mediacrawler 键 hash(防跨形态串缓存)。"""
    plain = build_request_hash(query="q", provider="mediacrawler", max_results=10)
    dynamic = build_request_hash(
        query="q", provider=mediacrawler_request_provider("xhs", "rev"), max_results=10
    )
    assert plain != dynamic


# ---------------------------------------------------------------------------
# 相关性守卫覆盖 mc 分支(2026-11-19 真机事故防线;并行会话审查提醒的缺项)
# ---------------------------------------------------------------------------
def test_mc_irrelevant_results_guard_falls_back(monkeypatch) -> None:
    """低相关笔记(0 条命中检索词 token)→ IRRELEVANT_RESULTS → 回退 Bing。"""
    records = [
        {
            "note_url": "https://www.xiaohongshu.com/explore/1",
            "title": "completely unrelated topic",
            "desc": "nothing to do with the query",
        },
        {
            "note_url": "https://www.xiaohongshu.com/explore/2",
            "title": "another unrelated one",
            "desc": "still nothing",
        },
    ]
    _patch_mc(monkeypatch, records)
    _patch_bing(
        monkeypatch,
        [
            {
                "url": "https://e.com/1",
                "title": "t",
                "snippet": "",
                "source": "bing_html",
            }
        ],
    )
    res = _mc_call(query="海上清真寺旅游攻略")
    assert res["status"] == "ok"
    assert res["provider"] == "bing_html"
    assert res["fallback"]["primary_error_code"] == "IRRELEVANT_RESULTS"
    assert "relevance matched" in res["fallback"]["primary_message"]


def test_mc_guard_stats_recorded_in_diagnostics(monkeypatch) -> None:
    """守卫命中统计进 diagnostics.primary,便于审计;全部命中不拦截。"""
    _patch_mc(monkeypatch, [_mc_note(1), _mc_note(2)])
    res = _mc_call()
    primary = res["diagnostics"]["primary"]
    assert primary["relevance_matched"] == 2
    assert primary["relevance_total"] == 2


# ---------------------------------------------------------------------------
# 入参校验与 _map_mc_records(§6.3)
# ---------------------------------------------------------------------------
def test_mc_missing_required_options_raises(monkeypatch) -> None:
    """缺 project_root → ValueError(编程错误,节点层 TOOL_EXECUTION_FAILED)。"""
    with pytest.raises(ValueError, match="project_root"):
        search_web_topic(
            query="q", provider="mediacrawler", provider_options={"platform": "xhs"}
        )


def test_map_mc_records_validity_rules() -> None:
    """scheme 白名单/空标题丢弃/同 URL 去重/限量/snippet 回退键。"""
    records = [
        {"note_url": "https://a.com/1", "title": "t1", "desc": "d1"},
        {"note_url": "https://a.com/1", "title": "t1-dup", "desc": "d1"},  # 重复 URL
        {"note_url": "ftp://a.com/2", "title": "t2"},  # 非法 scheme
        {"note_url": "https://a.com/3", "title": ""},  # 空标题
        {"url": "https://a.com/4", "title": "t4", "content": "c4"},  # url/content 回退键
        {"note_url": "https://a.com/5", "title": "t5"},  # 超量
    ]
    items = cap_mod._map_mc_records(records, max_results=3)
    assert [it["url"] for it in items] == [
        "https://a.com/1",
        "https://a.com/4",
        "https://a.com/5",
    ]
    assert items[1]["snippet"] == "c4"
    assert all(it["source"] == "mediacrawler" for it in items)
