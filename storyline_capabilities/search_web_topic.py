"""本地化 search_web_topic(plan_v4 §5 阶段 1 / A 类;2026-11-19 审查修订版扩展)。

策略:
- Provider 可插拔:``mediacrawler``(2026-11-19 用户拍板;2026-11-20 审查修订版
  阶段3接线 —— 子进程调本地 MediaCrawler 检索社交平台笔记,失败或有效 0 条
  **自动回退 bing_html**,artifact 记 requested_provider/provider 实际产出者/
  fallback 语义,见 §6.2)、``duckduckgo_html``(存量)、``google_html``
  (2026-11-19 按用户拍板接入,走本地 SOCKS5;检测到挑战/人机验证页即停,
  **不做任何规避动作**)、``stub``。**生产默认引擎由 config 决定(当前
  bing_html,MC 待默认切换门禁);能力层不读配置,provider 由节点层传入。**
- google_html **强制 SOCKS5 代理**(``socks5h://``,域名解析走代理端防本地 DNS
  污染);代理缺失/格式不对返回 ``PROXY_ERROR``,绝不静默回退直连。
- 错误码体系(审查修订版 §4.4 表):``NO_RESULTS`` / ``NETWORK_ERROR`` /
  ``HTTP_ERROR`` / ``PROXY_ERROR`` / ``PROVIDER_BLOCKED`` / ``PARSE_ERROR`` /
  ``IRRELEVANT_RESULTS``(2026-11-19 真机事故:解析成功但与检索词零相关的
  降级 SERP,不再算成功)/ ``UNKNOWN_PROVIDER``。
- 诊断:摘要随产物 ``diagnostics`` 字段返回(不含代理凭据/Cookie);仅当调用方
  显式传 ``debug_dir`` 时才把响应片段落成受限文本文件(≤64KiB、.txt、原子替换,
  审查修订版 §5.5)。

设计约束(plan §5 阶段 1 第 1~3 行):本模块不依赖 FireRed NodeState / 任何
私有框架;可作为独立 CLI/Script 跑。
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import re
import time
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

try:
    import requests  # type: ignore[import-untyped]
    from requests import exceptions as _requests_exceptions  # noqa: F401
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False
    _requests_exceptions = None  # type: ignore[assignment]

# MediaCrawler 子进程适配器(2026-11-20 审查修订版阶段3接线;无环依赖:
# client 不反向 import 本包)。
from storyline_capabilities import mediacrawler_client as _mc_client
from storyline_capabilities import reach_client as _reach_client


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

# ---------------------------------------------------------------------------
# 2026-11-19 审查修订版:产物 schema v2 / request_hash / 解析器版本 / 诊断
# ---------------------------------------------------------------------------
# 产物 schema 版本:v2 = +schema_version/engine/provider/request_hash/diagnostics;
# 旧字段全保留,读侧一律 .get 兼容(§4.4)。
ARTIFACT_SCHEMA_VERSION: int = 2

# 每供应商解析器版本与语言参数(request_hash 组成部分,§4.5)。
# 2026-11-19 补充:google_html 纯 HTTP 路线被实测关闭(无 JS 桩页,enablejs 重试
# 也要求 JS)、DDG 挑战 VPS 出口 → 用户拍板默认引擎改 bing_html(Bing 结果页服务端
# 渲染,隧道/直连均实测返回 10 条真实结果);google_html 代码保留可随时切回。
_PARSER_VERSIONS: dict[str, str] = {
    "mediacrawler": "mediacrawler_v1",  # 基础键;实际 hash 用 mediacrawler:{platform}:{revision}
    "duckduckgo_html": "ddg_html_v1",
    "google_html": "google_html_v1",
    "bing_html": "bing_html_v1",
    "stub": "stub_v1",
    # 基础键;实际 hash 用 reach_mcp:{sources}:{per}:{days}:{chars}:{adapter}
    # (2026-11-20 第九期:reach-mcp 英文线,hash provider 串见 reach_client)
    "reach_mcp": "reach_mcp_v1",
}
_PROVIDER_LOCALES: dict[str, str] = {
    "mediacrawler": "",
    "duckduckgo_html": "",
    "google_html": "zh-CN",
    "bing_html": "zh-CN",
    "stub": "",
    "reach_mcp": "",
}
_PROVIDER_ENGINE_LABELS: dict[str, str] = {
    "mediacrawler": "MediaCrawler",
    "duckduckgo_html": "DuckDuckGo",
    "google_html": "Google",
    "bing_html": "Bing",
    "stub": "stub",
    "reach_mcp": "ReachMCP(海外检索)",
}

# mediacrawler 条目的 source 值(实际数据来源标识,进 script.json references)。
_MC_ITEM_SOURCE = "mediacrawler"

# 平台域名白名单(2026-11-21 ks/bili 准入修订版 §4.3:不让任意外部 URL 绕过
# 域名校验)。MC 各平台 store **预构建**标准 URL,域名确定可枚举(xhs=百科/探索页、
# dy=视频页、ks=short-video、bili=av 视频页);hostname 等于或以允许域结尾才接受。
# 未登记域名的平台(未来扩展)退化为 scheme+hostname 校验。
_MC_ALLOWED_URL_DOMAINS: dict[str, tuple[str, ...]] = {
    "xhs": ("xiaohongshu.com",),
    "dy": ("douyin.com",),
    "ks": ("kuaishou.com",),
    "bili": ("bilibili.com",),
}
# reach_mcp 条目 source 缺失时的兜底值(真实来源名在 reach_source 字段)。
_REACH_ITEM_SOURCE = "reach_mcp"

# 调试片段落盘上限(§5.5:≤64KiB)。
WEB_TOPIC_DEBUG_MAX_BYTES: int = 64 * 1024

# Google 挑战/同意页特征(§5.4 识别顺序:挑战页优先于一切解析;命中即
# PROVIDER_BLOCKED 并停止,不做验证码处理/出口轮换等任何规避动作)。
_GOOGLE_BLOCKED_MARKERS: tuple[str, ...] = (
    "unusual traffic",
    "异常流量",
    "g-recaptcha",
    "recaptcha/api",
    "/sorry/",
)
_GOOGLE_CONSENT_HOST = "consent.google.com"
_GOOGLE_NO_RESULTS_MARKERS: tuple[str, ...] = (
    "did not match any documents",
    "未找到和您查询",
    "没有找到和您查询",
    "不包含某些字词",
)
# 摘要块 class 提示(有限回退:Google 版面实验会换 class,命中其一即采,§5.4-4)。
_GOOGLE_SNIPPET_CLASS_HINTS: tuple[str, ...] = (
    "VwiC3b",
    "b_lineclamp",
    "s3v9rd",
    "IsZvec",
)

# ---------------------------------------------------------------------------
# Bing(2026-11-19:默认引擎 bing_html;结果块 b_algo 服务端渲染)
# ---------------------------------------------------------------------------
_BING_SNIPPET_CLASS_HINTS: tuple[str, ...] = (
    "b_caption",
    "b_algoSlug",
    "b_lineclamp",
)
# 零结果时 Bing 的「无结果」特征(b_no 空态容器/文案;只在解析出零条时才查询)
_BING_NO_RESULTS_MARKERS: tuple[str, ...] = (
    "b_no",
    "There are no results for",
    "没有找到",
    "没有与此相关的结果",
)
_BING_BLOCKED_MARKERS: tuple[str, ...] = (
    "captcha",
    "unusual traffic",
    "异常流量",
)


def _clean_web_text(raw: object, max_len: int) -> str:
    """网页摘要是**不可信的外部文本**:压成单行、去控制字符、截断。

    与 ``nodes/storyline/_common._clean_web_title`` 同思路的能力层自持版本
    (能力层不依赖上层 nodes,不能反向 import);花括号中和留给注入侧做。
    """
    s = "".join(ch if (ch.isprintable() or ch == " ") else " " for ch in str(raw or ""))
    s = " ".join(s.split())
    return s[:max_len].strip()


# ---------------------------------------------------------------------------
# 检索词指令括号剥离 + 结果相关性守卫(2026-11-19 真机事故修订)
# ---------------------------------------------------------------------------
# 事故:console-061cd2d39245 经默认 SOCKS5(Azure 新加坡数据中心出口)查询
# 「海上清真寺旅游攻略…」,Bing 返回整页无关降级 SERP(安徽大学教务页),
# 解析层照单全收 status=ok → 垃圾引用注入文案。同请求事后重放结果正常,
# 判定为搜索引擎对数据中心出口的间歇性反爬降级。两道防线:
# ①检索词不携带指令性括号段(「(勿写平台名)」是给文案模型的元指令);
# ②解析结果与检索词零相关 → 不算成功(IRRELEVANT_RESULTS,可观察失败)。
#
# 指令动词表(括号内出现即整段剥离;刻意保守,避免误删正常检索词):
_INSTRUCTION_PAREN_PATTERN = re.compile(
    r"[（(][^（）()]*?(?:勿|不要|不得|禁止|无需|不用|别|避免|注意|务必|请)"
    r"[^（）()]*?[）)]"
)
# 检索词切段分隔符(构造侧用「、」拼接;引擎侧常见标点一律切开)。
_QUERY_SEGMENT_SPLIT_PATTERN = re.compile(r"[、，。,.;;!!??\s()（）/·]+")
# CJK 段按连续汉字串取二字滑窗(避免跨语种噪声 bigram,如「Vlog」切出「lo」)。
_CJK_RUN_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,}")
_CJK_CHAR_PATTERN = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]+")

# 相关性守卫阈值:命中检索词 token 的结果数低于该值即判不相关;结果数不足
# 该值时须全部命中。无可判定 token(如单字符 query)→ 守卫跳过不拦截。
WEB_TOPIC_RELEVANCE_MIN_MATCHED: int = 2


def strip_instruction_parentheticals(query: object) -> str:
    """剥离「(勿写平台名)」「（不要出现品牌名）」这类写给文案模型的指令括号段。

    指令文本进入搜索引擎只会污染查询;括号外的真实检索诉求原样保留。
    半角/全角括号都识别;无括号或括号内不含指令动词时原样返回。
    """
    return _INSTRUCTION_PAREN_PATTERN.sub("", str(query or ""))


def _relevance_tokens(query: str) -> list[str]:
    """检索词 → 判相关用的 token 集:CJK 段取二字滑窗,拉丁段取含字母的整词。

    指令括号段先剥离;切不出任何 token(如 query="q")返回空列表,调用方
    据此跳过守卫(无法判定时不拦截,避免误伤正常结果)。
    """
    tokens: list[str] = []
    segments = _QUERY_SEGMENT_SPLIT_PATTERN.split(
        strip_instruction_parentheticals(query)
    )
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        if _CJK_CHAR_PATTERN.search(seg):
            for run in _CJK_RUN_PATTERN.findall(seg):
                tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
        for word in _LATIN_WORD_TOKEN_PATTERN.findall(seg):
            if len(word) >= 3:
                tokens.append(word.lower())
    return list(dict.fromkeys(tokens))


def _count_relevant_results(query: str, results: list[dict[str, Any]]) -> int:
    """统计标题/摘要命中任一检索词 token 的结果条数。

    无可判定 token → 返回 ``len(results)``(守卫视为全部命中,即跳过)。
    """
    tokens = _relevance_tokens(query)
    if not tokens:
        return len(results)
    matched = 0
    for item in results:
        haystack = (
            str(item.get("title") or "") + "\n" + str(item.get("snippet") or "")
        ).lower()
        if any(token in haystack for token in tokens):
            matched += 1
    return matched


def _parser_version_for(provider: str) -> str:
    """provider → 解析器版本(2026-11-20 审查 P2-01)。

    ``mediacrawler:{platform}:{revision}`` 动态键解析为
    ``mc_{platform}_v{adapter}``:平台与适配器语义进 hash;上游 revision 编在
    provider 串里,MC 升级(或节点层传的 revision 变化)必得不同 hash。
    """
    if provider.startswith("mediacrawler:"):
        parts = provider.split(":")
        # 2026-11-21 审查修订版 §3.4:provider 串携带完整平台链(xhs>dy),
        # 解析器版本语义跟随主平台(链首)。
        chain = parts[1] if len(parts) > 1 else "xhs"
        platform = chain.split(">")[0] if chain else "xhs"
        return f"mc_{platform}_v{_mc_client.MEDIACRAWLER_ADAPTER_VERSION}"
    if provider.startswith("reach_mcp:"):
        # 动态键解析:适配器版本编入 hash(2026-11-20 审查 P2-01)——
        # reach 适配层映射/解包行为变化(适配器升版)必得不同 hash。
        return f"reach_mcp_v{_reach_client.REACH_ADAPTER_VERSION}"
    return _PARSER_VERSIONS.get(provider, "unknown")


def _locale_for(provider: str) -> str:
    if provider.startswith("mediacrawler:") or provider.startswith("reach_mcp:"):
        return ""
    return _PROVIDER_LOCALES.get(provider, "")


def mediacrawler_request_provider(
    platform_chain: "str | list[str] | tuple[str, ...]", mc_revision: str
) -> str:
    """mediacrawler 引擎的 request_hash provider 串(审查 P2-01/P2-04;
    2026-11-21 审查修订版 §3.4:平台链有序编入)。

    **完整有序平台链**(主平台 + 回退平台,">" 连接)与上游 revision 一起编入
    → 链成员/顺序/revision 任一变化必得不同 hash(缓存隔离)。节点层与能力层
    用同一函数计算,两侧 hash 一致才命中缓存;回退成功保留该 hash(QA 重试幂等),
    但"首选策略"指的是完整链而非第一个平台。接受 str(= 单平台链,兼容旧调用)。
    """
    if isinstance(platform_chain, str):
        chain_items: list[str] = [platform_chain]
    else:
        chain_items = [str(p) for p in platform_chain]
    chain = ">".join(chain_items)
    return f"mediacrawler:{chain}:{mc_revision or 'unknown'}"


def build_request_hash(*, query: str, provider: str, max_results: int) -> str:
    """缓存键(审查修订版 §4.5):provider+query+参数+语言+解析器+schema 版本。

    只含请求语义,不含代理地址与任何凭据;同 query 换 Provider 必得不同 hash
    (缓存必须比较它,防止"切了引擎仍命中旧 DDG 失败缓存")。
    """
    payload = "\x1f".join(
        [
            "v2",
            provider,
            str(query),
            str(int(max_results)),
            _locale_for(provider),
            _parser_version_for(provider),
            str(ARTIFACT_SCHEMA_VERSION),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _looks_blocked(text: str, markers: tuple[str, ...]) -> bool:
    """页面/响应体是否命中挑战/拒绝特征(§5.4-3)。"""
    low = str(text or "").lower()
    return any(marker in low for marker in markers)


def _write_debug_dump(
    debug_dir: Optional[Path], diagnostics: dict[str, Any], body: str
) -> None:
    """受限诊断落盘(审查修订版 §5.5)。

    仅当调用方显式传 ``debug_dir`` 才写;``.txt`` 纯文本(不给 Web 服务当
    HTML 展示)、≤64KiB、临时文件+原子替换;任何落盘失败静默吞掉——诊断文件
    绝不能改变检索/工作流状态,也不能覆盖原始错误。
    """
    if debug_dir is None:
        return
    try:
        debug_dir = Path(debug_dir)
        debug_dir.mkdir(parents=True, exist_ok=True)
        head = {
            "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(
                timespec="seconds"
            ),
            "diagnostics": diagnostics,
        }
        payload = (
            json.dumps(head, ensure_ascii=False, default=str)
            + "\n\n"
            + str(body or "")
        )[:WEB_TOPIC_DEBUG_MAX_BYTES]
        tmp = debug_dir / "search_web_topic_debug.txt.tmp"
        tmp.write_text(payload, encoding="utf-8", errors="replace")
        tmp.replace(debug_dir / "search_web_topic_debug.txt")
    except Exception:  # noqa: BLE001
        pass


def _normalize_google_result_url(raw: object) -> Optional[str]:
    """结果链接归一化与安全校验(审查修订版 §5.4-5/P1-04)。

    - 展开 Google 内部包装跳转 ``/url?q=<外链>``(仅识别 q/url 两个参数名);
    - 只放行 http/https 外链;
    - 丢弃 Google 系导航地址(g.co/goo.gl/googleusercontent、google 域名的
      首页/搜索/图片/地图/偏好设置等站内路径)。
    """
    url = str(raw or "").strip()
    if not url:
        return None
    if url.startswith("/url?"):
        try:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
        except ValueError:
            return None
        real = (qs.get("q") or qs.get("url") or [None])[0]
        if not real:
            return None
        url = str(real)
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/"):
        return None  # 站内相对路径 = 导航,不是外链
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host in ("g.co", "goo.gl") or host.endswith(
        (".googleusercontent.com", ".g.doubleclick.net")
    ):
        return None
    if host == "google.com" or host.endswith(".google.com"):
        path = (parsed.path or "/").lower()
        internal_paths = (
            "/",
            "/search",
            "/url",
            "/imgres",
            "/maps",
            "/preferences",
            "/setprefs",
            "/advanced_search",
            "/webhp",
            "/travel",
        )
        if path in internal_paths:
            return None
    return url


class _SerpResultParser(HTMLParser):
    """通用 SERP 结构化解析(Google/Bing 共用;审查修订版 §5.4-4:html.parser
    + 有限回退)。

    提取两类信号,退出后由调用方按文档序配对:
    - 结果锚点:标题标签(h2/h3)与 ``<a href>`` **同时嵌套开放**时捕获标题
      (Google: a>h3;Bing: h2>a,两种嵌套方向都支持)→ (seq, href, title);
    - 摘要块:div/p class 命中 hints 之一 → 只捕获最外层匹配容器 → (seq, text)。
    不把嵌套 HTML 当字符串模板;实体解码交给 ``convert_charrefs`` 与属性值
    自动 unescape。
    """

    def __init__(self, *, snippet_hints: tuple[str, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self._snippet_hints = snippet_hints
        self._a_stack: list[Optional[str]] = []
        self._heading_depth = 0
        self._title_href: Optional[str] = None
        self._title_buf: list[str] = []
        self._in_snippet = False
        self._snippet_depth = 0
        self._snippet_buf: list[str] = []
        self.seq = 0
        self.anchors: list[tuple[int, str, str]] = []  # (seq, href, title)
        self.snippets: list[tuple[int, str]] = []  # (seq, text)

    def _nearest_href(self) -> Optional[str]:
        return next((h for h in reversed(self._a_stack) if h), None)

    def _maybe_start_title(self, href: Optional[str]) -> None:
        # 标题标签与锚点同时开放 → 开始捕获标题(google: a>h3;bing: h2>a)
        if href and self._heading_depth > 0 and self._title_href is None:
            self._title_href = href
            self._title_buf = []

    def _flush_title(self) -> None:
        if self._title_href is None:
            return
        title = " ".join("".join(self._title_buf).split())
        if title:
            self.anchors.append((self.seq, self._title_href, title))
        self._title_href = None
        self._title_buf = []

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "a":
            self._a_stack.append(attrs_d.get("href"))
            self.seq += 1
            self._maybe_start_title(attrs_d.get("href"))
        elif tag in ("h2", "h3"):
            self._heading_depth += 1
            self._maybe_start_title(self._nearest_href())
        elif tag in ("div", "p"):
            if self._in_snippet:
                self._snippet_depth += 1
            else:
                cls = attrs_d.get("class") or ""
                if any(hint in cls for hint in self._snippet_hints):
                    self._in_snippet = True
                    self._snippet_depth = 1
                    self._snippet_buf = []

    def handle_endtag(self, tag):
        if tag == "a":
            if self._title_href is not None and len(self._a_stack) <= 1:
                self._flush_title()  # bing(h2>a):标题锚点先闭合
            if self._a_stack:
                self._a_stack.pop()
            if self._title_href is not None and self._nearest_href() != self._title_href:
                self._flush_title()  # google(a>h3):持有标题的锚点已闭合时兜底刷新
        elif tag in ("h2", "h3"):
            if self._title_href is not None:
                self._flush_title()  # google(a>h3):标题标签先闭合
            self._heading_depth = max(0, self._heading_depth - 1)
        elif tag in ("div", "p"):
            if self._in_snippet:
                self._snippet_depth -= 1
                if self._snippet_depth <= 0:
                    text = " ".join("".join(self._snippet_buf).split())
                    if text:
                        self.seq += 1
                        self.snippets.append((self.seq, text))
                    self._in_snippet = False
                    self._snippet_buf = []

    def handle_data(self, data):
        if self._title_href is not None:
            self._title_buf.append(data)
        elif self._in_snippet:
            self._snippet_buf.append(data)


# ---------------------------------------------------------------------------
# DuckDuckGo ``/html/`` 简易搜索(存量实现,回滚通道;行为不变)
# ---------------------------------------------------------------------------
def _ddg_html_search(
    query: str,
    max_results: int = 10,
    proxies: Optional[dict[str, str]] = None,
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
            **({"proxies": proxies} if proxies else {}),
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
    # 相关性守卫(2026-11-19 真机事故):整页无关降级 SERP 不算检索成功。
    if _count_relevant_results(query, results) < min(
        WEB_TOPIC_RELEVANCE_MIN_MATCHED, len(results)
    ):
        return [], "IRRELEVANT_RESULTS"
    return results, None


# ---------------------------------------------------------------------------
# Google SERP(2026-11-19 审查修订版 §5:授权 Gate 已由用户拍板通过)
# ---------------------------------------------------------------------------
def _google_html_search(
    *,
    query: str,
    max_results: int,
    proxy_url: Optional[str],
    debug_dir: Optional[Path],
) -> tuple[list[dict[str, Any]], Optional[str], dict[str, Any]]:
    """从 Google SERP 取搜索结果(强制 SOCKS5;挑战页即停,零规避)。

    Returns:
        ``(results, error_code, diagnostics)``。error_code ∈ {None,
        PROXY_ERROR, NETWORK_ERROR, HTTP_ERROR, PROVIDER_BLOCKED, NO_RESULTS,
        PARSE_ERROR, IRRELEVANT_RESULTS};diagnostics 不含代理凭据/Cookie(§4.4)。
    """
    diagnostics: dict[str, Any] = {
        "http_status": None,
        "final_host": None,
        "proxy_enabled": bool(proxy_url),
        "parser_version": _PARSER_VERSIONS["google_html"],
        "response_content_type": None,
        "response_bytes": None,
    }

    # §5.1-2:google_html 必须显式给 socks5h:// 代理;否则 PROXY_ERROR,
    # 不发请求、绝不静默回退直连。
    if not proxy_url or not str(proxy_url).strip():
        diagnostics["proxy_enabled"] = False
        return [], "PROXY_ERROR", diagnostics
    proxy = str(proxy_url).strip()
    try:
        proxy_scheme = urllib.parse.urlparse(proxy).scheme.lower()
    except ValueError:
        proxy_scheme = ""
    if proxy_scheme != "socks5h":
        return [], "PROXY_ERROR", diagnostics
    if not _HAS_REQUESTS:
        return [], "NETWORK_ERROR", diagnostics
    proxies = {"http": proxy, "https": proxy}
    diagnostics["proxy_enabled"] = True

    try:
        r = requests.get(
            "https://www.google.com/search",
            params={"q": query, "num": 10, "hl": "zh-CN"},
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            proxies=proxies,
            timeout=(5, 15),
        )
        # HTTP 4xx/5xx → HTTPError(下方 except 分类为 HTTP_ERROR/PROVIDER_BLOCKED;
        # 202/2xx 不抛,挑战页由特征识别兜底 —— 审查修订版 §5.4-1)
        r.raise_for_status()
        diagnostics["http_status"] = getattr(r, "status_code", None)
        try:
            diagnostics["final_host"] = urllib.parse.urlparse(
                getattr(r, "url", "") or ""
            ).hostname
        except ValueError:
            pass
        try:
            diagnostics["response_content_type"] = r.headers.get("content-type")
        except Exception:  # noqa: BLE001
            pass
        html = r.text
        diagnostics["response_bytes"] = len(html.encode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        if _requests_exceptions is not None and isinstance(
            e, _requests_exceptions.HTTPError
        ):
            # HTTPError(4xx/5xx):响应体含挑战特征 → PROVIDER_BLOCKED,否则 HTTP_ERROR
            resp = getattr(e, "response", None)
            resp_text = ""
            if resp is not None:
                try:
                    resp_text = resp.text or ""
                    diagnostics["http_status"] = resp.status_code
                except Exception:  # noqa: BLE001
                    pass
            code = (
                "PROVIDER_BLOCKED"
                if _looks_blocked(resp_text, _GOOGLE_BLOCKED_MARKERS)
                else "HTTP_ERROR"
            )
            _write_debug_dump(debug_dir, diagnostics, resp_text)
            return [], code, diagnostics
        if _requests_exceptions is not None and isinstance(
            e,
            (
                _requests_exceptions.ProxyError,
                _requests_exceptions.InvalidSchema,
                _requests_exceptions.MissingSchema,
            ),
        ):
            # 代理层错误(SOCKS 依赖缺失/代理不可达/scheme 未知)→ PROXY_ERROR,
            # 绝不回退直连(§5.3)。
            _write_debug_dump(debug_dir, diagnostics, "")
            return [], "PROXY_ERROR", diagnostics
        _write_debug_dump(debug_dir, diagnostics, "")
        return [], "NETWORK_ERROR", diagnostics

    # 挑战/同意页识别优先于一切解析(§5.4-3):命中即停,本轮结束不重试。
    if diagnostics.get("final_host") == _GOOGLE_CONSENT_HOST or _looks_blocked(
        html, _GOOGLE_BLOCKED_MARKERS
    ):
        _write_debug_dump(debug_dir, diagnostics, html)
        return [], "PROVIDER_BLOCKED", diagnostics

    parser = _SerpResultParser(snippet_hints=_GOOGLE_SNIPPET_CLASS_HINTS)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001
        pass

    # 配对:每个结果锚点取文档序其后第一条未消费摘要(§5.4-4 有限回退)。
    results: list[dict[str, Any]] = []
    snippet_budget = WEB_TOPIC_TOTAL_CONTEXT_MAX_CHARS
    seen_urls: set[str] = set()
    snippets = parser.snippets
    scan_from = 0
    for a_seq, href, title in parser.anchors:
        if len(results) >= max_results:
            break
        url = _normalize_google_result_url(href)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        snippet = ""
        si = scan_from
        while si < len(snippets) and snippets[si][0] <= a_seq:
            si += 1
        if si < len(snippets):
            raw_snip = snippets[si][1]
            scan_from = si + 1
            if snippet_budget > 0:
                snippet = _clean_web_text(
                    raw_snip, min(WEB_TOPIC_SNIPPET_MAX_CHARS, snippet_budget)
                )
                snippet_budget -= len(snippet)
        results.append(
            {
                "url": url,
                "title": _clean_web_text(title, 300),
                "snippet": snippet,
                "source": "google_html",
            }
        )

    if not results:
        low = html.lower()
        if any(marker in low for marker in _GOOGLE_NO_RESULTS_MARKERS):
            return [], "NO_RESULTS", diagnostics
        _write_debug_dump(debug_dir, diagnostics, html)
        return [], "PARSE_ERROR", diagnostics
    # 相关性守卫(2026-11-19 真机事故):解析成功但与检索词零相关的降级 SERP
    # 不算成功,落盘取证并返回 IRRELEVANT_RESULTS(与 PARSE_ERROR 同为可观察失败)。
    matched = _count_relevant_results(query, results)
    diagnostics["relevance_matched"] = matched
    diagnostics["relevance_total"] = len(results)
    if matched < min(WEB_TOPIC_RELEVANCE_MIN_MATCHED, len(results)):
        _write_debug_dump(debug_dir, diagnostics, html)
        return [], "IRRELEVANT_RESULTS", diagnostics
    return results, None, diagnostics


# ---------------------------------------------------------------------------
# Bing SERP(2026-11-19:默认引擎 bing_html;b_algo 服务端渲染,无需 JS)
# ---------------------------------------------------------------------------
def _normalize_bing_result_url(raw: object) -> Optional[str]:
    """结果链接归一化与安全校验(P1-04/§5.4-5)。

    - 展开 Bing 加密跳转 ``bing.com/ck/a?...&u=a1<urlsafe_base64>``;
    - 只放行 http/https 外链;
    - 丢弃 Bing 站内导航路径(首页/搜索/图片/视频/地图等)。
    """
    url = str(raw or "").strip()
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "/").lower()
    if host in ("bing.com", "www.bing.com", "cn.bing.com") and path.startswith("/ck/"):
        qs = urllib.parse.parse_qs(parsed.query)
        u = (qs.get("u") or [None])[0]
        if not u:
            return None
        b64 = u[2:] if u.startswith("a1") else u
        try:
            b64 += "=" * (-len(b64) % 4)
            url = base64.urlsafe_b64decode(b64.encode("ascii")).decode(
                "utf-8", "replace"
            )
        except Exception:  # noqa: BLE001
            return None
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return None
    if url.startswith("//"):
        url = "https:" + url
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return None
    elif url.startswith("/"):
        return None  # 站内相对路径 = 导航,不是外链
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host == "bing.com" or host.endswith(".bing.com"):
        bpath = (parsed.path or "/").lower()
        bing_internal_paths = (
            "/",
            "/search",
            "/ck/",
            "/images",
            "/videos",
            "/news",
            "/maps",
            "/academic",
            "/translator",
            "/shop",
        )
        if bpath in bing_internal_paths:
            return None
    return url


def _bing_html_search(
    *,
    query: str,
    max_results: int,
    proxy_url: Optional[str],
    debug_dir: Optional[Path],
) -> tuple[list[dict[str, Any]], Optional[str], dict[str, Any]]:
    """从 Bing SERP 取搜索结果(b_algo 服务端渲染;代理可选,直连国内可达)。

    Returns:
        ``(results, error_code, diagnostics)``。错误码域与 google_html 一致
        (PROXY_ERROR/NETWORK_ERROR/HTTP_ERROR/PROVIDER_BLOCKED/NO_RESULTS/
        PARSE_ERROR/IRRELEVANT_RESULTS);Bing 无 proxy 时直连(国内可达),
        有 proxy 时经代理出口。
    """
    diagnostics: dict[str, Any] = {
        "http_status": None,
        "final_host": None,
        "proxy_enabled": False,
        "parser_version": _PARSER_VERSIONS["bing_html"],
        "response_content_type": None,
        "response_bytes": None,
    }
    if not _HAS_REQUESTS:
        return [], "NETWORK_ERROR", diagnostics
    proxies: Optional[dict[str, str]] = None
    if proxy_url and str(proxy_url).strip():
        proxies = {"http": str(proxy_url).strip(), "https": str(proxy_url).strip()}
        diagnostics["proxy_enabled"] = True

    try:
        r = requests.get(
            "https://www.bing.com/search",
            params={"q": query, "mkt": "zh-CN"},
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            **({"proxies": proxies} if proxies else {}),
            timeout=(5, 15),
        )
        # HTTP 4xx/5xx → HTTPError(下方分类 HTTP_ERROR/PROVIDER_BLOCKED)
        r.raise_for_status()
        diagnostics["http_status"] = getattr(r, "status_code", None)
        try:
            diagnostics["final_host"] = urllib.parse.urlparse(
                getattr(r, "url", "") or ""
            ).hostname
        except ValueError:
            pass
        try:
            diagnostics["response_content_type"] = r.headers.get("content-type")
        except Exception:  # noqa: BLE001
            pass
        html = r.text
        diagnostics["response_bytes"] = len(html.encode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        if _requests_exceptions is not None and isinstance(
            e, _requests_exceptions.HTTPError
        ):
            resp = getattr(e, "response", None)
            resp_text = ""
            if resp is not None:
                try:
                    resp_text = resp.text or ""
                    diagnostics["http_status"] = resp.status_code
                except Exception:  # noqa: BLE001
                    pass
            code = (
                "PROVIDER_BLOCKED"
                if _looks_blocked(resp_text, _BING_BLOCKED_MARKERS)
                else "HTTP_ERROR"
            )
            _write_debug_dump(debug_dir, diagnostics, resp_text)
            return [], code, diagnostics
        if _requests_exceptions is not None and isinstance(
            e,
            (
                _requests_exceptions.ProxyError,
                _requests_exceptions.InvalidSchema,
                _requests_exceptions.MissingSchema,
            ),
        ):
            _write_debug_dump(debug_dir, diagnostics, "")
            return [], "PROXY_ERROR", diagnostics
        _write_debug_dump(debug_dir, diagnostics, "")
        return [], "NETWORK_ERROR", diagnostics

    # 挑战页识别优先于一切解析(§5.4-3):命中即停,不做任何规避。
    if _looks_blocked(html, _BING_BLOCKED_MARKERS):
        _write_debug_dump(debug_dir, diagnostics, html)
        return [], "PROVIDER_BLOCKED", diagnostics

    parser = _SerpResultParser(snippet_hints=_BING_SNIPPET_CLASS_HINTS)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001
        pass

    # 配对:每个结果锚点取文档序其后第一条未消费摘要。
    results: list[dict[str, Any]] = []
    snippet_budget = WEB_TOPIC_TOTAL_CONTEXT_MAX_CHARS
    seen_urls: set[str] = set()
    snippets = parser.snippets
    scan_from = 0
    for a_seq, href, title in parser.anchors:
        if len(results) >= max_results:
            break
        url = _normalize_bing_result_url(href)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        snippet = ""
        si = scan_from
        while si < len(snippets) and snippets[si][0] <= a_seq:
            si += 1
        if si < len(snippets):
            raw_snip = snippets[si][1]
            scan_from = si + 1
            if snippet_budget > 0:
                snippet = _clean_web_text(
                    raw_snip, min(WEB_TOPIC_SNIPPET_MAX_CHARS, snippet_budget)
                )
                snippet_budget -= len(snippet)
        results.append(
            {
                "url": url,
                "title": _clean_web_text(title, 300),
                "snippet": snippet,
                "source": "bing_html",
            }
        )

    if not results:
        low = html.lower()
        if any(marker.lower() in low for marker in _BING_NO_RESULTS_MARKERS):
            return [], "NO_RESULTS", diagnostics
        _write_debug_dump(debug_dir, diagnostics, html)
        return [], "PARSE_ERROR", diagnostics
    # 相关性守卫(2026-11-19 真机事故):解析成功但与检索词零相关的降级 SERP
    # (如数据中心出口被反爬降级、返回整页无关缓存结果)不算成功,落盘取证
    # 并返回 IRRELEVANT_RESULTS(与 PARSE_ERROR 同为可观察失败)。
    matched = _count_relevant_results(query, results)
    diagnostics["relevance_matched"] = matched
    diagnostics["relevance_total"] = len(results)
    if matched < min(WEB_TOPIC_RELEVANCE_MIN_MATCHED, len(results)):
        _write_debug_dump(debug_dir, diagnostics, html)
        return [], "IRRELEVANT_RESULTS", diagnostics
    return results, None, diagnostics


# ---------------------------------------------------------------------------
# MediaCrawler provider(2026-11-20 审查修订版阶段3)
# ---------------------------------------------------------------------------
# Bing 失败消息字典(mc 回退路径与 bing_html 分支共用,避免双份漂移)。
_BING_ERROR_MESSAGES: dict[str, str] = {
    "NETWORK_ERROR": "Bing request failed (network error).",
    "HTTP_ERROR": "Bing returned an HTTP error (see diagnostics).",
    "PROXY_ERROR": (
        "Bing proxy (if configured) is unreachable or PySocks "
        "is missing; request not sent."
    ),
    "PROVIDER_BLOCKED": (
        "Bing blocked this automated query (challenge page "
        "detected); not retrying."
    ),
    "PARSE_ERROR": (
        "Bing page fetched but no result blocks parsed "
        "(layout change?). See search_web_topic_debug.txt "
        "if debug enabled."
    ),
    "IRRELEVANT_RESULTS": (
        "Bing results parsed but unrelated to the query "
        "(degraded/cached SERP suspected). See "
        "search_web_topic_debug.txt if debug enabled."
    ),
}


def _map_mc_records(
    records: list[dict[str, Any]],
    max_results: int,
    platform: Optional[str] = None,
) -> list[dict[str, Any]]:
    """MC 原始记录 → 四键条目(审查 §6.3 结果有效性判定)。

    - url 只接受 http/https 且**必须带 hostname**(2026-11-21 ks/bili 准入
      修订版 §4.3:hostname 缺失整条丢弃,不"就地修复");
    - ``platform`` 提供时执行**平台域名白名单**(§4.3:不让任意外部 URL 绕过
      域名校验)—— hostname 等于或以允许域结尾才接受;平台未登记域名或
      platform 缺省时退化为 scheme+hostname 校验(兼容旧调用/旧桩);
    - title 清洗后非空(空标题条目对下游引用无意义);
    - snippet 允许为空但限长;同 URL 去重取首条;
    - 上限 max_results;source 恒 "mediacrawler"(不因 engine/缓存伪造来源)。
    """
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    allowed_domains = _MC_ALLOWED_URL_DOMAINS.get(str(platform or "").lower())
    for rec in records:
        if len(items) >= max_results:
            break
        if not isinstance(rec, dict):
            continue
        raw_url = ""
        # URL 键白名单(2026-11-21 审查修订版 P1/§5.3;ks/bili 准入修订版 §4.3):
        # xhs=note_url,dy=aweme_url,ks/bili=video_url(各自 store __init__.py
        # 预构建;ks=https://www.kuaishou.com/short-video/{video_id},
        # bili=https://www.bilibili.com/video/av{video_id}),通用回退=url。
        # 不从 photo_id/video_id/bvid 静默猜造 URL;新平台接入前必须按其
        # store 实测字段后再扩此表。
        for key in ("note_url", "url", "aweme_url", "video_url"):
            candidate = str(rec.get(key) or "").strip()
            if candidate:
                raw_url = candidate
                break
        title = _clean_web_text(rec.get("title"), 300)
        snippet = _clean_web_text(
            rec.get("desc") or rec.get("content"), WEB_TOPIC_SNIPPET_MAX_CHARS
        )
        try:
            parsed = urllib.parse.urlsplit(raw_url)
            scheme = parsed.scheme.lower()
            hostname = (parsed.hostname or "").lower()
        except ValueError:
            scheme, hostname = "", ""
        if scheme not in _ALLOWED_URL_SCHEMES or not hostname or not title:
            continue
        if allowed_domains and not any(
            hostname == d or hostname.endswith("." + d) for d in allowed_domains
        ):
            continue
        if raw_url in seen_urls:
            continue
        seen_urls.add(raw_url)
        items.append(
            {"url": raw_url, "title": title, "snippet": snippet, "source": _MC_ITEM_SOURCE}
        )
    return items


# ---------------------------------------------------------------------------
# reach-mcp 条目映射(2026-11-20 第九期:审查修订版 §5.4)
# ---------------------------------------------------------------------------
def _reach_engagement_views(engagement: object) -> Optional[int]:
    """engagement 归一化:dict 取 views / 数值取自身 / 其余 None(非负整数)。"""
    if isinstance(engagement, dict):
        engagement = engagement.get("views")
    if isinstance(engagement, bool) or not isinstance(engagement, (int, float)):
        return None
    if engagement < 0:
        return None
    return int(engagement)


def _map_reach_items(raw_items: list, max_results: int) -> list[dict[str, Any]]:
    """reach 原始条目 → 标准条目(URL 校验/清洗/去重/按源均衡取样)。

    映射规则(审查修订版 §5.4 表):
    - url 只接受合法 http/https 且有 hostname;无效整条丢弃;
    - title 缺失回退 ``text[:80]``,两者皆空整条丢弃;
    - snippet = ``text`` 清洗限长,**允许为空**(不生成虚假摘要);
    - source 规范化为小写真实源名,缺失兜底 ``_REACH_ITEM_SOURCE``;
    - date 保留原始可解析文本(限长),不强猜时区;
    - engagement → ``engagement_views``(dict/数值/None 归一化,P2-08);
    - 同 URL 去重;**按源 round-robin 均衡取样**(§3.7:防单源占满名额)。
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    seen_urls: set[str] = set()
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            continue
        if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES or not parsed.netloc:
            continue
        if url in seen_urls:
            continue
        title = _clean_web_text(raw.get("title"), 200)
        if not title:
            title = _clean_web_text(str(raw.get("text") or "")[:80], 80)
        if not title:
            continue
        snippet = _clean_web_text(raw.get("text"), WEB_TOPIC_SNIPPET_MAX_CHARS)
        source = _clean_web_text(raw.get("source"), 32).lower() or _REACH_ITEM_SOURCE
        seen_urls.add(url)
        buckets.setdefault(source, []).append({
            "url": url,
            "title": title,
            "snippet": snippet,
            "source": source,
            "reach_source": source,
            "date": (str(raw.get("date") or "")[:64] or None),
            "engagement_views": _reach_engagement_views(raw.get("engagement")),
        })
    # round-robin:源名排序保证确定性;名额用尽即停
    merged: list[dict[str, Any]] = []
    source_names = sorted(buckets)
    index = 0
    while len(merged) < max_results and any(buckets[s] for s in source_names):
        progressed = False
        for name in source_names:
            if len(merged) >= max_results:
                break
            queue = buckets[name]
            if queue:
                merged.append(queue.pop(0))
                progressed = True
        if not progressed:
            break
        index += 1
    return merged


# ---------------------------------------------------------------------------
# 对外主入口(可注入 provider)
# ---------------------------------------------------------------------------
def search_web_topic(
    *,
    query: str,
    max_results: int = 10,
    media_dir: Optional[Path] = None,
    provider: str = "duckduckgo_html",
    proxy_url: Optional[str] = None,
    debug_dir: Optional[Path] = None,
    download: bool = False,
    provider_options: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Web 主题搜索。

    Args:
        query: 搜索关键字(节点层已过 ``normalize_query`` 规整,单关键词)。
        max_results: 最多取多少条。
        media_dir: 可选,若指定 + ``download=True`` 则抓取 HTML 到本地。
        provider: ``"mediacrawler"`` / ``"duckduckgo_html"`` / ``"google_html"``
            / ``"bing_html"`` / ``"stub"``。
        proxy_url: 传输层代理(google_html 强制 socks5h://;duckduckgo_html
            可选)。只作为网络传输配置,不用于任何挑战绕过(§5.3)。
            mediacrawler 分支不走代理(社交平台直连),回退 Bing 时沿用。
        debug_dir: 诊断落盘目录(仅显式传入且解析失败时写受限 .txt,§5.5);
            由节点层按 ``STORYLINE_WEB_TOPIC_DEBUG`` 开关决定是否传入。
        provider_options: 仅 ``mediacrawler`` 分支使用(platform/project_root/
            save_root/timeout_seconds/headless/cdp_port/lock_dir/mc_revision);
            缺 project_root/save_root 直接 raise ValueError(编程错误,由节点
            层 TOOL_EXECUTION_FAILED 留痕)。

    Returns:
        dict::

            {"status": "ok"|"no_results"|"failed",
             "schema_version": 2,
             "engine": UI 展示名, "provider": **实际产出**结果的适配器标识,
             "requested_provider": 节点配置的首选 provider(mediacrawler 分支才有),
             "query": str(实际提交的检索词),
             "search_web_topic": [items],
             "preview_urls": [...],
             "error_code": None|"NO_RESULTS"|"NETWORK_ERROR"|"HTTP_ERROR"
                          |"PROXY_ERROR"|"PROVIDER_BLOCKED"|"PARSE_ERROR"
                          |"IRRELEVANT_RESULTS"|"UNKNOWN_PROVIDER",
             "message": str,
             "query_hash": sha256(query),
             "request_hash": sha256(请求语义,§4.5;按首选策略计算,回退成功也保留),
             "searched_at": UTC isoformat,
             "fallback": mc 回退 Bing 时 {from_provider, from_platform,
                          to_provider, primary_error_code, primary_message},
             "fallback_error": 仅 Bing 回退也失败时 {error_code, message}
                          (两端错误都保留,审查 P1-05),
             "diagnostics": {...}}

    2026-11-10 阶段2(前置化设计方案 §4.2):所有分支统一加
    ``status`` / ``query_hash`` / ``searched_at``;网络异常与解析失败从
    ``NO_RESULTS`` 中分离。``cache_hit`` 只会由节点层复用同 request_hash 缓存时
    改写,本函数不会返回它。
    """
    query_hash = hashlib.sha256(str(query).encode("utf-8")).hexdigest()
    searched_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    request_hash = build_request_hash(
        query=str(query), provider=provider, max_results=max_results
    )
    engine_label = _PROVIDER_ENGINE_LABELS.get(provider, provider)

    def _result(
        *,
        items: list[dict[str, Any]],
        error_code: Optional[str],
        message: str,
        preview_urls: Optional[list[str]] = None,
        diagnostics: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        # NO_RESULTS 是错误码域里的软态,status 归 no_results 而非 failed
        if error_code == "NO_RESULTS":
            status = "no_results"
        elif error_code:
            status = "failed"
        else:
            status = "ok" if items else "no_results"
        doc: dict[str, Any] = {
            "status": status,
            # 2026-11-10 真机验收补漏:§4.2 契约要求记录实际提交的检索词
            # (审计/排障入口;此前只有 query_hash,人工无法直接读回检索内容)
            "query": str(query),
            # 2026-11-19 审查修订版 §4.4:schema v2 契约(引擎展示名/适配器
            # 标识/请求语义 hash/诊断摘要;旧字段全保留,读侧 .get 兼容)
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "engine": engine_label,
            "provider": provider,
            "search_web_topic": items,
            "preview_urls": preview_urls if preview_urls is not None else [],
            "error_code": error_code,
            "message": message,
            "query_hash": query_hash,
            "request_hash": request_hash,
            "searched_at": searched_at,
        }
        if diagnostics is not None:
            doc["diagnostics"] = diagnostics
        return doc

    if provider == "stub":
        return _result(items=[], error_code=None, message="stub provider, no results")

    ddg_diagnostics: dict[str, Any] = {
        "http_status": None,
        "final_host": "duckduckgo.com",
        "proxy_enabled": bool(proxy_url),
        "parser_version": _PARSER_VERSIONS["duckduckgo_html"],
        "response_content_type": None,
        "response_bytes": None,
    }

    if provider == "google_html":
        items, error_code, diagnostics = _google_html_search(
            query=query,
            max_results=max_results,
            proxy_url=proxy_url,
            debug_dir=debug_dir,
        )
        if error_code:
            message = {
                "NETWORK_ERROR": "Google request failed (network error).",
                "HTTP_ERROR": (
                    "Google returned an HTTP error (see diagnostics)."
                ),
                "PROXY_ERROR": (
                    "google_html requires a socks5h:// proxy (and PySocks "
                    "installed); proxy missing/invalid or unreachable."
                ),
                "PROVIDER_BLOCKED": (
                    "Google blocked this automated query (challenge/consent "
                    "page detected); not retrying."
                ),
                "PARSE_ERROR": (
                    "Google page fetched but no result blocks parsed "
                    "(layout change?). See search_web_topic_debug.txt "
                    "if debug enabled."
                ),
                "IRRELEVANT_RESULTS": (
                    "Google results parsed but unrelated to the query "
                    "(degraded/cached SERP suspected). See "
                    "search_web_topic_debug.txt if debug enabled."
                ),
            }.get(error_code, f"search failed: {error_code}")
            if error_code == "IRRELEVANT_RESULTS":
                message += (
                    f" relevance matched {diagnostics.get('relevance_matched', 0)}"
                    f"/{diagnostics.get('relevance_total', 0)} results."
                )
            return _result(
                items=[],
                error_code=error_code,
                message=message,
                diagnostics=diagnostics,
            )
        if not items:
            return _result(
                items=[],
                error_code="NO_RESULTS",
                message="Google returned no results.",
                diagnostics=diagnostics,
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
            diagnostics=diagnostics,
        )

    if provider == "bing_html":
        items, error_code, diagnostics = _bing_html_search(
            query=query,
            max_results=max_results,
            proxy_url=proxy_url,
            debug_dir=debug_dir,
        )
        if error_code:
            # 消息字典与 mediacrawler 回退路径共用(2026-11-20 审查阶段3)
            message = _BING_ERROR_MESSAGES.get(
                error_code, f"search failed: {error_code}"
            )
            if error_code == "IRRELEVANT_RESULTS":
                message += (
                    f" relevance matched {diagnostics.get('relevance_matched', 0)}"
                    f"/{diagnostics.get('relevance_total', 0)} results."
                )
            return _result(
                items=[],
                error_code=error_code,
                message=message,
                diagnostics=diagnostics,
            )
        if not items:
            return _result(
                items=[],
                error_code="NO_RESULTS",
                message="Bing returned no results.",
                diagnostics=diagnostics,
            )
        preview_urls = [it["url"] for it in items if it.get("url")]
        saved_paths = []
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
            diagnostics=diagnostics,
        )

    if provider == "mediacrawler":
        # 2026-11-20 审查修订版阶段3:MediaCrawler 子进程检索 + Bing 自动回退
        # (2026-11-19 用户拍板;artifact 语义=审查文档 §6.2,回退状态矩阵=§6.4)。
        opts = dict(provider_options or {})
        mc_platform = str(opts.get("platform") or "xhs")
        mc_fallback_platforms = tuple(
            str(p) for p in (opts.get("fallback_platforms") or ())
        )
        mc_revision = str(opts.get("mc_revision") or "unknown")
        mc_per_attempt_timeout = int(opts.get("timeout_seconds") or 600)
        mc_chain_timeout = int(opts.get("chain_timeout_seconds") or 600)
        if not opts.get("project_root") or not opts.get("save_root"):
            raise ValueError(
                "mediacrawler provider_options requires project_root and save_root"
            )
        platform_chain: tuple[str, ...] = (mc_platform, *mc_fallback_platforms)
        # request_hash 以**首选检索策略(完整有序平台链)**为准(2026-11-21 审查
        # 修订版 §3.4):链成员/顺序/上游 revision 编入;回退成功也保留该 hash
        # (QA 重试幂等;换链/换 MC 版本必 miss)。
        effective_request_hash = build_request_hash(
            query=str(query),
            provider=mediacrawler_request_provider(platform_chain, mc_revision),
            max_results=max_results,
        )

        attempts: list[dict[str, Any]] = []
        selected_items: list[dict[str, Any]] = []
        selected_platform = ""
        selected_label = ""
        # 整链 deadline(审查修订版 §3.5):单平台 timeout 只是单次上限,预算
        # (chain_timeout_seconds)耗尽后不再启动下一平台,直接进 Bing。
        deadline = time.monotonic() + max(int(mc_chain_timeout), 1)
        for attempt_index, platform in enumerate(platform_chain):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                attempts.append(
                    {
                        "platform": platform,
                        "attempt_index": attempt_index,
                        "error_code": "TIME_BUDGET_EXHAUSTED",
                        "message": (
                            "chain time budget exhausted before this attempt started"
                        ),
                    }
                )
                break
            label = _mc_client.platform_label(platform)
            try:
                records, error_code, message, diag = _mc_client.search_notes(
                    query=str(query),
                    max_results=max_results,
                    platform=platform,
                    project_root=str(opts.get("project_root")),
                    save_root=str(opts.get("save_root")),
                    timeout_seconds=min(mc_per_attempt_timeout, int(remaining)),
                    headless=bool(opts.get("headless") or False),
                    cdp_port=int(opts.get("cdp_port") or 9222),
                    lock_dir=opts.get("lock_dir"),
                )
            except (TypeError, ValueError) as exc:
                # provider_options 形状错误属编程错误:节点层 TOOL_EXECUTION_FAILED 留痕
                raise ValueError(
                    f"mediacrawler provider_options invalid: {exc}"
                ) from exc

            items = (
                []
                if error_code
                else _map_mc_records(records, max_results, platform=platform)
            )
            # 单平台成功条件(审查修订版 §4.2):子进程成功 + 结果文件可读 +
            # 映射后 ≥1 条有效 + 相关性守卫通过;任一不满足 → 记录原因后试下一
            # 平台。相关性守卫(2026-11-19 真机事故防线)逐平台执行,低相关结果
            # 不算成功。
            relevance_blocked = False
            if error_code is None and items:
                matched = _count_relevant_results(str(query), items)
                diag.setdefault("relevance_matched", matched)
                diag.setdefault("relevance_total", len(items))
                if matched < min(WEB_TOPIC_RELEVANCE_MIN_MATCHED, len(items)):
                    relevance_blocked = True
            attempt_error = error_code or (
                "NO_RESULTS"
                if not items
                else ("IRRELEVANT_RESULTS" if relevance_blocked else None)
            )
            attempt_message = str(message or "")
            if relevance_blocked:
                attempt_message = (
                    f"relevance matched {diag['relevance_matched']}"
                    f"/{diag['relevance_total']} results"
                )
            attempts.append(
                {
                    "platform": platform,
                    "attempt_index": attempt_index,
                    "error_code": attempt_error,
                    "message": attempt_message,
                    "diagnostics": diag,
                }
            )
            if attempt_error is None:
                selected_items = items
                selected_platform = platform
                selected_label = label
                break

        diagnostics: dict[str, Any] = {
            "requested_provider": "mediacrawler",
            "requested_platform": mc_platform,
            "platform_chain": list(platform_chain),
            "attempts": attempts,
            "mc_revision": mc_revision,
            "adapter_version": _mc_client.MEDIACRAWLER_ADAPTER_VERSION,
            "parser_version": (
                f"mc_{mc_platform}_v{_mc_client.MEDIACRAWLER_ADAPTER_VERSION}"
            ),
            # primary 保持 = 主平台(首次尝试)诊断,兼容既有页面与断言(§4.4)
            "primary": (attempts[0].get("diagnostics") or {}) if attempts else {},
        }

        if selected_platform:
            # MC 某平台成功:provider 恒 mediacrawler(source 不因平台伪造),
            # engine 按实际产出平台显示;非主平台成功时 message 记录切换原因、
            # platform_fallback 留痕(审查修订版 §4.4)。
            switch_reasons = [
                f"MediaCrawler({_mc_client.platform_label(str(a['platform']))}) "
                + str(a["error_code"])
                + (f": {a['message']}" if a.get("message") else "")
                for a in attempts[:-1]
                if a.get("error_code")
            ]
            switch_message = (
                "; ".join(switch_reasons) + f"; 已切换 MediaCrawler({selected_label})"
                if switch_reasons
                else ""
            )
            preview_urls = [it["url"] for it in selected_items if it.get("url")]
            doc = _result(
                items=selected_items,
                preview_urls=preview_urls,
                error_code=None,
                message=switch_message,
                diagnostics=diagnostics,
            )
            doc["request_hash"] = effective_request_hash
            doc["provider"] = "mediacrawler"
            doc["requested_provider"] = "mediacrawler"
            doc["selected_platform"] = selected_platform
            doc["engine"] = f"MediaCrawler({selected_label})"
            if selected_platform != mc_platform:
                doc["platform_fallback"] = {
                    "from_platform": mc_platform,
                    "to_platform": selected_platform,
                    "reason": attempts[0].get("error_code") if attempts else None,
                }
            return doc

        # ---- 全链失败 → bing_html(审查 §6.4 回退矩阵;Bing 只调一次) ----
        primary_attempt = attempts[0] if attempts else {}
        fallback_info: dict[str, Any] = {
            "from_provider": "mediacrawler",
            "from_platform": mc_platform,
            "to_provider": "bing_html",
            # primary_* = 主平台(首次尝试)结果,字段名语义不变;整链逐平台
            # 失败历史在 diagnostics.attempts 与 message(审查修订版 §4.4)。
            "primary_error_code": primary_attempt.get("error_code") or "NO_RESULTS",
            "primary_message": str(primary_attempt.get("message") or ""),
        }
        diagnostics["fallback_engine"] = "bing_html"
        bing_items, bing_error, bing_diag = _bing_html_search(
            query=query,
            max_results=max_results,
            proxy_url=proxy_url,
            debug_dir=debug_dir,
        )
        diagnostics["fallback_diagnostics"] = bing_diag
        mc_reason = "; ".join(
            f"MediaCrawler({_mc_client.platform_label(str(a['platform']))}) "
            + str(a["error_code"])
            + (f": {a['message']}" if a.get("message") else "")
            for a in attempts
            if a.get("error_code")
        ) or "MediaCrawler all platforms failed without attempts"

        if bing_error:
            # 双重失败:status=failed;MC 主错误挂 fallback,Bing 错误挂
            # fallback_error(两端都不丢,审查 P1-05)
            bing_message = _BING_ERROR_MESSAGES.get(
                bing_error, f"search failed: {bing_error}"
            )
            if bing_error == "IRRELEVANT_RESULTS":
                bing_message += (
                    f" relevance matched {bing_diag.get('relevance_matched', 0)}"
                    f"/{bing_diag.get('relevance_total', 0)} results."
                )
            doc = _result(
                items=[],
                error_code=bing_error,
                message=f"{mc_reason}; Bing fallback also failed ({bing_message})",
                diagnostics=diagnostics,
            )
            doc["request_hash"] = effective_request_hash
            doc["provider"] = "bing_html"
            doc["requested_provider"] = "mediacrawler"
            doc["engine"] = "Bing"
            doc["fallback"] = fallback_info
            doc["fallback_error"] = {"error_code": bing_error, "message": bing_message}
            return doc
        if not bing_items:
            doc = _result(
                items=[],
                error_code="NO_RESULTS",
                message=f"{mc_reason}; Bing fallback returned no results.",
                diagnostics=diagnostics,
            )
            doc["request_hash"] = effective_request_hash
            doc["provider"] = "bing_html"
            doc["requested_provider"] = "mediacrawler"
            doc["engine"] = "Bing"
            doc["fallback"] = fallback_info
            return doc
        # 回退成功:status=ok(下游有可用结果,不标失败,审查 §6.4),但 engine
        # 如实显示回退来源,message 记录 MC 失败原因,审计链不丢失。
        preview_urls = [it["url"] for it in bing_items if it.get("url")]
        doc = _result(
            items=bing_items,
            preview_urls=preview_urls,
            error_code=None,
            message=f"{mc_reason}; 已回退 Bing",
            diagnostics=diagnostics,
        )
        doc["request_hash"] = effective_request_hash
        doc["provider"] = "bing_html"
        doc["requested_provider"] = "mediacrawler"
        doc["engine"] = "Bing(MediaCrawler 回退)"
        doc["fallback"] = fallback_info
        return doc

    if provider == "reach_mcp":
        # 2026-11-20 第九期:reach-mcp 英文线(审查修订版 §5)。设计要点:
        # - provider_options 必含 endpoint/sources/timeout_seconds,形状错误
        #   raise ValueError(编程错误,节点层 TOOL_EXECUTION_FAILED 留痕);
        # - request_hash 用完整 provider 串(源集合/每源条数/时间窗/摘要上限/
        #   适配器版本,§4.5)—— max_results 由 build_request_hash 统一编码;
        # - **无 Bing 回退**(§4.2 两线互不回退):英文线失败只影响英文参考;
        # - 相关性守卫复用中文实现(拉丁 token ≥3 字符,§5.4),拦截即
        #   IRRELEVANT_RESULTS(英文线无回退,守卫=最终态)。
        opts = dict(provider_options or {})
        reach_endpoint = str(opts.get("endpoint") or "")
        reach_sources = list(opts.get("sources") or [])
        reach_days = int(opts.get("days") or 30)
        reach_max_per_source = int(opts.get("max_per_source") or 5)
        reach_max_chars = int(opts.get("max_chars_per_item") or 500)
        reach_timeout = int(opts.get("timeout_seconds") or 90)
        if not reach_endpoint or not reach_sources:
            raise ValueError(
                "reach_mcp provider_options requires endpoint and sources"
            )
        effective_request_hash = build_request_hash(
            query=str(query),
            provider=_reach_client.reach_request_provider(
                sources=reach_sources,
                max_per_source=reach_max_per_source,
                days=reach_days,
                max_chars_per_item=reach_max_chars,
            ),
            max_results=max_results,
        )
        try:
            raw_items, reach_error, reach_message, reach_diag = (
                _reach_client.search_via_reach(
                    query=str(query),
                    sources=reach_sources,
                    days=reach_days,
                    max_per_source=reach_max_per_source,
                    max_chars_per_item=reach_max_chars,
                    endpoint=reach_endpoint,
                    timeout_seconds=reach_timeout,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"reach_mcp provider_options invalid: {exc}") from exc

        reach_items = [] if reach_error else _map_reach_items(
            raw_items or [], max_results
        )
        diagnostics: dict[str, Any] = {
            "requested_provider": "reach_mcp",
            "adapter_version": _reach_client.REACH_ADAPTER_VERSION,
            "parser_version": (
                f"reach_mcp_v{_reach_client.REACH_ADAPTER_VERSION}"
            ),
            "primary": reach_diag,
        }
        relevance_blocked = False
        if reach_error is None and reach_items:
            matched = _count_relevant_results(str(query), reach_items)
            diagnostics["relevance_matched"] = matched
            diagnostics["relevance_total"] = len(reach_items)
            if matched < min(WEB_TOPIC_RELEVANCE_MIN_MATCHED, len(reach_items)):
                relevance_blocked = True
        reach_error_code = reach_error or (
            "NO_RESULTS"
            if not reach_items
            else ("IRRELEVANT_RESULTS" if relevance_blocked else None)
        )
        message = str(reach_message or "")
        if relevance_blocked:
            message = (
                f"relevance matched {diagnostics['relevance_matched']}"
                f"/{diagnostics['relevance_total']} results"
            )
        if not message and reach_error_code:
            message = {
                "NO_RESULTS": "ReachMCP returned no results.",
                "NETWORK_ERROR": "ReachMCP request failed (network / endpoint unreachable).",
                "TOOL_ERROR": "ReachMCP tool reported an error.",
                "PARSE_ERROR": "ReachMCP result could not be parsed.",
            }.get(reach_error_code, f"search failed: {reach_error_code}")

        doc = _result(
            items=reach_items if not reach_error_code else [],
            preview_urls=[it["url"] for it in reach_items if it.get("url")]
            if not reach_error_code
            else [],
            error_code=reach_error_code,
            message=message,
            diagnostics=diagnostics,
        )
        doc["request_hash"] = effective_request_hash
        doc["provider"] = "reach_mcp"
        return doc

    if provider != "duckduckgo_html":
        return _result(
            items=[],
            error_code="UNKNOWN_PROVIDER",
            message=f"unknown search provider: {provider!r}",
        )

    items, error_code = _ddg_html_search(
        query=query,
        max_results=max_results,
        proxies=(
            {"http": proxy_url, "https": proxy_url} if proxy_url else None
        ),
    )
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
                "IRRELEVANT_RESULTS": (
                    "DuckDuckGo results parsed but unrelated to the query "
                    "(degraded/cached SERP suspected)."
                ),
            }.get(error_code, f"search failed: {error_code}"),
            diagnostics=ddg_diagnostics,
        )
    if not items:
        return _result(
            items=[],
            error_code="NO_RESULTS",
            message="DuckDuckGo returned no results.",
            diagnostics=ddg_diagnostics,
        )

    preview_urls = [it["url"] for it in items if it.get("url")]
    saved_paths = []
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
        diagnostics=ddg_diagnostics,
    )


__all__ = [
    "search_web_topic",
    "build_request_hash",
    "strip_instruction_parentheticals",
    "WEB_TOPIC_RELEVANCE_MIN_MATCHED",
    "ARTIFACT_SCHEMA_VERSION",
]
