"""单测:search_web_topic 能力层(2026-11-10 阶段2:前置化设计方案 §4.2)。

锁定:snippet 提取与清洗 / URL scheme 白名单 / 单条+累计限长护栏 /
错误分类(NETWORK_ERROR、PARSE_ERROR 与 NO_RESULTS 分离) /
status + query_hash + searched_at 统一留痕。

2026-11-19 审查修订版 §8.1 扩展:google_html provider(强制 socks5h:// 代理、
/url?q= 包装还原、挑战页 PROVIDER_BLOCKED、HTTP_ERROR/PROXY_ERROR 分离、
URL 归一化去重、诊断与受限 debug 落盘、request_hash 稳定性)。

mock 手法:monkeypatch 模块属性 ``requests`` 注入 FakeResponse(仓库无
requests_mock 依赖,沿用注入式 fake 惯例);``_HAS_REQUESTS`` 标志同理。
"""
from __future__ import annotations

import base64
import hashlib
import urllib.parse
from typing import Any, Union

import pytest
import requests

import storyline_capabilities.search_web_topic as cap_mod
from storyline_capabilities.search_web_topic import (
    WEB_TOPIC_SNIPPET_MAX_CHARS,
    WEB_TOPIC_TOTAL_CONTEXT_MAX_CHARS,
    build_request_hash,
    search_web_topic,
)


class _FakeResponse:
    def __init__(
        self,
        text: str,
        status: int = 200,
        url: str = "https://www.google.com/search?q=x",
    ) -> None:
        self.text = text
        self.status_code = status
        self.url = url
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            err = requests.exceptions.HTTPError(f"HTTP {self._status}")
            err.response = self
            raise err


class _FakeRequests:
    """替换能力层模块属性 ``requests``;response_or_exc 为 Exception 时直接抛出。"""

    def __init__(self, response_or_exc: Union[_FakeResponse, Exception]) -> None:
        self._resp = response_or_exc
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params=None, headers=None, timeout=None, proxies=None):
        self.calls.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
                "proxies": proxies,
            }
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


def _google_result_html(results: list[tuple[str, str, str]]) -> str:
    """(href, title, snippet) → 模拟 Google SERP 结果页(div.g + h3 + VwiC3b)。"""
    blocks = []
    for href, title, snippet in results:
        blocks.append(
            '<div class="g Ww4FFb vowF2b">'
            f'<a href="{href}"><h3 class="LC20lb MBeuO">{title}</h3></a>'
            f'<div class="VwiC3b MNbdRd">{snippet}</div>'
            "</div>"
        )
    return "<html><body>" + "".join(blocks) + "</body></html>"


_SOCKS = "socks5h://127.0.0.1:1088"


# ---------------------------------------------------------------------------
# 正常解析路径(DuckDuckGo 存量)
# ---------------------------------------------------------------------------
def test_results_include_snippet_and_decoded_uddg_url(monkeypatch) -> None:
    inner = urllib.parse.quote("https://example.com/article?x=1&y=2", safe="")
    html = _result_html(
        [(f"https://duckduckgo.com/l/?uddg={inner}", "防晒 &amp; 补水", "摘要 &lt;b&gt;文本&lt;/b&gt;")]
    )
    fake = _FakeRequests(_FakeResponse(html, url="https://duckduckgo.com/html/?q=x"))
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
    # schema v2 契约:引擎展示名/适配器标识/请求语义 hash 全部落盘
    assert res["engine"] == "DuckDuckGo"
    assert res["provider"] == "duckduckgo_html"
    assert res["schema_version"] == 2
    assert res["request_hash"] == build_request_hash(
        query="夏季防晒", provider="duckduckgo_html", max_results=10
    )
    assert res["diagnostics"]["parser_version"] == "ddg_html_v1"


def test_status_query_hash_and_searched_at_are_stamped(monkeypatch) -> None:
    monkeypatch.setattr(
        cap_mod,
        "requests",
        _FakeRequests(
            _FakeResponse(_result_html([("https://e.com/1", "防晒指南", "s")]))
        ),
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
# 错误分类(DuckDuckGo 存量)
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


# ---------------------------------------------------------------------------
# 2026-11-19 审查修订版 §8.1:google_html 用例矩阵
# ---------------------------------------------------------------------------
def test_google_normal_results_parse_with_socks_proxy(monkeypatch) -> None:
    """正常解析:直连外链 + /url?q= 包装混合;代理以 http/https 双 scheme 显式传入。"""
    wrapped = "/url?q=" + urllib.parse.quote(
        "https://b.com/2?k=1&lang=zh", safe=""
    ) + "&sa=U"
    html = _google_result_html(
        [
            ("https://a.com/1", "防晒指南", "户外防晒要点"),
            (wrapped, "防晒补水面霜", "保湿测评汇总"),
        ]
    )
    fake = _FakeRequests(_FakeResponse(html))
    monkeypatch.setattr(cap_mod, "requests", fake)

    res = search_web_topic(
        query="夏季防晒", provider="google_html", proxy_url=_SOCKS
    )

    assert res["error_code"] is None
    assert res["status"] == "ok"
    assert res["engine"] == "Google"
    assert res["provider"] == "google_html"
    assert res["schema_version"] == 2
    assert res["request_hash"] == build_request_hash(
        query="夏季防晒", provider="google_html", max_results=10
    )
    items = res["search_web_topic"]
    assert [it["url"] for it in items] == ["https://a.com/1", "https://b.com/2?k=1&lang=zh"]
    assert [it["title"] for it in items] == ["防晒指南", "防晒补水面霜"]
    assert [it["snippet"] for it in items] == ["户外防晒要点", "保湿测评汇总"]
    assert all(it["source"] == "google_html" for it in items)
    # 请求形态:代理双 scheme、参数 q/num/hl、超时元组(§5.3)
    call = fake.calls[0]
    assert call["proxies"] == {"http": _SOCKS, "https": _SOCKS}
    assert call["params"]["q"] == "夏季防晒"
    assert call["params"]["hl"] == "zh-CN"
    assert call["timeout"] == (5, 15)
    diag = res["diagnostics"]
    assert diag["proxy_enabled"] is True
    assert diag["parser_version"] == "google_html_v1"
    assert diag["http_status"] == 200


def test_google_entity_in_href_is_decoded(monkeypatch) -> None:
    """href 里的 &amp; 实体与 URL 编码参数必须还原,不破坏 query(§8.1)。"""
    html = _google_result_html(
        [("https://a.com/p?x=1&amp;y=%E9%98%B2", "标题", "摘要")]
    )
    monkeypatch.setattr(
        cap_mod, "requests", _FakeRequests(_FakeResponse(html))
    )
    res = search_web_topic(query="q", provider="google_html", proxy_url=_SOCKS)
    assert res["search_web_topic"][0]["url"] == "https://a.com/p?x=1&y=%E9%98%B2"


def test_google_drops_internal_and_bad_urls_and_dedupes(monkeypatch) -> None:
    """javascript:/站内相对路径/Google 导航地址丢弃;重复 URL 去重(§5.4-6)。"""
    html = _google_result_html(
        [
            ("javascript:alert(1)", "坏1", "摘要1"),
            ("/search?q=x", "站内搜索", "摘要2"),
            ("https://www.google.com/preferences", "Google偏好", "摘要3"),
            ("https://c.com/1", "重复页", "摘要4"),
            ("https://c.com/1", "重复页(again)", "摘要5"),
            ("https://d.com/", "正常外链", "摘要6"),
        ]
    )
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q", provider="google_html", proxy_url=_SOCKS)
    assert [it["url"] for it in res["search_web_topic"]] == ["https://c.com/1", "https://d.com/"]


def test_google_no_results_marker_maps_to_no_results(monkeypatch) -> None:
    html = "<html><body><p>未找到和您查询相关的结果。</p></body></html>"
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q", provider="google_html", proxy_url=_SOCKS)
    assert res["error_code"] == "NO_RESULTS"
    assert res["status"] == "no_results"


def test_google_unknown_structure_maps_to_parse_error(monkeypatch) -> None:
    """200 但既无结果结构也无无结果标记 → PARSE_ERROR(不误判 NO_RESULTS,§5.4-8)。"""
    html = "<html><body>" + "x" * 5000 + "</body></html>"
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q", provider="google_html", proxy_url=_SOCKS)
    assert res["error_code"] == "PARSE_ERROR"
    assert res["status"] == "failed"


def test_google_challenge_page_maps_to_provider_blocked(monkeypatch) -> None:
    """HTTP 202 + 异常流量挑战页 → PROVIDER_BLOCKED,不得当正常页解析(§5.4-3)。"""
    html = "<html><body>Our systems have detected unusual traffic from your computer network. <div class=\"g-recaptcha\"></div></body></html>"
    monkeypatch.setattr(
        cap_mod, "requests", _FakeRequests(_FakeResponse(html, status=202))
    )
    res = search_web_topic(query="q", provider="google_html", proxy_url=_SOCKS)
    assert res["error_code"] == "PROVIDER_BLOCKED"
    assert res["status"] == "failed"
    assert "blocked" in res["message"].lower()


def test_google_consent_redirect_maps_to_provider_blocked(monkeypatch) -> None:
    """最终 URL 落在 consent.google.com → PROVIDER_BLOCKED(§5.4-3)。"""
    html = _google_result_html([("https://a.com/1", "t", "s")])
    monkeypatch.setattr(
        cap_mod,
        "requests",
        _FakeRequests(
            _FakeResponse(html, url="https://consent.google.com/m?hl=zh-CN&continue=x")
        ),
    )
    res = search_web_topic(query="q", provider="google_html", proxy_url=_SOCKS)
    assert res["error_code"] == "PROVIDER_BLOCKED"


def test_google_http_error_maps_to_http_error(monkeypatch) -> None:
    """HTTP 503 且无挑战特征 → HTTP_ERROR(§4.4 错误码表)。"""
    monkeypatch.setattr(
        cap_mod, "requests", _FakeRequests(_FakeResponse("Service Unavailable", status=503))
    )
    res = search_web_topic(query="q", provider="google_html", proxy_url=_SOCKS)
    assert res["error_code"] == "HTTP_ERROR"
    assert res["status"] == "failed"
    assert res["diagnostics"]["http_status"] == 503


def test_google_http_403_with_challenge_maps_to_provider_blocked(monkeypatch) -> None:
    """4xx/5xx 但响应体含挑战特征 → PROVIDER_BLOCKED 优先于 HTTP_ERROR。"""
    monkeypatch.setattr(
        cap_mod,
        "requests",
        _FakeRequests(
            _FakeResponse("unusual traffic detected", status=403)
        ),
    )
    res = search_web_topic(query="q", provider="google_html", proxy_url=_SOCKS)
    assert res["error_code"] == "PROVIDER_BLOCKED"


def test_google_network_exception_maps_to_network_error(monkeypatch) -> None:
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(RuntimeError("dns fail")))
    res = search_web_topic(query="q", provider="google_html", proxy_url=_SOCKS)
    assert res["error_code"] == "NETWORK_ERROR"
    assert res["status"] == "failed"


def test_google_proxy_exception_maps_to_proxy_error(monkeypatch) -> None:
    """ProxyError / SOCKS 依赖缺失(InvalidSchema)→ PROXY_ERROR,不回退直连(§5.3)。"""
    for exc in (
        requests.exceptions.ProxyError("proxy down"),
        requests.exceptions.InvalidSchema(
            "Missing dependencies for SOCKS support."
        ),
    ):
        monkeypatch.setattr(cap_mod, "requests", _FakeRequests(exc))
        res = search_web_topic(query="q", provider="google_html", proxy_url=_SOCKS)
        assert res["error_code"] == "PROXY_ERROR"
        assert res["status"] == "failed"


def test_google_missing_proxy_maps_to_proxy_error_without_request(monkeypatch) -> None:
    """google_html 强制代理:缺失 → PROXY_ERROR 且**不发请求**(§5.1-2/§5.3)。"""
    fake = _FakeRequests(_FakeResponse(_google_result_html([("https://a.com/1", "t", "s")])))
    monkeypatch.setattr(cap_mod, "requests", fake)
    res = search_web_topic(query="q", provider="google_html", proxy_url=None)
    assert res["error_code"] == "PROXY_ERROR"
    assert fake.calls == []


def test_google_wrong_proxy_scheme_maps_to_proxy_error(monkeypatch) -> None:
    """代理 scheme 必须 socks5h(http:// 或空 scheme 拒绝),不发请求(§5.1-2)。"""
    fake = _FakeRequests(_FakeResponse(_google_result_html([("https://a.com/1", "t", "s")])))
    monkeypatch.setattr(cap_mod, "requests", fake)
    res = search_web_topic(
        query="q", provider="google_html", proxy_url="http://127.0.0.1:3067"
    )
    assert res["error_code"] == "PROXY_ERROR"
    assert fake.calls == []


def test_google_snippet_capped_per_item_and_in_total(monkeypatch) -> None:
    long_snip = "摘" * 1000
    html = _google_result_html(
        [(f"https://e.com/{i}", f"标题{i}", long_snip) for i in range(10)]
    )
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q", provider="google_html", proxy_url=_SOCKS)
    items = res["search_web_topic"]
    assert len(items) == 10
    assert len(items[0]["snippet"]) == WEB_TOPIC_SNIPPET_MAX_CHARS
    assert sum(len(it["snippet"]) for it in items) <= WEB_TOPIC_TOTAL_CONTEXT_MAX_CHARS
    assert items[-1]["snippet"] == ""
    assert items[-1]["title"] == "标题9"


def test_google_debug_dump_written_on_parse_error_only_when_enabled(
    monkeypatch, tmp_path
) -> None:
    """debug 关:不落盘;debug 开(parse_error 时):≤64KiB 纯文本 + 头部诊断(§5.5)。"""
    html = "<html><body>" + "x" * 5000 + "</body></html>"

    # debug 关:不写文件
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q", provider="google_html", proxy_url=_SOCKS)
    assert res["error_code"] == "PARSE_ERROR"
    assert not (tmp_path / "search_web_topic_debug.txt").exists()

    # debug 开:写受限文本,头部带诊断 JSON
    fake = _FakeRequests(_FakeResponse(html))
    monkeypatch.setattr(cap_mod, "requests", fake)
    res = search_web_topic(
        query="q", provider="google_html", proxy_url=_SOCKS, debug_dir=tmp_path
    )
    assert res["error_code"] == "PARSE_ERROR"
    dump = tmp_path / "search_web_topic_debug.txt"
    assert dump.exists()
    assert dump.stat().st_size <= cap_mod.WEB_TOPIC_DEBUG_MAX_BYTES
    head = dump.read_text(encoding="utf-8", errors="replace")
    assert head.startswith("{")
    assert "google_html_v1" in head


def test_google_debug_dump_failure_is_silent(monkeypatch, tmp_path) -> None:
    """debug 目录不可写时:静默吞掉,检索错误分类不受影响(§5.5)。"""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("occupied", encoding="utf-8")
    html = "<html><body>" + "x" * 5000 + "</body></html>"
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(
        query="q", provider="google_html", proxy_url=_SOCKS, debug_dir=blocker
    )
    assert res["error_code"] == "PARSE_ERROR"


def test_build_request_hash_differs_by_provider_query_and_params() -> None:
    """request_hash:换 provider/改 query/改 max_results 必不同;同输入稳定(§4.5)。"""
    base = build_request_hash(query="q", provider="duckduckgo_html", max_results=10)
    assert base == build_request_hash(
        query="q", provider="duckduckgo_html", max_results=10
    )
    assert base != build_request_hash(query="q", provider="google_html", max_results=10)
    assert base != build_request_hash(query="q", provider="bing_html", max_results=10)
    assert base != build_request_hash(query="其他", provider="duckduckgo_html", max_results=10)
    assert base != build_request_hash(query="q", provider="duckduckgo_html", max_results=5)
    # hash 不含代理地址与凭据(§4.5)
    assert _SOCKS not in base and "127.0.0.1" not in base


# ---------------------------------------------------------------------------
# 2026-11-19 定案:bing_html 默认引擎用例矩阵(h2>a 嵌套 + b_caption 摘要)
# ---------------------------------------------------------------------------
def _bing_result_html(results: list[tuple[str, str, str]]) -> str:
    """(href, title, snippet) → 模拟 Bing SERP 结果页(li.b_algo + h2>a + b_caption)。"""
    blocks = []
    for href, title, snippet in results:
        blocks.append(
            '<li class="b_algo svx4">'
            f'<h2><a href="{href}">{title}</a></h2>'
            f'<div class="b_caption"><p class="b_lineclamp4">{snippet}</p></div>'
            "</li>"
        )
    return "<html><body><ol id=\"b_results\">" + "".join(blocks) + "</ol></body></html>"


def test_bing_normal_results_parse_direct_and_no_proxy(monkeypatch) -> None:
    """正常解析(h2>a 嵌套 + b_caption 摘要);默认无代理 → 直连,proxies 不传。"""
    html = _bing_result_html(
        [("https://a.com/guide", "马六甲海上清真寺攻略", "清真寺门票与着装要求")]
    )
    fake = _FakeRequests(_FakeResponse(html, url="https://www.bing.com/search?q=x"))
    monkeypatch.setattr(cap_mod, "requests", fake)

    res = search_web_topic(query="马六甲清真寺", provider="bing_html")

    assert res["error_code"] is None
    assert res["status"] == "ok"
    assert res["engine"] == "Bing"
    assert res["provider"] == "bing_html"
    assert res["schema_version"] == 2
    assert res["request_hash"] == build_request_hash(
        query="马六甲清真寺", provider="bing_html", max_results=10
    )
    (item,) = res["search_web_topic"]
    assert item["url"] == "https://a.com/guide"
    assert item["title"] == "马六甲海上清真寺攻略"
    assert item["snippet"] == "清真寺门票与着装要求"
    assert item["source"] == "bing_html"
    call = fake.calls[0]
    assert call["params"] == {"q": "马六甲清真寺", "mkt": "zh-CN"}
    assert call["timeout"] == (5, 15)
    assert call["proxies"] is None  # 未给代理 → 直连(bing_html 代理可选)
    assert res["diagnostics"]["parser_version"] == "bing_html_v1"


def test_bing_ck_a_wrapped_url_unwrapped(monkeypatch) -> None:
    """Bing 加密跳转 /ck/a?u=a1<base64> 还原为真实外链。"""
    enc = "a1" + base64.urlsafe_b64encode(b"https://real.com/page?a=1&b=2").decode().rstrip("=")
    html = _bing_result_html([(f"https://www.bing.com/ck/a?!&&u={enc}", "标题", "摘要")])
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q", provider="bing_html")
    assert res["search_web_topic"][0]["url"] == "https://real.com/page?a=1&b=2"


def test_bing_proxy_passthrough_when_configured(monkeypatch) -> None:
    """配置了代理 → bing 请求以 http/https 双 scheme 经代理出口。"""
    fake = _FakeRequests(
        _FakeResponse(_bing_result_html([("https://a.com/1", "t", "s")]))
    )
    monkeypatch.setattr(cap_mod, "requests", fake)
    res = search_web_topic(query="q", provider="bing_html", proxy_url=_SOCKS)
    assert res["status"] == "ok"
    assert fake.calls[0]["proxies"] == {"http": _SOCKS, "https": _SOCKS}
    assert res["diagnostics"]["proxy_enabled"] is True


def test_bing_drops_internal_and_bad_urls_and_dedupes(monkeypatch) -> None:
    """javascript:/站内相对路径/Bing 导航地址丢弃;重复 URL 去重。"""
    html = _bing_result_html(
        [
            ("javascript:alert(1)", "坏1", "摘要1"),
            ("/search?q=x", "站内搜索", "摘要2"),
            ("https://www.bing.com/images", "Bing图片", "摘要3"),
            ("https://c.com/1", "重复页", "摘要4"),
            ("https://c.com/1", "重复页2", "摘要5"),
            ("https://d.com/", "正常外链", "摘要6"),
        ]
    )
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q", provider="bing_html")
    assert [it["url"] for it in res["search_web_topic"]] == ["https://c.com/1", "https://d.com/"]


def test_bing_no_results_marker_maps_to_no_results(monkeypatch) -> None:
    html = '<html><body><div class="b_no">没有找到与 q 相关的结果</div></body></html>'
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q", provider="bing_html")
    assert res["error_code"] == "NO_RESULTS"
    assert res["status"] == "no_results"


def test_bing_unknown_structure_maps_to_parse_error(monkeypatch) -> None:
    html = "<html><body>" + "x" * 5000 + "</body></html>"
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q", provider="bing_html")
    assert res["error_code"] == "PARSE_ERROR"
    assert res["status"] == "failed"


def test_bing_challenge_page_maps_to_provider_blocked(monkeypatch) -> None:
    html = "<html><body>please complete the captcha to continue</body></html>"
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q", provider="bing_html")
    assert res["error_code"] == "PROVIDER_BLOCKED"
    assert res["status"] == "failed"


def test_bing_http_error_maps_to_http_error(monkeypatch) -> None:
    monkeypatch.setattr(
        cap_mod, "requests", _FakeRequests(_FakeResponse("Unavailable", status=503))
    )
    res = search_web_topic(query="q", provider="bing_html")
    assert res["error_code"] == "HTTP_ERROR"
    assert res["diagnostics"]["http_status"] == 503


def test_bing_network_exception_maps_to_network_error(monkeypatch) -> None:
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(RuntimeError("conn fail")))
    res = search_web_topic(query="q", provider="bing_html")
    assert res["error_code"] == "NETWORK_ERROR"


def test_bing_snippet_capped_per_item_and_in_total(monkeypatch) -> None:
    long_snip = "摘" * 1000
    html = _bing_result_html(
        [(f"https://e.com/{i}", f"标题{i}", long_snip) for i in range(10)]
    )
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="q", provider="bing_html")
    items = res["search_web_topic"]
    assert len(items) == 10
    assert len(items[0]["snippet"]) == WEB_TOPIC_SNIPPET_MAX_CHARS
    assert sum(len(it["snippet"]) for it in items) <= WEB_TOPIC_TOTAL_CONTEXT_MAX_CHARS
    assert items[-1]["snippet"] == ""


def test_bing_debug_dump_written_on_parse_error(monkeypatch, tmp_path) -> None:
    html = "<html><body>" + "x" * 5000 + "</body></html>"
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(
        query="q", provider="bing_html", debug_dir=tmp_path
    )
    assert res["error_code"] == "PARSE_ERROR"
    dump = tmp_path / "search_web_topic_debug.txt"
    assert dump.exists()
    assert dump.stat().st_size <= cap_mod.WEB_TOPIC_DEBUG_MAX_BYTES
    assert "bing_html_v1" in dump.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 2026-11-19 真机事故修订:相关性守卫(IRRELEVANT_RESULTS)+ 指令括号剥离
# ---------------------------------------------------------------------------
def test_strip_instruction_parentheticals_variants() -> None:
    """指令括号段剥离:半角/全角括号、独立指令段、无指令动词的括号原样保留。"""
    assert (
        cap_mod.strip_instruction_parentheticals(
            "海上清真寺旅游攻略，开放时间(勿写平台名)"
        )
        == "海上清真寺旅游攻略，开放时间"
    )
    assert (
        cap_mod.strip_instruction_parentheticals("攻略（不要出现品牌名）日落")
        == "攻略日落"
    )
    assert cap_mod.strip_instruction_parentheticals("(勿写平台名)") == ""
    # 括号内无指令动词 → 视为正常检索词,原样保留
    assert (
        cap_mod.strip_instruction_parentheticals("马尔代夫(度假)攻略")
        == "马尔代夫(度假)攻略"
    )


def test_bing_irrelevant_results_map_to_irrelevant_results(
    monkeypatch, tmp_path
) -> None:
    """真机事故回归:整页无关降级 SERP(AHU 教务页)→ IRRELEVANT_RESULTS + 落盘取证。"""
    html = _bing_result_html(
        [
            (
                "https://jw.ahu.edu.cn/",
                "登录页面",
                "安徽大学教务系统登录页面，提供统一身份认证及账户激活服务。",
            ),
            (
                "https://one.ahu.edu.cn/cas/login",
                "智慧安大",
                "记住用户名 绑定此设备 忘记密码？360浏览器请使用极速模式",
            ),
            (
                "https://jwc.ahu.edu.cn/",
                "教务处",
                "教务处公众号 友情链接：地址:安徽省合肥市经济技术开发区九龙路111号",
            ),
        ]
    )
    monkeypatch.setattr(
        cap_mod,
        "requests",
        _FakeRequests(_FakeResponse(html, url="https://www.bing.com/search?q=x")),
    )
    res = search_web_topic(
        query="海上清真寺旅游攻略", provider="bing_html", debug_dir=tmp_path
    )
    assert res["error_code"] == "IRRELEVANT_RESULTS"
    assert res["status"] == "failed"
    assert res["search_web_topic"] == []
    assert res["diagnostics"]["relevance_matched"] == 0
    assert res["diagnostics"]["relevance_total"] == 3
    dump = tmp_path / "search_web_topic_debug.txt"
    assert dump.exists()
    assert "jw.ahu.edu.cn" in dump.read_text(encoding="utf-8", errors="replace")


def test_bing_relevant_results_pass_relevance_guard(monkeypatch) -> None:
    """正常相关结果不受守卫影响;diagnostics 留痕命中数供审计。"""
    html = _bing_result_html(
        [
            ("https://a.com/1", "马六甲海上清真寺旅游攻略", "清真寺开放时间与着装要求"),
            ("https://b.com/2", "海上清真寺打卡指南", "日落时分最美"),
            ("https://c.com/3", "清真寺参观须知", "门票与交通汇总"),
        ]
    )
    monkeypatch.setattr(
        cap_mod,
        "requests",
        _FakeRequests(_FakeResponse(html, url="https://www.bing.com/search?q=x")),
    )
    res = search_web_topic(query="海上清真寺旅游攻略", provider="bing_html")
    assert res["error_code"] is None
    assert res["status"] == "ok"
    assert res["diagnostics"]["relevance_matched"] == 3
    assert res["diagnostics"]["relevance_total"] == 3


def test_bing_relevance_guard_partial_junk_trips(monkeypatch) -> None:
    """10 条里仅 1 条相关 → 仍判不相关(阈值 2,结果数不足阈值时须全命中)。"""
    html = _bing_result_html(
        [("https://good.com/1", "海上清真寺攻略", "清真寺介绍")]
        + [
            (f"https://x.com/{i}", f"教务系统{i}", f"安徽大学教务处页面{i}")
            for i in range(2, 11)
        ]
    )
    monkeypatch.setattr(
        cap_mod,
        "requests",
        _FakeRequests(_FakeResponse(html, url="https://www.bing.com/search?q=x")),
    )
    res = search_web_topic(query="海上清真寺旅游攻略", provider="bing_html")
    assert res["error_code"] == "IRRELEVANT_RESULTS"
    assert res["diagnostics"]["relevance_matched"] == 1


def test_bing_relevance_guard_skipped_when_query_has_no_tokens(monkeypatch) -> None:
    """单字符 query 切不出 token → 守卫跳过(无法判定时不拦截)。"""
    html = _bing_result_html([("https://a.com/1", "任意标题", "任意摘要")])
    monkeypatch.setattr(
        cap_mod,
        "requests",
        _FakeRequests(_FakeResponse(html, url="https://www.bing.com/search?q=x")),
    )
    res = search_web_topic(query="q", provider="bing_html")
    assert res["status"] == "ok"


def test_google_irrelevant_results_map_to_irrelevant_results(monkeypatch) -> None:
    """守卫对 google_html 同样生效(三引擎统一语义);message 带命中计数。"""
    html = _google_result_html(
        [
            ("https://jw.ahu.edu.cn/", "登录页面", "安徽大学教务系统登录页"),
            ("https://one.ahu.edu.cn/", "智慧安大", "统一身份认证登录"),
            ("https://jwc.ahu.edu.cn/", "教务处", "友情链接与联系方式"),
        ]
    )
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(
        query="海上清真寺旅游攻略", provider="google_html", proxy_url=_SOCKS
    )
    assert res["error_code"] == "IRRELEVANT_RESULTS"
    assert res["status"] == "failed"
    assert res["message"].endswith("relevance matched 0/3 results.")


def test_ddg_irrelevant_results_map_to_irrelevant_results(monkeypatch) -> None:
    """守卫对 duckduckgo_html 同样生效。"""
    html = _result_html(
        [
            ("https://jw.ahu.edu.cn/", "登录页面", "安徽大学教务系统登录页"),
            ("https://jwc.ahu.edu.cn/", "教务处", "友情链接与联系方式"),
        ]
    )
    monkeypatch.setattr(cap_mod, "requests", _FakeRequests(_FakeResponse(html)))
    res = search_web_topic(query="海上清真寺旅游攻略")
    assert res["error_code"] == "IRRELEVANT_RESULTS"
    assert res["status"] == "failed"


# ---------------------------------------------------------------------------
# reach_mcp 分支(2026-11-20 第九期:审查修订版 §5.4;经 reach_client 接缝注入)
# ---------------------------------------------------------------------------
_REACH_OPTS_BASE = {
    "endpoint": "http://127.0.0.1:8765/mcp",
    "sources": ("youtube", "web", "reddit"),
    "days": 30,
    "max_per_source": 5,
    "max_chars_per_item": 500,
    "timeout_seconds": 90,
}


def _install_reach(monkeypatch, raw_items, error=None, message="", diag=None):
    calls: list[dict] = []

    def fake_search_via_reach(**kwargs):
        calls.append(kwargs)
        return raw_items, error, message, (diag or {"sources_used": []})

    monkeypatch.setattr(cap_mod._reach_client, "search_via_reach", fake_search_via_reach)
    return calls


def _reach_item(url="https://www.youtube.com/watch?v=abc", title="A", text="snippet A", source="youtube", **extra):
    item = {"url": url, "title": title, "text": text, "source": source}
    item.update(extra)
    return item


def test_reach_branch_success_maps_and_balances_quota(monkeypatch) -> None:
    raw = [
        _reach_item(url="https://a/1", title="Portable Web One", text="first portable", source="web"),
        _reach_item(url="https://a/2", title="Power Web Two", text="second power", source="web"),
        _reach_item(url="https://a/3", title="Station Web Three", text="third station", source="web"),
        _reach_item(url="https://v/1", title="Portable Video One", source="youtube", engagement={"views": 100}),
        _reach_item(url="https://v/2", title="Power Video Two", source="youtube", engagement=7),
    ]
    calls = _install_reach(monkeypatch, raw)
    res = search_web_topic(
        query="portable power station",
        max_results=4,
        provider="reach_mcp",
        provider_options=dict(_REACH_OPTS_BASE),
    )
    assert res["status"] == "ok" and res["provider"] == "reach_mcp"
    assert res["engine"] == "ReachMCP(海外检索)"
    assert res["error_code"] is None
    # round-robin 均衡:web 与 youtube 交替,单源不占满 4 个名额(§3.7)
    sources = [it["source"] for it in res["search_web_topic"]]
    assert sources == ["web", "youtube", "web", "youtube"]
    it = res["search_web_topic"][0]
    assert it["reach_source"] == "web" and it["date"] is None
    assert res["search_web_topic"][1]["engagement_views"] == 100
    assert res["search_web_topic"][3]["engagement_views"] == 7
    # 请求参数透传契约
    assert calls[0]["sources"] == ["youtube", "web", "reddit"]
    assert calls[0]["max_per_source"] == 5 and calls[0]["days"] == 30
    # diagnostics 携带 parser/adapter 版本
    assert res["diagnostics"]["adapter_version"] == "1"
    assert res["diagnostics"]["parser_version"] == "reach_mcp_v1"


def test_reach_branch_url_validation_title_fallback_and_dedupe(monkeypatch) -> None:
    raw = [
        _reach_item(url="ftp://bad", title="dropped scheme"),
        _reach_item(url="https://ok/1", title="", text="fallback title from text"),
        _reach_item(url="https://ok/2", title="", text=""),
        _reach_item(url="https://ok/1", title="duplicate url"),
    ]
    _install_reach(monkeypatch, raw)
    res = search_web_topic(
        query="anything at all", max_results=10, provider="reach_mcp",
        provider_options=dict(_REACH_OPTS_BASE),
    )
    items = res["search_web_topic"]
    assert [it["url"] for it in items] == ["https://ok/1"]
    assert items[0]["title"] == "fallback title from text"


def test_reach_branch_engagement_normalization(monkeypatch) -> None:
    raw = [
        _reach_item(url="https://a/1", title="engagement dict views", engagement={"views": 42, "likes": 1}),
        _reach_item(url="https://a/2", title="engagement plain number", engagement=15),
        _reach_item(url="https://a/3", title="engagement negative", engagement=-3),
        _reach_item(url="https://a/4", title="engagement missing", engagement=None),
        _reach_item(url="https://a/5", title="engagement empty dict", engagement={}),
    ]
    _install_reach(monkeypatch, raw)
    res = search_web_topic(
        query="engagement test", max_results=10, provider="reach_mcp",
        provider_options=dict(_REACH_OPTS_BASE),
    )
    views = {it["url"]: it["engagement_views"] for it in res["search_web_topic"]}
    assert views == {
        "https://a/1": 42,
        "https://a/2": 15,
        "https://a/3": None,
        "https://a/4": None,
        "https://a/5": None,
    }


def test_reach_branch_relevance_guard_blocks_without_fallback(monkeypatch) -> None:
    raw = [
        _reach_item(url="https://x/1", title="安徽大学教务系统登录页", text="友情链接", source="web"),
        _reach_item(url="https://x/2", title="教务处通知", text="与检索词无关", source="web"),
    ]
    _install_reach(monkeypatch, raw)
    res = search_web_topic(
        query="quantum computing breakthrough",
        max_results=10, provider="reach_mcp",
        provider_options=dict(_REACH_OPTS_BASE),
    )
    assert res["error_code"] == "IRRELEVANT_RESULTS"
    assert res["status"] == "failed"
    # 英文线无 Bing 回退(§4.2):不得出现 fallback 键
    assert "fallback" not in res
    assert res["diagnostics"]["relevance_matched"] == 0


def test_reach_branch_no_results_status(monkeypatch) -> None:
    _install_reach(monkeypatch, None, error="NO_RESULTS", message="reach-mcp returned no items")
    res = search_web_topic(
        query="portable power station", max_results=10, provider="reach_mcp",
        provider_options=dict(_REACH_OPTS_BASE),
    )
    assert res["status"] == "no_results" and res["error_code"] == "NO_RESULTS"
    assert res["search_web_topic"] == []


def test_reach_branch_network_error_status(monkeypatch) -> None:
    _install_reach(monkeypatch, None, error="NETWORK_ERROR", message="reach-mcp request timed out")
    res = search_web_topic(
        query="portable power station", max_results=10, provider="reach_mcp",
        provider_options=dict(_REACH_OPTS_BASE),
    )
    assert res["status"] == "failed" and res["error_code"] == "NETWORK_ERROR"


def test_reach_branch_request_hash_isolates_every_param(monkeypatch) -> None:
    _install_reach(monkeypatch, [])
    hashes = []
    for overrides in (
        {},
        {"sources": ("youtube", "web")},
        {"max_per_source": 6},
        {"days": 60},
        {"max_chars_per_item": 200},
    ):
        res = search_web_topic(
            query="same query", max_results=10, provider="reach_mcp",
            provider_options={**_REACH_OPTS_BASE, **overrides},
        )
        hashes.append(res["request_hash"])
    assert len(set(hashes)) == len(hashes), "任一参数变化都必须改变 request_hash"
    # 同参数幂等
    again = search_web_topic(
        query="same query", max_results=10, provider="reach_mcp",
        provider_options=dict(_REACH_OPTS_BASE),
    )
    assert again["request_hash"] == hashes[0]


def test_reach_branch_missing_provider_options_raises(monkeypatch) -> None:
    _install_reach(monkeypatch, [])
    with pytest.raises(ValueError):
        search_web_topic(query="q", provider="reach_mcp", provider_options={"endpoint": "http://x"})
    with pytest.raises(ValueError):
        search_web_topic(query="q", provider="reach_mcp", provider_options={"sources": ("web",)})


def test_reach_branch_failure_keeps_request_hash(monkeypatch) -> None:
    """失败产物同样携带 request_hash(节点层缓存对 failed 产物复用的前提)。"""
    _install_reach(monkeypatch, None, error="NETWORK_ERROR", message="down")
    res = search_web_topic(
        query="portable power station", max_results=10, provider="reach_mcp",
        provider_options=dict(_REACH_OPTS_BASE),
    )
    assert res["request_hash"]


def test_reach_branch_max_results_cap(monkeypatch) -> None:
    raw = [_reach_item(url=f"https://a/{i}", title=f"cap test item {i}", source="web") for i in range(9)]
    _install_reach(monkeypatch, raw)
    res = search_web_topic(
        query="cap test", max_results=3, provider="reach_mcp",
        provider_options=dict(_REACH_OPTS_BASE),
    )
    assert len(res["search_web_topic"]) == 3
