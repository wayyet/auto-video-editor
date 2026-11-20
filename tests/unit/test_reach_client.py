"""reach_client 单元测试 —— 全部经 ``_call_reach_search`` 接缝注入,不依赖真实容器。

覆盖(设计审查与修订执行计划 20261120 §10.1):
- structuredContent / 文本 JSON 双路径解包(P1-01);
- isError / 空内容 / 非 JSON / 非 dict / items 非 list;
- 连接失败、超时、MCP 协议错误的错误码映射;
- 部分源空/错的部分成功语义(P2-02)与全源错误不可伪报;
- request hash provider 串的参数完备性(P0-06)。
"""

from __future__ import annotations

import json

import pytest

from storyline_capabilities import reach_client
from storyline_capabilities.reach_client import (
    REACH_ADAPTER_VERSION,
    _unwrap_tool_result,
    reach_request_provider,
    search_via_reach,
)


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(
        self,
        *,
        is_error: bool = False,
        structured: object = None,
        blocks: tuple = (),
    ) -> None:
        self.isError = is_error
        self.structuredContent = structured
        self.content = list(blocks)


def _payload(items: list, **extra) -> str:
    body = {"items": items, "source_summary": "web:3", "sources_used": []}
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# request hash provider 串(P0-06/P2-01)
# ---------------------------------------------------------------------------

def test_reach_request_provider_sorts_and_dedupes_sources() -> None:
    a = reach_request_provider(sources=["web", "reddit", "youtube"], max_per_source=5, days=30, max_chars_per_item=500)
    b = reach_request_provider(sources=["youtube", "web", "reddit", "web"], max_per_source=5, days=30, max_chars_per_item=500)
    assert a == b
    assert a.startswith("reach_mcp:reddit,web,youtube:")


def test_reach_request_provider_every_response_shaping_param_changes_hash_input() -> None:
    base = {"sources": ["web"], "max_per_source": 5, "days": 30, "max_chars_per_item": 500}
    variants = [
        {"max_per_source": 6},
        {"days": 60},
        {"max_chars_per_item": 200},
        {"sources": ["web", "reddit"]},
    ]
    baseline = reach_request_provider(**base)
    for override in variants:
        other = reach_request_provider(**{**base, **override})
        assert other != baseline, f"{override} 未影响 provider 串"


def test_reach_request_provider_includes_adapter_version() -> None:
    v1 = reach_request_provider(sources=["web"], max_per_source=5, days=30, max_chars_per_item=500)
    v2 = reach_request_provider(sources=["web"], max_per_source=5, days=30, max_chars_per_item=500, adapter_version="2")
    assert v1.endswith(f":{REACH_ADAPTER_VERSION}")
    assert v1 != v2


# ---------------------------------------------------------------------------
# _unwrap_tool_result(P1-01:本机实测服务器不返回 structuredContent)
# ---------------------------------------------------------------------------

def test_unwrap_prefers_structured_content() -> None:
    payload, err = _unwrap_tool_result(_Result(structured={"items": [1]}))
    assert err is None and payload == {"items": [1]}


def test_unwrap_falls_back_to_text_json() -> None:
    payload, err = _unwrap_tool_result(_Result(blocks=(_Block(_payload([{"url": "https://a"}])),)))
    assert err is None and payload is not None and payload["items"][0]["url"] == "https://a"


def test_unwrap_is_error_returns_message() -> None:
    payload, err = _unwrap_tool_result(_Result(is_error=True, blocks=(_Block("boom"),)))
    assert payload is None and err == "boom"


def test_unwrap_no_content_is_error() -> None:
    payload, err = _unwrap_tool_result(_Result())
    assert payload is None and err


def test_unwrap_skips_non_json_and_reads_next_block() -> None:
    payload, err = _unwrap_tool_result(_Result(blocks=(_Block("<html>not json</html>"), _Block(_payload([])))))
    assert err is None and payload is not None


def test_unwrap_json_non_object_is_error() -> None:
    payload, err = _unwrap_tool_result(_Result(blocks=(_Block(json.dumps([1, 2])),)))
    assert payload is None and err


# ---------------------------------------------------------------------------
# search_via_reach 错误映射与诊断
# ---------------------------------------------------------------------------

def _install(monkeypatch: pytest.MonkeyPatch, result: object, capture: dict | None = None) -> None:
    def fake_call(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return result

    monkeypatch.setattr(reach_client, "_call_reach_search", fake_call)


_ARGS = {
    "query": "portable power station",
    "sources": ["youtube", "web", "reddit"],
    "days": 30,
    "max_per_source": 5,
    "max_chars_per_item": 500,
    "endpoint": "http://127.0.0.1:8765/mcp",
    "timeout_seconds": 90,
}


def test_search_success_with_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    capture: dict = {}
    _install(
        monkeypatch,
        _Result(blocks=(_Block(_payload(
            [{"url": "https://a", "title": "A"}],
            sources_used=[
                {"source": "web", "status": "ok", "count": 3},
                {"source": "reddit", "status": "no_results", "count": 0},
            ],
            source_summary="web:3 | EMPTY: reddit",
        )),)),
        capture,
    )
    items, error_code, message, diag = search_via_reach(**_ARGS)
    assert error_code is None and len(items) == 1
    assert diag["items_total"] == 1
    assert diag["source_summary"] == "web:3 | EMPTY: reddit"
    assert diag["partial_success"] is True  # 三源请求只有 web 有产出
    assert diag["sources_used"][0]["source"] == "web"
    # 参数组装契约:sources 排序、synthesize 固定 False(P0-05 客户端契约)
    assert capture["sources"] == ["reddit", "web", "youtube"]
    assert capture["synthesize"] is False
    assert capture["days"] == 30 and capture["max_per_source"] == 5
    assert capture["max_chars_per_item"] == 500
    assert message == ""


def test_search_single_source_is_not_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Result(blocks=(_Block(_payload([], sources_used=[
        {"source": "web", "status": "ok", "count": 0},
    ])),)))
    items, error_code, _, diag = search_via_reach(**{**_ARGS, "sources": ["web"]})
    assert error_code == "NO_RESULTS" and items is None
    assert diag["partial_success"] is False


def test_search_empty_items_is_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Result(blocks=(_Block(_payload([])),)))
    items, error_code, _, _ = search_via_reach(**_ARGS)
    assert error_code == "NO_RESULTS" and items is None


def test_search_all_sources_errored_is_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Result(blocks=(_Block(_payload([], sources_used=[
        {"source": "web", "status": "error", "count": 0, "error": "boom"},
        {"source": "reddit", "status": "error", "count": 0, "error": "403"},
    ], source_summary="ERRORS: web, reddit")),)))
    _, error_code, message, diag = search_via_reach(**_ARGS)
    assert error_code == "NETWORK_ERROR"
    assert "ERRORS" in message
    assert diag["sources_used"][1]["error"] == "403"


def test_search_is_error_maps_to_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Result(is_error=True, blocks=(_Block("tool exploded"),)))
    _, error_code, message, _ = search_via_reach(**_ARGS)
    assert error_code == "TOOL_ERROR" and "tool exploded" in message


def test_search_empty_result_maps_to_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Result())
    _, error_code, _, _ = search_via_reach(**_ARGS)
    assert error_code == "PARSE_ERROR"


def test_search_items_not_list_maps_to_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Result(structured={"items": "oops"}))
    _, error_code, _, _ = search_via_reach(**_ARGS)
    assert error_code == "PARSE_ERROR"


def test_search_source_summary_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    long_summary = "x" * 5000
    _install(monkeypatch, _Result(blocks=(_Block(_payload([{"url": "https://a"}], source_summary=long_summary)),)))
    _, _, _, diag = search_via_reach(**_ARGS)
    assert len(diag["source_summary"]) == reach_client._DIAG_STRING_MAX


def test_search_connect_failure_maps_to_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_call(**kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(reach_client, "_call_reach_search", fake_call)
    _, error_code, message, _ = search_via_reach(**_ARGS)
    assert error_code == "NETWORK_ERROR" and "transport error" in message


def test_search_timeout_maps_to_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_call(**kwargs):
        raise TimeoutError()

    monkeypatch.setattr(reach_client, "_call_reach_search", fake_call)
    _, error_code, message, _ = search_via_reach(**_ARGS)
    assert error_code == "NETWORK_ERROR" and "timed out" in message


def test_search_rpc_error_maps_to_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON-RPC 层错误(会话失效/协议不符)→ TOOL_ERROR(裸 httpx 传输,无 mcp SDK)。"""
    def fake_call(**kwargs):
        raise reach_client.ReachRpcError("Session not found")

    monkeypatch.setattr(reach_client, "_call_reach_search", fake_call)
    _, error_code, message, _ = search_via_reach(**_ARGS)
    assert error_code == "TOOL_ERROR" and "Session not found" in message


def test_parse_http_payload_sse_takes_last_data_line() -> None:
    """实测契约:reach-mcp 响应恒为 SSE 流 → 取最后一条 data: 行(锁定解析)。"""
    import httpx

    resp = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text='event: message\ndata: {"a": 1}\n\nevent: message\ndata: {"b": 2}\n\n',
    )
    assert reach_client._parse_http_payload(resp) == {"b": 2}


def test_parse_http_payload_plain_json_fallback() -> None:
    """application/json 响应(协议兜底)直接解析。"""
    import httpx

    resp = httpx.Response(
        200, headers={"content-type": "application/json"}, text='{"ok": true}'
    )
    assert reach_client._parse_http_payload(resp) == {"ok": True}


def test_search_unknown_exception_defaults_to_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Weird(ExceptionGroup):  # anyio 4 的任务组异常包装
        pass

    def fake_call(**kwargs):
        raise _Weird("group", [ConnectionError("refused")])

    monkeypatch.setattr(reach_client, "_call_reach_search", fake_call)
    _, error_code, _, _ = search_via_reach(**_ARGS)
    assert error_code == "NETWORK_ERROR"


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------

def test_search_rejects_non_http_endpoint() -> None:
    with pytest.raises(ValueError):
        search_via_reach(**{**_ARGS, "endpoint": "ftp://127.0.0.1:8765/mcp"})


def test_search_rejects_empty_sources() -> None:
    with pytest.raises(ValueError):
        search_via_reach(**{**_ARGS, "sources": ["", "  "]})
