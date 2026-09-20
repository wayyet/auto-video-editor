import base64
import binascii
import html as html_lib
import logging
import os
import re
import time

from typing import Any, ClassVar, Dict, List, Optional, Type
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from pydantic import BaseModel

from open_storyline.nodes.core_nodes.base_node import NodeMeta, BaseNode
from open_storyline.nodes.node_schema import SearchWebTopicInput
from open_storyline.nodes.node_state import NodeState
from open_storyline.utils.register import NODE_REGISTRY

logger = logging.getLogger(__name__)

MAX_RESULTS_PER_SITE = 20
MAX_RETRIES = 2
RETRY_DELAY = 1.5
SITE_QUERY_INTERVAL = 0.8   # 相邻站点查询之间的礼貌性间隔（秒），避免触发搜索引擎风控
SNIPPET_MAX_CHARS = 160     # 摘要在给 LLM 的文摘中的截断长度
TITLE_MAX_CHARS = 80

# 桌面版 Edge UA，搜索引擎对无 UA 请求会直接拒绝或返回精简页
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@NODE_REGISTRY.register()
class SearchWebTopicNode(BaseNode):
    meta = NodeMeta(
        name="search_web_topic",
        description=(
            "Search the configured websites (default: xiaohongshu.com and douyin.com) for trending "
            "topics, popular title styles and script ideas related to a keyword, and return TEXT "
            "material (titles / snippets / links) for reference. Use this tool whenever you would "
            "otherwise suggest the user to manually search Xiaohongshu or Douyin for related topics "
            "and materials — call it directly instead of asking the user to search by themselves. "
            "The results are for script/title inspiration only; it does NOT download any media. "
            "当你想建议用户“到网上小红书或者抖音搜索相关的主题和资料”时，直接调用本工具完成站内搜索，"
            "把搜到的主题、爆款标题风格和文案要点总结给用户，作为文案与标题生成的参考。"
        ),
        node_id="search_web_topic",
        node_kind="search_web_topic",
        require_prior_kind=[],
        default_require_prior_kind=[],
        next_available_node=["generate_script"],
    )
    input_schema: ClassVar[Type[BaseModel]] = SearchWebTopicInput

    async def default_process(self, node_state: NodeState, inputs: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    async def process(self, node_state: NodeState, inputs: Dict[str, Any]) -> Dict[str, Any]:
        search_keyword = (inputs.get("search_keyword") or "").strip()
        if not search_keyword:
            node_state.node_summary.info_for_llm(
                "search_keyword is required. Please call again with a topic keyword, e.g. '重庆旅游 vlog'."
            )
            raise RuntimeError("search_web_topic requires a non-empty `search_keyword`")

        cfg = self.server_cfg.search_web
        sites: List[str] = [s.strip() for s in (inputs.get("sites") or []) if s and s.strip()]
        if not sites:
            sites = list(cfg.sites)
        results_per_site = min(
            int(inputs.get("results_per_site") or cfg.results_per_site),
            MAX_RESULTS_PER_SITE,
        )

        proxies = _resolve_search_proxies(cfg.proxy)

        all_results: List[Dict[str, Any]] = []
        for idx, site in enumerate(sites):
            if idx > 0:
                time.sleep(SITE_QUERY_INTERVAL)
            site_results = search_site_topics(
                keyword=search_keyword,
                site=site,
                engines=list(cfg.engines),
                limit=results_per_site,
                timeout=cfg.timeout,
                proxies=proxies,
            )
            if site_results:
                node_state.node_summary.info_for_user(
                    f"已从 {site} 搜索到 {len(site_results)} 条与「{search_keyword}」相关的内容"
                )
            else:
                node_state.node_summary.add_warning(
                    f"未能从 {site} 搜索到与「{search_keyword}」相关的内容（可能是网络受限或搜索引擎风控）"
                )
            all_results.extend(site_results)

        digest = build_digest(search_keyword, all_results)
        node_state.node_summary.info_for_llm(digest)

        return {"web_topics": all_results}


def _resolve_search_proxies(configured_proxy: str) -> Optional[Dict[str, str]]:
    """优先使用 config.toml 的 proxy；未配置时回退 HTTP_PROXY / HTTPS_PROXY 环境变量。"""
    proxy = (configured_proxy or "").strip()
    if not proxy:
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def search_site_topics(
    keyword: str,
    site: str,
    engines: List[str],
    limit: int,
    timeout: float,
    proxies: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    用搜索引擎的 site: 语法在单个站点内检索，按引擎优先级逐个尝试，
    直到某个引擎返回非空结果为止。
    """
    query = f"site:{site} {keyword}"
    engine_funcs = {
        "ddgs": search_ddgs,
        "bing": search_bing,
        "duckduckgo": search_duckduckgo,
    }

    for engine in engines:
        func = engine_funcs.get(engine.strip().lower())
        if func is None:
            logger.warning(f"Unknown search engine '{engine}', skipped")
            continue
        for attempt in range(MAX_RETRIES):
            try:
                raw = func(query=query, count=limit * 2, timeout=timeout, proxies=proxies)
                results = _filter_results(raw, site=site, limit=limit, engine=engine)
                if results:
                    return results
                break  # 引擎可访问但没有命中，直接换下一个引擎
            except requests.exceptions.RequestException as e:
                logger.warning(
                    f"[search_web_topic] {engine} request failed "
                    f"(attempt {attempt + 1}/{MAX_RETRIES}): {e}"
                )
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
            except Exception as e:
                logger.warning(f"[search_web_topic] {engine} parse failed: {e}")
                break
    return []


def _filter_results(
    raw: List[Dict[str, str]],
    site: str,
    limit: int,
    engine: str,
) -> List[Dict[str, Any]]:
    """只保留真正落在目标站点上的结果，并按 url 去重。"""
    results: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        url = (item.get("url") or "").strip()
        title = _clean_text(item.get("title") or "")
        snippet = _clean_text(item.get("snippet") or "")
        if not url or not title:
            continue
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if not (netloc == site or netloc.endswith("." + site)):
            continue
        if parsed.path in ("", "/") and not parsed.query:
            continue  # 站点首页不是内容页，对主题参考没有价值
        if url in seen:
            continue
        seen.add(url)
        results.append({
            "site": site,
            "title": title,
            "snippet": snippet,
            "url": url,
            "engine": engine,
        })
        if len(results) >= limit:
            break
    return results


def build_digest(keyword: str, results: List[Dict[str, Any]]) -> str:
    """把搜索结果整理成便于 LLM 直接引用的中文文摘。"""
    if not results:
        return (
            f"针对关键词「{keyword}」的站内搜索没有返回结果。"
            "可能是网络受限或搜索引擎风控，请向用户说明情况，"
            "并可建议用户在侧边栏检查网络/代理配置后重试。"
        )

    by_site: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        by_site.setdefault(r["site"], []).append(r)

    lines = [f"以下是关键词「{keyword}」的站内搜索结果，可用作主题、标题风格与文案要点的参考："]
    for site, items in by_site.items():
        lines.append(f"\n【{site}】共 {len(items)} 条：")
        for i, item in enumerate(items, 1):
            title = item["title"][:TITLE_MAX_CHARS]
            snippet = item["snippet"][:SNIPPET_MAX_CHARS]
            lines.append(f"{i}. {title}")
            if snippet:
                lines.append(f"   摘要: {snippet}")
            lines.append(f"   链接: {item['url']}")
    lines.append(
        "\n请基于以上资料总结出主题方向、爆款标题的风格特点和文案要点，供后续文案/标题生成参考。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 搜索引擎实现
# ---------------------------------------------------------------------------

def search_ddgs(
    query: str,
    count: int,
    timeout: float,
    proxies: Optional[Dict[str, str]] = None,
) -> List[Dict[str, str]]:
    """
    首选引擎：ddgs 库（自带浏览器指纹伪装与多后端轮换，实测国内直连可用）。
    裸 requests 请求 Bing/DDG 会被反爬挑战页拦截，ddgs 是目前唯一稳定拿到
    小红书/抖音站内结果的方式。
    """
    from ddgs import DDGS  # 延迟导入，缺依赖时不影响其他引擎

    proxy = proxies.get("https") if proxies else None
    with DDGS(proxy=proxy, timeout=timeout) as d:
        raw = d.text(query, region="cn-zh", max_results=min(count, 30))
    return [
        {
            "title": item.get("title") or "",
            "snippet": item.get("body") or "",
            "url": item.get("href") or "",
        }
        for item in (raw or [])
    ]


def search_bing(
    query: str,
    count: int,
    timeout: float,
    proxies: Optional[Dict[str, str]] = None,
) -> List[Dict[str, str]]:
    """Bing 网页搜索（国内可直连）。解析 b_algo 结果块。"""
    url = (
        "https://www.bing.com/search"
        f"?q={quote_plus(query)}&count={min(count, 30)}&mkt=zh-CN&setlang=zh-hans"
    )
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout, proxies=proxies)
    r.raise_for_status()
    page = r.text

    results: List[Dict[str, str]] = []
    for block_match in re.finditer(r'<li class="b_algo\b.*?</li>', page, re.S):
        block = block_match.group(0)
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        link = _resolve_bing_url(html_lib.unescape(m.group(1)))
        title = m.group(2)
        sm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = sm.group(1) if sm else ""
        results.append({"title": title, "snippet": snippet, "url": link})
    return results


def _resolve_bing_url(link: str) -> str:
    """
    Bing 的结果链接常包成跳转形式 https://www.bing.com/ck/a?...&u=a1<base64url>，
    真实地址在 u 参数里（去掉前缀 a1 后做 base64url 解码）。
    """
    try:
        parsed = urlparse(link)
        if "bing.com" not in parsed.netloc or not parsed.path.startswith("/ck/"):
            return link
        u_values = parse_qs(parsed.query).get("u")
        if not u_values:
            return link
        payload = u_values[0]
        if payload.startswith("a1"):
            payload = payload[2:]
        padded = payload + "=" * (-len(payload) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return link


def search_duckduckgo(
    query: str,
    count: int,
    timeout: float,
    proxies: Optional[Dict[str, str]] = None,
) -> List[Dict[str, str]]:
    """
    DuckDuckGo HTML 版搜索（国内直连不通，需在 [search_web] 配置 proxy 才可用，
    作为 Bing 失效时的回退引擎）。
    """
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout, proxies=proxies)
    r.raise_for_status()
    page = r.text

    titles = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S
    )
    snippets = re.findall(
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', page, re.S
    )

    results: List[Dict[str, str]] = []
    for i, (href, title) in enumerate(titles[:count]):
        link = _resolve_ddg_url(html_lib.unescape(href))
        snippet = snippets[i] if i < len(snippets) else ""
        results.append({"title": title, "snippet": snippet, "url": link})
    return results


def _resolve_ddg_url(link: str) -> str:
    """DuckDuckGo 结果是 //duckduckgo.com/l/?uddg=<url 编码> 的跳转链接，取出真实地址。"""
    try:
        if link.startswith("//"):
            link = "https:" + link
        parsed = urlparse(link)
        if "duckduckgo.com" in parsed.netloc:
            uddg = parse_qs(parsed.query).get("uddg")
            if uddg:
                return unquote(uddg[0])
        return link
    except ValueError:
        return link


def _clean_text(raw: str) -> str:
    """去掉 HTML 标签、还原实体、压缩空白。"""
    text = re.sub(r"<[^>]+>", "", raw)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
