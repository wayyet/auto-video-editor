"""Xpoz(langchain-xpoz)海外社媒检索适配器(2026-11-20 审查修订版阶段2)。

把 ``langchain-xpoz`` 0.3.0 的四个搜索工具类(X/TikTok/Reddit/Instagram 关键词
搜索)包装成 ``search_web_topic`` 的数据源:逐平台调用、返回原始记录列表;
映射成四键条目 ``{url,title,snippet,source}`` 由能力层完成(本模块不定义条目
契约,与 ``mediacrawler_client`` 同款分层)。

分层纪律:本模块不 import config / 不读环境变量 —— ``api_key``/``platforms``/
``operation_timeout`` 全部由能力层显式传入,保持能力层"可独立 CLI/Script 跑"
的既有约束;诊断信息不含 API Key / Authorization 头(审查 P2-06 脱敏纪律)。

timeout 真实注入(审查 §3.2 P1 修订要求 1):langchain-xpoz 0.3.0 的工具工厂
``get_sync_client(api_key)`` **不收 timeout**(内部 ``XpozClient`` 落到 SDK 默认
300s),本模块按审查推荐路径**扩展工具包装类** —— 子类覆写 ``_run``,把
``operation_timeout`` 显式传给 ``XpozClient(timeout=...)``,使配置真正生效。

超时语义如实声明(审查 §3.2:不允许文档保留一个实际上不生效/不真实的承诺):
``XpozClient(timeout=...)`` 是 **SDK 单次操作超时**;四平台在本适配器内串行,
最坏总耗时 ≈ 平台数 × 该值。本模块**不**提供总超时承诺,artifact diagnostics
逐平台记录实际耗时;若后续升级 SDK 提供整体 deadline 入口,再评估接入。

错误分类(审查 §3.12 P2):按 SDK 具体异常类型捕获映射,不靠异常类名字符串
猜测;``ValidationError`` 是请求参数问题,不伪装成网络故障;未知异常才走通用
分支。返回契约与 ``mediacrawler_client.search_notes`` 同形:
``(records, error_code, message, diagnostics)``,``error_code=None`` 表示成功
(含 0 条 → 由能力层决定 NO_RESULTS 语义;部分平台成功即算成功,审查阶段2)。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from xpoz import XpozClient
from xpoz import (
    AuthenticationError,
    NotFoundError,
    OperationFailedError,
    OperationTimeoutError,
    ValidationError,
    XpozConnectionError,
    XpozError,
)
from langchain_xpoz import (
    XpozInstagramSearch,
    XpozRedditSearch,
    XpozTiktokSearch,
    XpozTwitterSearch,
)

# 适配器版本(进 diagnostics 与 request_hash 解析器版本;行为语义变更必须 +1)。
XPOZ_ADAPTER_VERSION: int = 1

# 平台代码 → 展示名(engine label / 条目 source 用)。
XPOZ_PLATFORM_LABELS: dict[str, str] = {
    "tiktok": "TikTok",
    "twitter": "X/Twitter",
    "reddit": "Reddit",
    "instagram": "Instagram",
}

# 平台代码白名单(能力层/测试共用;与 config._XPOZ_PLATFORM_WHITELIST 一致,
# 不 import config,两处必须同步维护 —— 与 MC_PLATFORM_CODES 同款纪律)。
XPOZ_PLATFORM_CODES: tuple[str, ...] = tuple(XPOZ_PLATFORM_LABELS)

# SDK 缺省操作超时(xpoz._mcp._polling.DEFAULT_TIMEOUT_SECONDS = 300);
# 仅作 config 缺省值参考,本模块不读 env,实际值由能力层传入。
XPOZ_SDK_DEFAULT_TIMEOUT: float = 300.0


def platform_label(platform: str) -> str:
    return XPOZ_PLATFORM_LABELS.get(str(platform), str(platform))


# ---------------------------------------------------------------------------
# timeout 注入型工具子类(审查 §3.2:扩展工具包装类,使 timeout 传递到 SDK)
# ---------------------------------------------------------------------------
# 每个子类仅覆写 ``_run``:把父类内部的 ``get_sync_client(api_key)``(无 timeout)
# 替换为 ``XpozClient(api_key=..., timeout=self.operation_timeout, check_update=False)``,
# 其余(namespace 方法调用形态、返回结构)与父类发布实现逐字一致。
# ``operation_timeout`` 声明为 pydantic 字段,由 ``search_xpoz`` 按平台实例注入。
class _TimeoutTiktokSearch(XpozTiktokSearch):
    operation_timeout: float = XPOZ_SDK_DEFAULT_TIMEOUT

    def _run(self, query: str, max_results=None, start_date=None, end_date=None,
             fields=None, run_manager=None, **kwargs: Any) -> dict:
        client = XpozClient(
            api_key=self.api_key, timeout=float(self.operation_timeout), check_update=False
        )
        result = client.tiktok.search_posts(
            query, limit=max_results, start_date=start_date, end_date=end_date, fields=fields
        )
        return {"data": [i.model_dump() for i in result.data],
                "total_results": result.pagination.total_rows}


class _TimeoutTwitterSearch(XpozTwitterSearch):
    operation_timeout: float = XPOZ_SDK_DEFAULT_TIMEOUT

    def _run(self, query: str, max_results=None, start_date=None, end_date=None,
             language=None, fields=None, run_manager=None, **kwargs: Any) -> dict:
        client = XpozClient(
            api_key=self.api_key, timeout=float(self.operation_timeout), check_update=False
        )
        result = client.twitter.search_posts(
            query, limit=max_results, start_date=start_date, end_date=end_date,
            language=language, fields=fields,
        )
        return {"data": [i.model_dump() for i in result.data],
                "total_results": result.pagination.total_rows}


class _TimeoutRedditSearch(XpozRedditSearch):
    operation_timeout: float = XPOZ_SDK_DEFAULT_TIMEOUT

    def _run(self, query: str, max_results=None, subreddit=None, sort=None,
             start_date=None, end_date=None, fields=None, run_manager=None,
             **kwargs: Any) -> dict:
        client = XpozClient(
            api_key=self.api_key, timeout=float(self.operation_timeout), check_update=False
        )
        result = client.reddit.search_posts(
            query, limit=max_results, subreddit=subreddit, sort=sort,
            start_date=start_date, end_date=end_date, fields=fields,
        )
        return {"data": [i.model_dump() for i in result.data],
                "total_results": result.pagination.total_rows}


class _TimeoutInstagramSearch(XpozInstagramSearch):
    operation_timeout: float = XPOZ_SDK_DEFAULT_TIMEOUT

    def _run(self, query: str, max_results=None, start_date=None, end_date=None,
             fields=None, run_manager=None, **kwargs: Any) -> dict:
        client = XpozClient(
            api_key=self.api_key, timeout=float(self.operation_timeout), check_update=False
        )
        result = client.instagram.search_posts(
            query, limit=max_results, start_date=start_date, end_date=end_date, fields=fields
        )
        return {"data": [i.model_dump() for i in result.data],
                "total_results": result.pagination.total_rows}


# 平台代码 → (工具子类, invoke 输入附加键)。Twitter 显式 language="en"
# (审查 §3.5:服务端过滤优先,`lang` 字段仍用于产物 language_status 二次核验)。
_TOOL_CLASSES: dict[str, tuple[type, dict[str, Any]]] = {
    "tiktok": (_TimeoutTiktokSearch, {}),
    "twitter": (_TimeoutTwitterSearch, {"language": "en"}),
    "reddit": (_TimeoutRedditSearch, {}),
    "instagram": (_TimeoutInstagramSearch, {}),
}


def classify_xpoz_error(exc: BaseException) -> tuple[str, str]:
    """SDK 异常 → (能力层 error_code, 脱敏 message)。

    按 xpoz SDK 具体异常类型映射(审查 §3.12);消息不含 API Key / 请求配置,
    只描述失败类别。未知异常保留类型名供诊断,不伪装成已知类别。
    """
    if isinstance(exc, AuthenticationError):
        return "PROVIDER_BLOCKED", "Xpoz authentication failed (API key invalid or expired)."
    if isinstance(exc, OperationTimeoutError):
        return "NETWORK_ERROR", "Xpoz operation timed out."
    if isinstance(exc, XpozConnectionError):
        return "NETWORK_ERROR", "Xpoz connection failed."
    if isinstance(exc, OperationFailedError):
        return "NETWORK_ERROR", "Xpoz operation failed."
    if isinstance(exc, ValidationError):
        return "PARSE_ERROR", "Xpoz rejected the request parameters."
    if isinstance(exc, NotFoundError):
        return "NETWORK_ERROR", "Xpoz resource not found."
    if isinstance(exc, XpozError):
        return "NETWORK_ERROR", "Xpoz request failed."
    return "NETWORK_ERROR", f"unexpected error: {type(exc).__name__}"


def search_xpoz(
    *,
    query: str,
    max_results: int,
    platforms: tuple[str, ...] | list[str],
    api_key: str,
    operation_timeout: float,
) -> tuple[list[dict[str, Any]], Optional[str], str, dict[str, Any]]:
    """跑一轮 Xpoz 多平台关键词搜索,返回 ``(records, error_code, message, diagnostics)``。

    records 是各平台原始 ``model_dump`` 记录的展平列表,
    形如 ``[{"platform": "tiktok", "item": {...}}, ...]``(未映射、未清洗、
    未做语言过滤 —— 归一化由能力层 ``_map_xpoz_records`` 完成)。

    语义(审查阶段2验收):
    - 逐平台独立调用,**部分平台成功即算成功**(error_code=None);
    - 记录合计为 0 且无错误 → ``(None, "")``(能力层归 NO_RESULTS);
    - **全平台失败** → 聚合错误:任一平台鉴权失败 → PROVIDER_BLOCKED
      (最可操作),否则 NETWORK_ERROR;message 汇总各平台失败原因;
    - invoke 返回体 ``data`` 非 list → 该平台记 PARSE_ERROR(结构契约破坏);
    - diagnostics 逐平台记录 status/count/elapsed_ms/error_*(脱敏)。
    """
    diagnostics: dict[str, Any] = {
        "adapter_version": XPOZ_ADAPTER_VERSION,
        "platforms": {},
        "requested_platforms": list(platforms),
        "operation_timeout": float(operation_timeout),
    }
    requested = [str(p) for p in platforms]
    unknown = [p for p in requested if p not in XPOZ_PLATFORM_CODES]
    if unknown:
        raise ValueError(f"xpoz platforms not in whitelist: {unknown!r}")
    if not requested:
        raise ValueError("xpoz platforms is empty")
    if not api_key:
        return [], "PROVIDER_BLOCKED", (
            "XPOZ_API_KEY missing (get a token at xpoz.ai/get-token and set "
            "the XPOZ_API_KEY environment variable)."
        ), diagnostics

    records: list[dict[str, Any]] = []
    per_platform: dict[str, dict[str, Any]] = {}
    platform_errors: list[tuple[str, str, str]] = []  # (platform, error_code, message)

    for platform in requested:
        entry: dict[str, Any] = {"status": "ok", "count": 0, "elapsed_ms": None}
        started = time.monotonic()
        try:
            tool_cls, extra_input = _TOOL_CLASSES[platform]
            tool = tool_cls(api_key=api_key, operation_timeout=float(operation_timeout))
            result = tool.invoke(
                {"query": str(query), "max_results": int(max_results), **extra_input}
            )
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise ValidationError("invoke returned unexpected structure (data not a list)")
            items = [it for it in result["data"] if isinstance(it, dict)]
            entry["status"] = "ok"
            entry["count"] = len(items)
            records.extend({"platform": platform, "item": it} for it in items)
        except Exception as exc:  # noqa: BLE001 —— 逐平台隔离,失败不拖垮其它平台
            error_code, message = classify_xpoz_error(exc)
            entry["status"] = "error"
            entry["error_code"] = error_code
            entry["error_type"] = type(exc).__name__
            entry["error_message"] = message
            platform_errors.append((platform, error_code, message))
        entry["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        per_platform[platform] = entry

    diagnostics["platforms"] = per_platform

    if len(platform_errors) == len(requested):
        # 全平台失败:鉴权问题最可操作,优先归类
        if any(code == "PROVIDER_BLOCKED" for _, code, _ in platform_errors):
            agg_code = "PROVIDER_BLOCKED"
        else:
            agg_code = "NETWORK_ERROR"
        summary = "; ".join(f"{platform_label(p)} {msg}" for p, _, msg in platform_errors)
        return [], agg_code, f"Xpoz all platforms failed: {summary}", diagnostics

    # 部分或全部成功;0 条由能力层归 NO_RESULTS(与 MC 客户端契约同形)
    return records, None, "", diagnostics


__all__ = [
    "search_xpoz",
    "classify_xpoz_error",
    "platform_label",
    "XPOZ_ADAPTER_VERSION",
    "XPOZ_PLATFORM_LABELS",
    "XPOZ_PLATFORM_CODES",
    "XPOZ_SDK_DEFAULT_TIMEOUT",
]
