"""本地化 search_web_topic(plan_v4 §5 阶段 1 / A 类)。

策略:
- 本地实现基于 DuckDuckGo 的 ``/html/`` 端点(无需 API key),返回搜索结果列表
  (text / image / topic)。
- 真正复杂的 web 主题抓取(Tavily / Bing Search API 等)在阶段 1 仅留接口 +
  stub;阶段 5 接入真实供应商。
- 通过 ``search_provider`` 名字注入位,默认 ``"duckduckgo_html"``;None 时返回
  empty。

设计约束(plan §5 阶段 1 第 1~3 行):本模块不依赖 FireRed NodeState / 任何
私有框架;可作为独立 CLI/Script 跑。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import requests  # type: ignore[import-untyped]
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 限长护栏与错误分类(2026-11-10 阶段2:前置化设计方案 §4.2)
# ---------------------------------------------------------------------------
WEB_TOPIC_SNIPPET_MAX_CHARS: int = 600
WEB_TOPIC_TOTAL_CONTEXT_MAX_CHARS: int = 4000

# HTTP 200 但解析不到结果锚点时,用页面里的「无结果」标记区分「真没结果」与
# 「供应商改版」(best-effort:出现标记 → NO_RESULTS;不出现 → PARSE_ERROR)。
_DDG_NO_RESULTS_MARKERS: tuple[str, ...] = ("no-results", "No results")

_ALLOWED_URL_SCHEMES: tuple[str, ...] = ("http", "https")


def _clean_web_text(raw: object, max_len: int) -> str:
    """网页摘要是**不可信的外部文本**:压成单行、去控制字符、截断。

    与 ``nodes/storyline/_common._clean_web_title`` 同思路的能力层自持版本
    (能力层不依赖上层 nodes,不能反向 import);花括号中和留给注入侧做。
    """
    s = "".join(ch if (ch.isprintable() or ch == " ") else " " for ch in str(raw or ""))
    s = " ".join(s.split())
    return s[:max_len].strip()


# ---------------------------------------------------------------------------
# DuckDuckGo ``/html/`` 简易搜索
# ---------------------------------------------------------------------------
def _ddg_html_search(
    query: str, max_results: int = 10
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """从 DuckDuckGo html 端点取搜索结果列表(文本 + URL + 摘要)。

    抓取策略:抓 HTML 主体,搜 ``<a class="result__a" href="...">...</a>``,
    摘要取同一结果块内的 ``<a class="result__snippet">``。
    不依赖 BeautifulSoup;实在没有就退化到 urlencoded POST。

    Returns:
        ``(results, error_code)``。error_code 为 None 表示成功解析;
        ``"NETWORK_ERROR"`` = requests 缺失或请求/HTTP 异常;
        ``"PARSE_ERROR"`` = HTTP 200 但解析不到结果锚点、且页面无「无结果」
        标记(供应商改版可观察,前置化设计方案 §4.2)。
    """
    if not query.strip():
        return [], None
    if not _HAS_REQUESTS:
        return [], "NETWORK_ERROR"

    try:
        r = requests.get(
            "https://duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": DEFAULT_USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        html = r.text
    except Exception:  # noqa: BLE001
        return [], "NETWORK_ERROR"

    results: list[dict[str, Any]] = []
    snippet_budget = WEB_TOPIC_TOTAL_CONTEXT_MAX_CHARS
    i = 0
    while i < len(html) and len(results) < max_results:
        a_start = html.find('class="result__a" href="', i)
        if a_start == -1:
            break
        a_start += len('class="result__a" href="')
        a_end = html.find('"', a_start)
        if a_end == -1:
            break
        url = html[a_start:a_end]
        # next </a> ...<text>
        t_start = html.find(">", a_end) + 1
        t_end = html.find("</a>", t_start)
        text = html[t_start:t_end].strip() if t_start > 0 and t_end > 0 else ""
        # 摘要:同一结果块内的 result__snippet 锚点(在下一个 result__a 之前)
        snippet = ""
        s_start = html.find('class="result__snippet"', t_end)
        next_a = html.find('class="result__a"', t_end)
        if s_start != -1 and (next_a == -1 or s_start < next_a):
            s_text_start = html.find(">", s_start) + 1
            s_text_end = html.find("</a>", s_text_start)
            if s_text_start > 0 and s_text_end > 0:
                snippet = html[s_text_start:s_text_end].strip()
        # 解码 HTML entity(简化)
        text = (
            text.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
        )
        decoded_snippet = (
            snippet.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
        )
        url = (
            url.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )
        # DuckDuckGo 把外链包在 uddg= 重定向里
        if "uddg=" in url:
            try:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                real = qs.get("uddg", [None])[0]
                if real:
                    url = real
            except Exception:  # noqa: BLE001
                pass
        # URL 白名单:只放行 http/https(javascript: 等一律丢弃,设计方案 §4.2)
        if urllib.parse.urlparse(url).scheme.lower() not in _ALLOWED_URL_SCHEMES:
            i = t_end
            continue
        # 摘要限长:单条 + 累计预算,超预算后置空(护栏见模块常量)
        if snippet_budget > 0:
            snippet = _clean_web_text(
                decoded_snippet, min(WEB_TOPIC_SNIPPET_MAX_CHARS, snippet_budget)
            )
            snippet_budget -= len(snippet)
        else:
            snippet = ""
        results.append(
            {
                "url": url,
                "title": text,
                "snippet": snippet,
                "source": "duckduckgo_html",
            }
        )
        i = t_end

    if not results:
        if any(marker in html for marker in _DDG_NO_RESULTS_MARKERS):
            return [], None  # 页面明示无结果 → 上游按 NO_RESULTS 归类
        return [], "PARSE_ERROR"
    return results, None


# ---------------------------------------------------------------------------
# 对外主入口(可注入 provider)
# ---------------------------------------------------------------------------
def search_web_topic(
    *,
    query: str,
    max_results: int = 10,
    media_dir: Optional[Path] = None,
    provider: str = "duckduckgo_html",
    download: bool = False,
) -> dict[str, Any]:
    """Web 主题搜索。

    Args:
        query: 搜索关键字。
        max_results: 最多取多少条。
        media_dir: 可选,若指定 + ``download=True`` 则抓取 HTML 到本地。
        provider: ``"duckduckgo_html"`` / ``"stub"`` / 其它未来供应商。

    Returns:
        dict::

            {"status": "ok"|"no_results"|"failed",
             "query": str(实际提交的检索词),
             "search_web_topic": [items],
             "preview_urls": [...],
             "error_code": None|"NO_RESULTS"|"NETWORK_ERROR"|"PARSE_ERROR"|"UNKNOWN_PROVIDER",
             "message": str,
             "query_hash": sha256(query), "searched_at": UTC isoformat}

    2026-11-10 阶段2(前置化设计方案 §4.2):所有分支统一加
    ``status`` / ``query_hash`` / ``searched_at``;网络异常与解析失败从
    ``NO_RESULTS`` 中分离。``cache_hit`` 只会由节点层复用同 query_hash 缓存时
    改写(阶段3+),本函数不会返回它。
    """
    query_hash = hashlib.sha256(str(query).encode("utf-8")).hexdigest()
    searched_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")

    def _result(
        *,
        items: list[dict[str, Any]],
        error_code: Optional[str],
        message: str,
        preview_urls: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        # NO_RESULTS 是错误码域里的软态,status 归 no_results 而非 failed
        if error_code == "NO_RESULTS":
            status = "no_results"
        elif error_code:
            status = "failed"
        else:
            status = "ok" if items else "no_results"
        return {
            "status": status,
            # 2026-11-10 真机验收补漏:§4.2 产物契约要求记录实际提交的检索词
            # (审计/排障入口;此前只有 query_hash,人工无法直接读回检索内容)
            "query": str(query),
            "search_web_topic": items,
            "preview_urls": preview_urls if preview_urls is not None else [],
            "error_code": error_code,
            "message": message,
            "query_hash": query_hash,
            "searched_at": searched_at,
        }

    if provider == "stub":
        return _result(items=[], error_code=None, message="stub provider, no results")

    if provider != "duckduckgo_html":
        return _result(
            items=[],
            error_code="UNKNOWN_PROVIDER",
            message=f"unknown search provider: {provider!r}",
        )

    items, error_code = _ddg_html_search(query=query, max_results=max_results)
    if error_code:
        return _result(
            items=[],
            error_code=error_code,
            message={
                "NETWORK_ERROR": "DuckDuckGo request failed (network / HTTP error).",
                "PARSE_ERROR": (
                    "DuckDuckGo page fetched but no result anchors parsed "
                    "(provider layout change?)."
                ),
            }.get(error_code, f"search failed: {error_code}"),
        )
    if not items:
        return _result(
            items=[],
            error_code="NO_RESULTS",
            message="DuckDuckGo returned no results.",
        )

    preview_urls = [it["url"] for it in items if it.get("url")]
    saved_paths: list[str] = []
    if download and media_dir is not None and _HAS_REQUESTS:
        media_dir = Path(media_dir)
        media_dir.mkdir(parents=True, exist_ok=True)
        for idx, it in enumerate(items, start=1):
            url = it.get("url")
            if not url:
                continue
            out = media_dir / f"web_topic_{idx:03d}.html"
            try:
                r = requests.get(
                    url,
                    headers={"User-Agent": DEFAULT_USER_AGENT},
                    timeout=15,
                )
                r.raise_for_status()
                out.write_text(r.text, encoding="utf-8", errors="replace")
                saved_paths.append(str(out))
            except Exception:  # noqa: BLE001
                continue

    return _result(
        items=saved_paths or items,
        preview_urls=preview_urls,
        error_code=None,
        message="",
    )


__all__ = ["search_web_topic"]
