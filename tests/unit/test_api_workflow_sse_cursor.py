"""SSE 游标解析(Last-Event-ID)单测(第二期阶段 4 测试矩阵「SSE 断线补发」)。

``web/api_workflow.py:_parse_last_event_id`` 是 SSE 断线补发的入口:
- 优先 ``Last-Event-ID`` 头,回退 ``?last_event_id=`` 查询参数;
- 空/非数字/负值一律视为「无游标」(全量补发从 0 开始);
- 头存在但非法时**不**回退查询参数(显式坏游标不该被静默改写)。

补发本身的幂等性(去重、cursor 单调)由 web/tests/workflow_monitor.test.mjs
(前端 reducer)与 tests/integration/test_workflow_stream_events.py
(PG 侧 fetch_events_after)覆盖,这里只锁解析契约。
"""

from __future__ import annotations

import sys
from pathlib import Path

from starlette.datastructures import Headers, QueryParams

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from web.api_workflow import _parse_last_event_id  # noqa: E402


class _FakeRequest:
    def __init__(self, headers=None, query=None):
        self.headers = Headers(headers or {})
        self.query_params = QueryParams(query or {})


def test_no_header_no_query_returns_none():
    assert _parse_last_event_id(_FakeRequest()) is None


def test_header_numeric():
    assert _parse_last_event_id(_FakeRequest({"Last-Event-ID": "12"})) == 12


def test_header_case_insensitive():
    assert _parse_last_event_id(_FakeRequest({"last-event-id": "7"})) == 7


def test_header_whitespace_stripped():
    assert _parse_last_event_id(_FakeRequest({"Last-Event-ID": " 8 "})) == 8


def test_header_zero_is_valid_cursor():
    assert _parse_last_event_id(_FakeRequest({"Last-Event-ID": "0"})) == 0


def test_header_invalid_returns_none():
    assert _parse_last_event_id(_FakeRequest({"Last-Event-ID": "abc"})) is None


def test_header_negative_returns_none():
    assert _parse_last_event_id(_FakeRequest({"Last-Event-ID": "-1"})) is None


def test_empty_header_falls_back_to_query():
    """空串头视为未提供,回退查询参数。"""
    assert _parse_last_event_id(_FakeRequest({"Last-Event-ID": ""}, {"last_event_id": "7"})) == 7


def test_query_param_fallback():
    assert _parse_last_event_id(_FakeRequest({}, {"last_event_id": "5"})) == 5


def test_header_takes_precedence_over_query():
    assert _parse_last_event_id(_FakeRequest({"Last-Event-ID": "3"}, {"last_event_id": "9"})) == 3


def test_invalid_header_does_not_fall_back_to_query():
    """显式坏游标不被静默改写为查询参数(避免误把全量补发当增量)。"""
    assert _parse_last_event_id(_FakeRequest({"Last-Event-ID": "xyz"}, {"last_event_id": "9"})) is None
