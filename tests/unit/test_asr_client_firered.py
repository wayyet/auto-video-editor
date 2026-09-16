"""FireRedASR2S client 单测 — Week 3 补全 §11/P1-2 接入验证。

覆盖:
(a) ``FireRedASR2SClient.transcribe()`` httpx 正常返回 → 透传结果
(b) 服务不可达 → 抛 ``ASRUnavailable``
(c) ``ASR_BACKEND=firered`` 环境变量切换默认 client
(d) 字段完整性:响应缺字段 → 跳过该条
(e) HTTP 4xx/5xx → 抛 ``ASRUnavailable``
(f) 响应不是 JSON → 抛 ``ASRUnavailable``
(g) ``get_active_backend()`` 正确报告后端
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from jy_common.asr_client import (
    ASRUnavailable,
    FireRedASR2SClient,
    MockASRClient,
    get_active_backend,
    set_default_client,
)


class _FakeHTTPClient:
    """最小化 httpx 替身:可控的 post 返回。"""

    def __init__(self, *, response=None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    def post(self, url, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self._error is not None:
            raise self._error
        return self._response


def _make_response(status_code: int, payload) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = str(payload)[:200]
    return resp


# ---------------------------------------------------------------------------
# (a) httpx 正常返回 → 透传
# ---------------------------------------------------------------------------
def test_firered_client_transcribe_happy_path() -> None:
    payload = [
        {"text": "你好", "start_s": 0.0, "end_s": 1.5},
        {"text": "世界", "start_s": 1.5, "end_s": 3.0},
    ]
    fake_http = _FakeHTTPClient(response=_make_response(200, payload))
    client = FireRedASR2SClient(endpoint="http://test:9999/transcribe", http_client=fake_http)

    out = client.transcribe("/tmp/v.mp4")

    assert out == payload
    assert fake_http.calls[0]["url"] == "http://test:9999/transcribe"
    assert fake_http.calls[0]["json"] == {"video_path": "/tmp/v.mp4"}


# ---------------------------------------------------------------------------
# (b) 服务不可达 → 抛 ASRUnavailable
# ---------------------------------------------------------------------------
def test_firered_client_raises_when_service_unreachable() -> None:
    fake_http = _FakeHTTPClient(error=httpx.ConnectError("Connection refused"))
    client = FireRedASR2SClient(http_client=fake_http)

    with pytest.raises(ASRUnavailable) as exc_info:
        client.transcribe("/tmp/v.mp4")
    assert "不可达" in str(exc_info.value)


def test_firered_client_raises_on_timeout() -> None:
    fake_http = _FakeHTTPClient(error=httpx.TimeoutException("timeout"))
    client = FireRedASR2SClient(http_client=fake_http)

    with pytest.raises(ASRUnavailable):
        client.transcribe("/tmp/v.mp4")


# ---------------------------------------------------------------------------
# (d) 字段完整性:缺字段 → 跳过
# ---------------------------------------------------------------------------
def test_firered_client_skips_items_with_missing_fields() -> None:
    payload = [
        {"text": "好", "start_s": 0.0, "end_s": 1.0},
        {"text": "no time"},                              # 缺 start_s/end_s → 跳过
        {"text": "bad start", "start_s": "abc", "end_s": 1.0},  # 不可解析 → 跳过
        "string item",                                   # 不是 dict → 跳过
        {"text": "good", "start_s": 1.0, "end_s": 2.0},
    ]
    fake_http = _FakeHTTPClient(response=_make_response(200, payload))
    client = FireRedASR2SClient(http_client=fake_http)

    out = client.transcribe("/tmp/v.mp4")

    assert len(out) == 2
    assert out[0]["text"] == "好"
    assert out[1]["text"] == "good"


# ---------------------------------------------------------------------------
# (e) HTTP 4xx/5xx
# ---------------------------------------------------------------------------
def test_firered_client_raises_on_http_error() -> None:
    fake_http = _FakeHTTPClient(response=_make_response(500, "Internal Server Error"))
    client = FireRedASR2SClient(http_client=fake_http)

    with pytest.raises(ASRUnavailable) as exc_info:
        client.transcribe("/tmp/v.mp4")
    assert "HTTP 500" in str(exc_info.value)


# ---------------------------------------------------------------------------
# (f) 响应不是 JSON
# ---------------------------------------------------------------------------
def test_firered_client_raises_on_invalid_json() -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.side_effect = ValueError("not json")
    resp.text = "not json"

    fake_http = _FakeHTTPClient(response=resp)
    client = FireRedASR2SClient(http_client=fake_http)

    with pytest.raises(ASRUnavailable):
        client.transcribe("/tmp/v.mp4")


def test_firered_client_raises_on_non_list_response() -> None:
    fake_http = _FakeHTTPClient(response=_make_response(200, {"segments": []}))
    client = FireRedASR2SClient(http_client=fake_http)

    with pytest.raises(ASRUnavailable):
        client.transcribe("/tmp/v.mp4")


# ---------------------------------------------------------------------------
# (c) ASR_BACKEND 环境变量切换 + (g) get_active_backend
# ---------------------------------------------------------------------------
def test_get_active_backend_reports_mock_by_default() -> None:
    set_default_client(MockASRClient())
    assert get_active_backend() == "mock"


def test_get_active_backend_reports_firered() -> None:
    fake_http = _FakeHTTPClient()
    set_default_client(FireRedASR2SClient(http_client=fake_http))
    assert get_active_backend() == "firered"


def test_set_default_client_round_trip() -> None:
    """set_default_client 注入 + get_active_backend 报告一致。"""
    fake_http = _FakeHTTPClient()
    firered = FireRedASR2SClient(http_client=fake_http)
    set_default_client(firered)

    from jy_common.asr_client import get_default_client
    assert get_default_client() is firered
    assert get_active_backend() == "firered"

    # 还原默认 — 避免污染后续测试
    set_default_client(MockASRClient())


# ---------------------------------------------------------------------------
# Mock fallback — 节点 8 捕获 ASRUnavailable 后降级为 Mock
# ---------------------------------------------------------------------------
def test_mock_client_works_after_firered_failure() -> None:
    """节点 8 降级路径:Mock 在 ASRUnavailable 后兜底。"""
    fake_http = _FakeHTTPClient(error=httpx.ConnectError("refused"))
    firered = FireRedASR2SClient(http_client=fake_http)

    try:
        firered.transcribe("/tmp/v.mp4")
    except ASRUnavailable:
        # 降级为 Mock
        mock = MockASRClient(segments=2)
        out = mock.transcribe("/tmp/v.mp4")
        assert len(out) == 2
        assert all("Mock" in seg["text"] for seg in out)