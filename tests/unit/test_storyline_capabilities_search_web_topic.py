"""单测:search_web_topic 能力层(2026-11-10 阶段2:前置化设计方案 §4.2)。

锁定:snippet 提取与清洗 / URL scheme 白名单 / 单条+累计限长护栏 /
错误分类(NETWORK_ERROR、PARSE_ERROR 与 NO_RESULTS 分离) /
status + query_hash + searched_at 统一留痕。

mock 手法:monkeypatch 模块属性 ``requests`` 注入 FakeResponse(仓库无
requests_mock 依赖,沿用注入式 fake 惯例);``_HAS_REQUESTS`` 标志同理。
"""
from __future__ import annotations

import hashlib
import urllib.parse
from typing import Any, Union

import storyline_capabilities.search_web_topic as cap_mod
from storyline_capabilities.search_web_topic import (
    WEB_TOPIC_SNIPPET_MAX_CHARS,
    WEB_TOPIC_TOTAL_CONTEXT_MAX_CHARS,
    search_web_topic,
)


class _FakeResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")


class _FakeRequests:
    """替换能力层模块属性 ``requests``;response_or_exc 为 Exception 时直接抛出。"""

    def __init__(self, response_or_exc: Union[_FakeResponse, Exception]) -> None:
        self._resp = response_or_exc
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "params": params, "headers": headers, "timeout": timeout}
        )
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def _result_html(results: list[tuple[str, str, str]]) -> str:
    """(url, title, snippet) 三元组 → 模拟 DuckDuckGo html 结果页。"""
    blocks = []
    for url, title, snippet in results:
        blocks.append(
            f'<a class="result__a" href="{url}">{title}</a>'
            f'<a class="result__snippet" href="#">{snippet}</a>'
        )
    return "<html><body>" + "".join(blocks) + "</body></html>"


# ---------------------------------------------------------------------------
# 正常解析路径
# ---------------------------------------------------------------------------
def test_results_include_snippet_and_decoded_uddg_url(monkeypatch) -> None:
    inner = urllib.parse.quote("https://example.com/article?x=1&y=2", safe="")
    html = _result_html(
        [(f"https://duckduckgo.com/l/?uddg={inner}", "防晒 &amp; 补水", "摘要 &lt;b&gt;文本&lt;/b&gt;")]
    )
    fake = _FakeRequests(_FakeResponse(html))
    monkeypatch.setattr(cap_mod, "requests", fake)

    res = search_web_topic(query="夏季防晒")

    assert res["error_code"] is None
    assert res["status"] == "ok"
    (item,) = res["search_web_topic"]
    assert item["title"] == "防晒 & 补水"
    assert item["snippet"] == "摘要 <b>文本</b>"
    assert item["url"] == "https://example.com/article?x=1&y=2"
    assert item["source"] == "duckduckgo_html"
    # 请求参数留痕:端点 / query / 超时
    assert fake.calls[0]["params"] == {"q": "夏季防晒"}
    assert fake.calls[0]["timeout"] == 15


def test_status_query_hash_and_searched_at_are_stamped(monkeypatch) -> None:
    monkeypatch.setattr(
        cap_mod,
        "requests",
        _FakeRequests(_FakeResponse(_result_html([("https://e.com/1", "t", "s")]))),
    )
    res = search_web_topic(query="夏季防晒")
    assert res["query_hash"] == hashlib.sha256("夏季防晒".encode("utf-8")).hexdigest()
    assert res["searched_at"]  # UTC isoformat,由实现保证格式
    assert res["status"] == "ok"
    # 2026-11-10 真机验收补漏:§4.2 契约要求产物记录实际提交的检索词
    assert res["query"] == "夏季防晒"


def test_control_chars_and_newlines_in_snippet_are_cleaned(monkeypatch) -> None:
    html = _result_html([("https://e.com/1", "标题", "第一行\n第二行\x00尾巴")])
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q")
    assert res["search_web_topic"][0]["snippet"] == "第一行 第二行 尾巴"


# ---------------------------------------------------------------------------
# 护栏
# ---------------------------------------------------------------------------
def test_non_http_schemes_are_dropped(monkeypatch) -> None:
    html = _result_html(
        [
            ("javascript:alert(1)", "坏链接", "摘要"),
            ("https://good.example.com/", "好链接", "摘要二"),
        ]
    )
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q")
    assert [it["url"] for it in res["search_web_topic"]] == ["https://good.example.com/"]


def test_snippet_capped_per_item_and_in_total(monkeypatch) -> None:
    long_snip = "摘" * 1000
    html = _result_html(
        [(f"https://e.com/{i}", f"标题{i}", long_snip) for i in range(10)]
    )
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q")
    items = res["search_web_topic"]
    assert len(items) == 10
    assert len(items[0]["snippet"]) == WEB_TOPIC_SNIPPET_MAX_CHARS
    assert sum(len(it["snippet"]) for it in items) <= WEB_TOPIC_TOTAL_CONTEXT_MAX_CHARS
    # 累计预算耗尽后的条目摘要置空,标题/URL 保留
    assert items[-1]["snippet"] == ""
    assert items[-1]["title"] == "标题9"


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------
def test_no_results_page_maps_to_no_results(monkeypatch) -> None:
    html = '<html><body><div class="no-results">No results for q</div></body></html>'
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q")
    assert res["error_code"] == "NO_RESULTS"
    assert res["status"] == "no_results"
    assert res["query_hash"] == hashlib.sha256("q".encode("utf-8")).hexdigest()


def test_large_page_without_anchors_maps_to_parse_error(monkeypatch) -> None:
    """HTTP 200 但解析不到锚点且无「无结果」标记 → 供应商改版,PARSE_ERROR 可观察。"""
    html = "<html><body>" + "x" * 5000 + "</body></html>"
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q")
    assert res["error_code"] == "PARSE_ERROR"
    assert res["status"] == "failed"
    assert "provider" in res["message"].lower() or "layout" in res["message"].lower()


def test_request_exception_maps_to_network_error(monkeypatch) -> None:
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(RuntimeError("conn refused")))
    res = search_web_topic(query="q")
    assert res["error_code"] == "NETWORK_ERROR"
    assert res["status"] == "failed"


def test_http_error_maps_to_network_error(monkeypatch) -> None:
    monkeypatch.setattr(
        cap_mod, "requests", _FakeRequests(_FakeResponse("x", status=503))
    )
    res = search_web_topic(query="q")
    assert res["error_code"] == "NETWORK_ERROR"


def test_missing_requests_library_maps_to_network_error(monkeypatch) -> None:
    monkeypatch.setattr(cap_mod, "_HAS_REQUESTS", False)
    res = search_web_topic(query="q")
    assert res["error_code"] == "NETWORK_ERROR"
    assert res["status"] == "failed"


# ---------------------------------------------------------------------------
# 既有分支语义保持
# ---------------------------------------------------------------------------
def test_unknown_provider_maps_to_failed_status() -> None:
    res = search_web_topic(query="x", provider="tavily_404")
    assert res["error_code"] == "UNKNOWN_PROVIDER"
    assert res["status"] == "failed"
    assert res["query_hash"]  # 统一留痕:非 DDG 分支也带 status/hash/时间


def test_stub_provider_keeps_error_code_none() -> None:
    res = search_web_topic(query="任何", provider="stub")
    assert res["error_code"] is None
    assert res["search_web_topic"] == []
    assert res["status"] == "no_results"


def test_empty_query_maps_to_no_results() -> None:
    res = search_web_topic(query="   ")
    assert res["error_code"] == "NO_RESULTS"
    assert res["status"] == "no_results"
