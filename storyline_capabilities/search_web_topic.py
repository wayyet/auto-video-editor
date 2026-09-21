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
# DuckDuckGo ``/html/`` 简易搜索
# ---------------------------------------------------------------------------
def _ddg_html_search(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """从 DuckDuckGo html 端点取搜索结果列表(文本 + URL)。

    抓取策略:抓 HTML 主体,搜 ``<a class="result__a" href="...">...</a>``。
    不依赖 BeautifulSoup;实在没有就退化到 urlencoded POST。
    """
    if not query.strip():
        return []
    if not _HAS_REQUESTS:
        return []

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
        return []

    results: list[dict[str, Any]] = []
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
        # 解码 HTML entity(简化)
        text = (
            text.replace("&amp;", "&")
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
        results.append(
            {"url": url, "title": text, "source": "duckduckgo_html"}
        )
        i = t_end
    return results


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
        dict ``{"search_web_topic": [items], "preview_urls": [...], "error_code": None|"NO_RESULTS"|"UNKNOWN_PROVIDER"}``
    """
    if provider == "stub":
        return {
            "search_web_topic": [],
            "preview_urls": [],
            "error_code": None,
            "message": "stub provider, no results",
        }

    if provider != "duckduckgo_html":
        return {
            "search_web_topic": [],
            "preview_urls": [],
            "error_code": "UNKNOWN_PROVIDER",
            "message": f"unknown search provider: {provider!r}",
        }

    items = _ddg_html_search(query=query, max_results=max_results)
    if not items:
        return {
            "search_web_topic": [],
            "preview_urls": [],
            "error_code": "NO_RESULTS",
            "message": "DuckDuckGo returned no results.",
        }

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

    return {
        "search_web_topic": saved_paths or items,
        "preview_urls": preview_urls,
        "error_code": None,
        "message": "",
    }


__all__ = ["search_web_topic"]
