"""reach-mcp HTTP MCP 客户端 —— 英文网页话题检索适配层(2026-11-20 第九期)。

上游:`xyonium/reach-mcp`(https://github.com/xyonium/reach-mcp),本机以
Docker 常驻容器部署(镜像 ``ghcr.io/xyonium/reach-mcp``,本地叠加镜像
``reach-mcp-local:socks`` 补装 httpx 的 SOCKS 依赖;容器重建与逐源验证
见 docs/第九期开发/ 执行记录 20261120)。

纪律(与 mediacrawler_client 同款,审查修订版 §5.1):
- 不 import config、不读环境变量,全参数显式传入;
- 对外固定四元组 ``(raw_items, error_code, message, diagnostics)``,业务
  故障不抛异常(可预期错误映射到 search_web_topic 错误码域);
- diagnostics/message 脱敏限长;本适配层不持有任何密钥。

**传输实现偏差说明**(相对审查文档 §5.2 的 mcp SDK 建议):审查文档未预见到
``scripts/assembly/phase5_e2e.py`` 的解耦门禁 —— 全仓仅 ``agent_gate/`` 可
以引用 mcp SDK(第五期依赖约简的架构纪律,集成测试 CI 必跑)。本适配层因此
改为**裸 JSON-RPC over httpx**(streamable HTTP MCP 三步握手:initialize →
notifications/initialized → tools/call),零 mcp SDK 依赖、零 asyncio(同步
httpx 直连),协议行为已对活容器实测验证(server 1.30.0,响应恒为 SSE 流,
取最后一条 ``data:`` 行解析)。SDK 升级/迁移问题(P1-05)随之消失。

上游工具契约(2026-11-20 本机实测):
- ``search(query, sources, days, max_per_source, max_chars_per_item,
  synthesize)`` → ``{items, sources_used[], source_summary}``;条目
  ``{source, id, title, url, author, date, score, engagement, text,
  cluster, duration_min?}``;
- **部分源空/错是常态**(sources_used 逐源 status):只要存在通过映射的
  有效条目即成功,逐源状态原样进 diagnostics(§2.4);
- 实测结果无 ``structuredContent``,数据在文本 content 块的 JSON 里 ——
  解包先 structuredContent 后文本 JSON(P1-01);
- ``synthesize`` 固定 False:取原始条目,不依赖容器内 LLM 合成(§4.5,
  作为客户端契约由本模块写死,不是调用方可选项)。
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

#: 适配器版本 —— 参与英文 request_hash(P2-01:必须是 hash provider 串的
#: 组成部分,不允许定义后成为无消费者常量)。上游契约/映射行为变化时递增。
REACH_ADAPTER_VERSION = "1"

#: 单次调用超时下限(秒):三步握手总预算必须大于 0 且有下限。
_MIN_TIMEOUT_SECONDS = 10

#: diagnostics 字段限长(审查 P2-05:source_summary 可能过长且含外部文本)。
_DIAG_STRING_MAX = 300
_DIAG_SOURCES_MAX = 50

_MCP_PROTOCOL_VERSION = "2025-06-18"
_HTTP_ACCEPT = "application/json, text/event-stream"


class ReachRpcError(Exception):
    """MCP JSON-RPC 层错误(服务端返回 error 对象/会话失效/协议不符)。"""


def reach_request_provider(
    *,
    sources: list[str],
    max_per_source: int,
    days: int,
    max_chars_per_item: int,
    adapter_version: str = REACH_ADAPTER_VERSION,
) -> str:
    """英文 request_hash 的 provider 身份串(审查 §4.5)。

    所有影响返回内容的请求维度都必须进入 hash:源集合(排序去重)、
    每源条数、时间窗、单条摘要上限、适配器版本。``max_results`` 与
    query 由 :func:`search_web_topic.build_request_hash` 统一编码。
    """
    normalized = ",".join(_normalize_sources(sources))
    return (
        f"reach_mcp:{normalized}:{int(max_per_source)}:{int(days)}"
        f":{int(max_chars_per_item)}:{adapter_version}"
    )


def _normalize_sources(sources: list[str]) -> list[str]:
    """源名规整:strip/lower/去空/去重/排序(相同集合不同顺序 → 同一 hash)。"""
    seen: set[str] = set()
    for raw in sources or []:
        name = str(raw or "").strip().lower()
        if name:
            seen.add(name)
    return sorted(seen)


def _first_text(result: Any) -> str:
    """取 result 的第一个文本 block(截断,不解析);兼容 dict 与属性对象。"""
    blocks = _get_field(result, "content") or []
    for block in blocks:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            return text[:_DIAG_STRING_MAX]
    return ""


def _get_field(obj: Any, key: str) -> Any:
    """dict / 属性对象双兼容取值(测试假对象与真实 RPC dict 同构处理)。"""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _parse_http_payload(resp: httpx.Response) -> dict:
    """HTTP 响应 → JSON-RPC 消息 dict。

    reach-mcp 实测所有响应均为 ``text/event-stream``(SSE):取最后一条
    ``data:`` 行;application/json 响应直接解析(协议兜底)。
    """
    text = resp.text
    if "event-stream" in (resp.headers.get("content-type") or ""):
        data_lines = [
            ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")
        ]
        text = data_lines[-1] if data_lines else ""
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ReachRpcError(f"invalid JSON-RPC payload: {str(exc)[:120]}") from exc


def _unwrap_tool_result(result: Any) -> tuple[Optional[dict], Optional[str]]:
    """解包 MCP ``tools/call`` 结果 → ``(payload_dict, error_message)``。

    优先级(审查 §5.2):``structuredContent``(dict)→ 文本 content 块的
    JSON 对象 → 失败。``isError=True`` 是工具执行失败,不是解析失败。
    """
    if _get_field(result, "isError"):
        return None, _first_text(result) or "reach-mcp tool reported an error"
    structured = _get_field(result, "structuredContent")
    if isinstance(structured, dict):
        return structured, None
    for block in _get_field(result, "content") or []:
        text = (
            block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        )
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload, None
        return None, "reach-mcp payload is not a JSON object"
    return None, "reach-mcp result has no structured or text payload"


def _validate_items(payload: dict) -> tuple[Optional[list], Optional[str]]:
    """校验 ``payload["items"]`` 必须是 list(缺失/非 list → PARSE_ERROR)。"""
    items = payload.get("items")
    if items is None:
        return [], None
    if not isinstance(items, list):
        return None, "reach-mcp payload items is not a list"
    return items, None


def _normalize_sources_used(raw: Any) -> list[dict]:
    """逐源状态规整限长:保留上游原始状态语义,未知状态不丢弃(P2-02)。"""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for entry in raw[:_DIAG_SOURCES_MAX]:
        if not isinstance(entry, dict):
            continue
        out.append({
            "source": str(entry.get("source") or "")[:64],
            "status": str(entry.get("status") or "")[:32],
            "count": entry.get("count") if isinstance(entry.get("count"), int) else None,
            "error": str(entry.get("error") or "")[:_DIAG_STRING_MAX] or None,
        })
    return out


def _endpoint_netloc(endpoint: str) -> str:
    """diagnostics 只留 endpoint 的 netloc(不落路径/查询串,防 future 凭据入参)。"""
    try:
        return urlparse(str(endpoint or "")).netloc[:120]
    except ValueError:
        return ""


def _classify_transport_error(exc: BaseException) -> tuple[str, str]:
    """传输层异常 → ``(error_code, message)``。

    - :class:`ReachRpcError`:MCP JSON-RPC 层错误 → TOOL_ERROR;
    - ``TimeoutError``/超时 → NETWORK_ERROR(超时语义);
    - 其余(连接拒绝/DNS/HTTP 错误)→ NETWORK_ERROR。
    """
    if isinstance(exc, ReachRpcError):
        return "TOOL_ERROR", f"reach-mcp MCP protocol error: {str(exc)[:_DIAG_STRING_MAX]}"[:400]
    name = type(exc).__name__
    text = str(exc).lower()
    if name == "TimeoutError" or "timeout" in text or "timed out" in text:
        return "NETWORK_ERROR", "reach-mcp request timed out"
    return "NETWORK_ERROR", f"reach-mcp transport error ({name})"


def _rpc_post(
    client: httpx.Client,
    endpoint: str,
    message: dict,
    session_id: Optional[str],
) -> httpx.Response:
    headers = {"Accept": _HTTP_ACCEPT, "Content-Type": "application/json"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    resp = client.post(endpoint, json=message, headers=headers)
    if resp.status_code >= 400:
        raise ReachRpcError(
            f"HTTP {resp.status_code}: {resp.text[:_DIAG_STRING_MAX]}"
        )
    return resp


def _call_reach_search(
    *,
    query: str,
    sources: list[str],
    days: int,
    max_per_source: int,
    max_chars_per_item: int,
    endpoint: str,
    timeout_seconds: int,
    synthesize: bool = False,
) -> dict:
    """传输接缝(seam):streamable HTTP MCP 三步握手,返回 ``tools/call`` 的
    ``result`` 对象(dict)。单测 monkeypatch 本函数即可覆盖全部返回形状,
    无需真实容器(审查 §5.2)。

    裸 JSON-RPC over httpx(同步,零 mcp SDK 依赖 —— 偏差理由见模块头注释):
    ①initialize(取 ``Mcp-Session-Id``)→ ②notifications/initialized →
    ③tools/call。
    """
    effective_timeout = max(_MIN_TIMEOUT_SECONDS, int(timeout_seconds))
    with httpx.Client(timeout=float(effective_timeout)) as client:
        # ① initialize:握手并取会话 id
        init_resp = _rpc_post(
            client,
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "auto-video-editor",
                        "version": REACH_ADAPTER_VERSION,
                    },
                },
            },
            None,
        )
        init_msg = _parse_http_payload(init_resp)
        if "error" in init_msg:
            raise ReachRpcError(str(init_msg["error"])[:_DIAG_STRING_MAX])
        session_id = init_resp.headers.get("mcp-session-id")

        # ② initialized 通知(无 id;202 空响应属正常)
        _rpc_post(
            client,
            endpoint,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id,
        )

        # ③ tools/call
        call_resp = _rpc_post(
            client,
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search",
                    "arguments": {
                        "query": str(query),
                        "sources": list(sources),
                        "days": int(days),
                        "max_per_source": int(max_per_source),
                        "max_chars_per_item": int(max_chars_per_item),
                        # 客户端契约:原始条目,不依赖容器内 LLM 合成(§4.5)
                        "synthesize": bool(synthesize),
                    },
                },
            },
            session_id,
        )
        call_msg = _parse_http_payload(call_resp)
        if "error" in call_msg:
            raise ReachRpcError(str(call_msg["error"])[:_DIAG_STRING_MAX])
        result = call_msg.get("result")
        if not isinstance(result, dict):
            raise ReachRpcError("tools/call result is not an object")
        return result


def search_via_reach(
    *,
    query: str,
    sources: list[str],
    days: int,
    max_per_source: int,
    max_chars_per_item: int,
    endpoint: str,
    timeout_seconds: int,
) -> tuple[list[dict], Optional[str], str, dict]:
    """reach-mcp ``search`` 适配主入口(英文检索专用)。

    Returns:
        四元组 ``(raw_items, error_code, message, diagnostics)``:

        - 成功:``(items, None, "", diagnostics)``;``items`` 是上游原始
          条目 list(未做业务映射,映射归能力层 ``_map_reach_items``);
        - ``items`` 为空且非全源错误 → ``(None, "NO_RESULTS", ...)``;
        - 全部源 error → ``(None, "NETWORK_ERROR", ...)``(§2.4:不可把
          全源故障伪报成普通 no_results);
        - isError / MCP 协议错误 → ``TOOL_ERROR``;
        - 连接失败 / 超时 → ``NETWORK_ERROR``;
        - 结果解包失败 → ``PARSE_ERROR``。

    Raises:
        ValueError: ``endpoint`` 非 http/https(编程/配置错误,由节点层
            TOOL_EXECUTION_FAILED 留痕,与 MC 分支同款纪律)。
    """
    scheme = urlparse(str(endpoint or "")).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"REACH endpoint 必须是 http/https URL: {endpoint!r}")

    ordered_sources = _normalize_sources(sources)
    if not ordered_sources:
        raise ValueError("REACH sources 不能为空")

    diagnostics: dict[str, Any] = {
        "adapter_version": REACH_ADAPTER_VERSION,
        "endpoint_host": _endpoint_netloc(endpoint),
        "sources_requested": ordered_sources,
        "sources_used": [],
        "source_summary": "",
        "available_sources": [],
        "partial_success": False,
        "items_total": 0,
    }

    try:
        result = _call_reach_search(
            query=query,
            sources=ordered_sources,
            days=days,
            max_per_source=max_per_source,
            max_chars_per_item=max_chars_per_item,
            endpoint=endpoint,
            timeout_seconds=timeout_seconds,
            synthesize=False,
        )
    except Exception as exc:  # noqa: BLE001 - 统一映射,不向节点层穿透
        error_code, message = _classify_transport_error(exc)
        return None, error_code, message, diagnostics

    # isError=True 是工具执行失败(服务端已知错误),先于解包判定(P1-01)
    if _get_field(result, "isError"):
        _, unwrap_error = _unwrap_tool_result(result)
        return (
            None,
            "TOOL_ERROR",
            (unwrap_error or "reach-mcp tool reported an error")[:400],
            diagnostics,
        )

    payload, unwrap_error = _unwrap_tool_result(result)
    if payload is None:
        return (
            None,
            "PARSE_ERROR",
            (unwrap_error or "reach-mcp payload unwrap failed")[:400],
            diagnostics,
        )

    diagnostics["source_summary"] = str(payload.get("source_summary") or "")[:_DIAG_STRING_MAX]
    diagnostics["sources_used"] = _normalize_sources_used(payload.get("sources_used"))
    available = payload.get("available_sources")
    if isinstance(available, list):
        diagnostics["available_sources"] = [
            str(x)[:64] for x in available[:_DIAG_SOURCES_MAX]
        ]

    items, items_error = _validate_items(payload)
    if items is None:
        return None, "PARSE_ERROR", (items_error or "")[:400], diagnostics

    diagnostics["items_total"] = len(items)

    # 逐源状态判定(审查 §2.4):全源 error 是网络/源故障,不是"没结果";
    # 部分源空/错只进 diagnostics,不影响成功判定。
    statuses = [str(e.get("status") or "") for e in diagnostics["sources_used"]]
    if statuses and all(s == "error" for s in statuses):
        return (
            None,
            "NETWORK_ERROR",
            f"reach-mcp all sources errored ({diagnostics['source_summary']})",
            diagnostics,
        )
    if not items:
        return None, "NO_RESULTS", "reach-mcp returned no items", diagnostics

    contributing = sum(
        1 for e in diagnostics["sources_used"] if e.get("status") == "ok" and e.get("count")
    )
    diagnostics["partial_success"] = (
        len(ordered_sources) > 1 and 0 < contributing < len(ordered_sources)
    )
    return items, None, "", diagnostics
