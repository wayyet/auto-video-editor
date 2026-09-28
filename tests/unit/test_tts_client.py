"""FireRedTTS2 / MockTTS client 单测 — 9 工具迁移 §6.3 阶段三。

覆盖:
(a) ``FireRedTTS2Client.synthesize()`` httpx 正常返回 → 透传 audio_path / duration_ms
(b) 服务不可达 → 抛 ``TTSUnavailable``
(c) ``TTS_BACKEND=firered`` 环境变量切换默认 client(对照 ``ASR_BACKEND``)
(d) 字段完整性:响应缺 audio_path → 抛 ``TTSUnavailable``
(e) HTTP 4xx/5xx → 抛 ``TTSUnavailable``
(f) 响应不是 JSON → 抛 ``TTSUnavailable``
(g) ``get_active_backend()`` 正确报告后端
(h) ``MockTTSClient.synthesize()`` 写临时 WAV,>5KB,可被 acceptance 阈值接受
(i) ``call_firered_tts`` 是单入口便捷函数
(j) ``set_default_client`` / ``get_default_client`` 切换后能被 ``call_firered_tts`` 看到
"""

from __future__ import annotations

import os
import struct
import tempfile
import wave
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from jy_common.tts_client import (
    FireRedTTS2Client,
    MockTTSClient,
    TTSUnavailable,
    call_firered_tts,
    get_active_backend,
    set_default_client,
)


# ---------------------------------------------------------------------------
# 测试用具
# ---------------------------------------------------------------------------
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


@pytest.fixture(autouse=True)
def _reset_default_client() -> None:
    """每个测试后重置默认 client,避免跨测试污染。"""
    yield
    set_default_client(MockTTSClient())  # 默认回到 Mock


# ---------------------------------------------------------------------------
# (a) FireRedTTS2Client 正常路径
# ---------------------------------------------------------------------------
def test_firered_synthesize_returns_audio_path_and_duration() -> None:
    """正常 200 + JSON → 透传 audio_path / duration_ms。"""
    payload = {"audio_path": "/out/tts.wav", "duration_ms": 2500}
    fake_http = _FakeHTTPClient(response=_make_response(200, payload))

    client = FireRedTTS2Client(endpoint="http://fake:8010/synthesize", http_client=fake_http)
    out = client.synthesize("Hello world")

    assert out["audio_path"] == "/out/tts.wav"
    assert out["duration_ms"] == 2500
    assert fake_http.calls[0]["url"] == "http://fake:8010/synthesize"
    assert fake_http.calls[0]["json"]["text"] == "Hello world"


def test_firered_synthesize_passes_voice_and_speed() -> None:
    """voice / speed 入参被序列化到请求体。"""
    payload = {"audio_path": "/out/x.wav", "duration_ms": 100}
    fake_http = _FakeHTTPClient(response=_make_response(200, payload))

    client = FireRedTTS2Client(endpoint="http://fake", http_client=fake_http)
    client.synthesize("Hi", voice="en_female_1", speed=1.25)

    sent = fake_http.calls[0]["json"]
    assert sent["voice"] == "en_female_1"
    assert sent["speed"] == 1.25


def test_firered_synthesize_missing_duration_defaults_to_zero() -> None:
    """响应缺 duration_ms → 默认 0(对照 asr_client 字段缺失策略)。"""
    payload = {"audio_path": "/out/x.wav"}  # 没 duration_ms
    fake_http = _FakeHTTPClient(response=_make_response(200, payload))

    client = FireRedTTS2Client(endpoint="http://fake", http_client=fake_http)
    out = client.synthesize("Hi")

    assert out["duration_ms"] == 0


# ---------------------------------------------------------------------------
# (b) 服务不可达 / (e) HTTP 4xx/5xx / (f) 响应非 JSON
# ---------------------------------------------------------------------------
def test_firered_synthesize_raises_on_connection_error() -> None:
    """``httpx.RequestError`` → ``TTSUnavailable``。"""
    fake_http = _FakeHTTPClient(error=httpx.ConnectError("connection refused"))

    client = FireRedTTS2Client(endpoint="http://fake", http_client=fake_http)
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("Hi")
    assert "FireRedTTS2 服务不可达" in str(exc_info.value)
    assert "ConnectError" in str(exc_info.value) or "connection refused" in str(exc_info.value)


def test_firered_synthesize_raises_on_timeout() -> None:
    """``httpx.TimeoutException`` → ``TTSUnavailable``。"""
    fake_http = _FakeHTTPClient(error=httpx.ReadTimeout("timeout"))

    client = FireRedTTS2Client(endpoint="http://fake", http_client=fake_http)
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("Hi")
    assert "不可达" in str(exc_info.value)


def test_firered_synthesize_raises_on_http_4xx() -> None:
    """HTTP 4xx → ``TTSUnavailable``,消息含 status code。"""
    fake_http = _FakeHTTPClient(response=_make_response(400, {"error": "bad"}))

    client = FireRedTTS2Client(endpoint="http://fake", http_client=fake_http)
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("Hi")
    assert "HTTP 400" in str(exc_info.value)


def test_firered_synthesize_raises_on_http_5xx() -> None:
    """HTTP 5xx → ``TTSUnavailable``。"""
    fake_http = _FakeHTTPClient(response=_make_response(503, {"error": "down"}))

    client = FireRedTTS2Client(endpoint="http://fake", http_client=fake_http)
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("Hi")
    assert "HTTP 503" in str(exc_info.value)


def test_firered_synthesize_raises_on_non_json_response() -> None:
    """响应非 JSON → ``TTSUnavailable``。"""
    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.json.side_effect = ValueError("not json")
    bad_resp.text = "not json"
    fake_http = _FakeHTTPClient(response=bad_resp)

    client = FireRedTTS2Client(endpoint="http://fake", http_client=fake_http)
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("Hi")
    assert "JSON" in str(exc_info.value)


# ---------------------------------------------------------------------------
# (d) 字段完整性
# ---------------------------------------------------------------------------
def test_firered_synthesize_raises_when_audio_path_missing() -> None:
    """响应缺 audio_path → ``TTSUnavailable``。"""
    payload = {"duration_ms": 100}  # 没 audio_path
    fake_http = _FakeHTTPClient(response=_make_response(200, payload))

    client = FireRedTTS2Client(endpoint="http://fake", http_client=fake_http)
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("Hi")
    assert "audio_path" in str(exc_info.value)


def test_firered_synthesize_raises_when_response_is_list() -> None:
    """响应是 list 而非 dict → ``TTSUnavailable``。"""
    payload = [{"audio_path": "/x.wav"}]
    fake_http = _FakeHTTPClient(response=_make_response(200, payload))

    client = FireRedTTS2Client(endpoint="http://fake", http_client=fake_http)
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("Hi")
    assert "dict" in str(exc_info.value)


def test_firered_synthesize_empty_text_raises() -> None:
    """空文本 → ``TTSUnavailable``(发请求前就拦)。"""
    fake_http = _FakeHTTPClient(response=_make_response(200, {"audio_path": "/x.wav", "duration_ms": 0}))

    client = FireRedTTS2Client(endpoint="http://fake", http_client=fake_http)
    with pytest.raises(TTSUnavailable):
        client.synthesize("")


# ---------------------------------------------------------------------------
# (c) 环境变量切换
# ---------------------------------------------------------------------------
def test_default_client_can_be_switched_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``TTS_BACKEND=firered`` → 默认 client 是 ``FireRedTTS2Client``。

    注:本测试必须在 set_default_client 之前设置环境变量。
    """
    # 先保存并清空默认,然后用 env 重新解析
    import jy_common.tts_client as tts_mod

    monkeypatch.setenv("TTS_BACKEND", "firered")
    # 重新触发 _resolve_default_client
    new_default = tts_mod._resolve_default_client()
    assert isinstance(new_default, FireRedTTS2Client)

    monkeypatch.setenv("TTS_BACKEND", "mock")
    new_default = tts_mod._resolve_default_client()
    assert isinstance(new_default, MockTTSClient)


def test_unknown_backend_falls_back_to_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """未知 backend → 静默降级为 Mock,不抛错。"""
    import jy_common.tts_client as tts_mod

    monkeypatch.setenv("TTS_BACKEND", "unknown_thing")
    new_default = tts_mod._resolve_default_client()
    assert isinstance(new_default, MockTTSClient)


def test_firered_endpoint_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 endpoint 是 8010 端口(Week 6+ 占位,待真实环境核实)。"""
    monkeypatch.delenv("FIRERED_TTS_ENDPOINT", raising=False)
    client = FireRedTTS2Client()
    assert client._endpoint == "http://127.0.0.1:8010/synthesize"


def test_firered_endpoint_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``FIRERED_TTS_ENDPOINT`` 环境变量覆盖默认。"""
    monkeypatch.setenv("FIRERED_TTS_ENDPOINT", "http://prod:9999/synth")
    client = FireRedTTS2Client()
    assert client._endpoint == "http://prod:9999/synth"


# ---------------------------------------------------------------------------
# (g) get_active_backend
# ---------------------------------------------------------------------------
def test_get_active_backend_reports_correct_type() -> None:
    """``get_active_backend()`` 报告当前默认 client 的类型。"""
    set_default_client(MockTTSClient())
    assert get_active_backend() == "mock"

    # 不实际发请求,只标记 backend
    fake_http = _FakeHTTPClient(response=_make_response(200, {"audio_path": "/x", "duration_ms": 1}))
    set_default_client(FireRedTTS2Client(endpoint="http://x", http_client=fake_http))
    assert get_active_backend() == "firered"

    class _Custom:
        def synthesize(self, text, *, voice=None, speed=None):
            return {"audio_path": "/x", "duration_ms": 0}

    set_default_client(_Custom())  # type: ignore[assignment]
    assert get_active_backend() == "custom"


# ---------------------------------------------------------------------------
# (h) MockTTSClient
# ---------------------------------------------------------------------------
def test_mock_synthesize_writes_wav_above_5kb(tmp_path: Path) -> None:
    """Mock 写一段静音 WAV,>5KB(阶段五 acceptance_check 阈值)。"""
    client = MockTTSClient(output_dir=tmp_path)
    out = client.synthesize("hello world")

    audio_path = Path(out["audio_path"])
    assert audio_path.exists()
    assert audio_path.stat().st_size > 5_000

    # 验证 WAV 头是 "RIFF"
    with open(audio_path, "rb") as f:
        magic = f.read(4)
    assert magic == b"RIFF"


def test_mock_synthesize_duration_in_ms() -> None:
    """``duration_ms`` ≈ duration_s * 1000。"""
    client = MockTTSClient()
    out = client.synthesize("hi")
    assert out["duration_ms"] == 500  # 默认 0.5s


def test_mock_synthesize_unique_filenames(tmp_path: Path) -> None:
    """每次 synthesize 生成新文件名(避免覆盖,允许同文本多次调用)。"""
    client = MockTTSClient(output_dir=tmp_path)
    out_a = client.synthesize("hi")
    out_b = client.synthesize("hi")
    assert out_a["audio_path"] != out_b["audio_path"]


def test_mock_synthesize_empty_text_raises() -> None:
    """Mock 与真实 client 一致:空文本抛 ``TTSUnavailable``。"""
    client = MockTTSClient()
    with pytest.raises(TTSUnavailable):
        client.synthesize("")


# ---------------------------------------------------------------------------
# (i) call_firered_tts — 统一入口便捷函数
# ---------------------------------------------------------------------------
def test_call_firered_tts_dispatches_to_current_default() -> None:
    """``call_firered_tts`` 调用 ``get_default_client().synthesize``。"""
    calls: list[tuple[str, dict]] = []

    class _RecordingClient:
        def synthesize(self, text, *, voice=None, speed=None):
            calls.append((text, {"voice": voice, "speed": speed}))
            return {"audio_path": "/recorded.wav", "duration_ms": 99}

    set_default_client(_RecordingClient())  # type: ignore[assignment]
    out = call_firered_tts("hi", voice="v1", speed=1.5)

    assert calls == [("hi", {"voice": "v1", "speed": 1.5})]
    assert out["audio_path"] == "/recorded.wav"


def test_call_firered_tts_propagates_tts_unavailable() -> None:
    """底层 client 抛 ``TTSUnavailable`` → 便捷函数透传。"""

    class _AlwaysFail:
        def synthesize(self, text, *, voice=None, speed=None):
            raise TTSUnavailable("down")

    set_default_client(_AlwaysFail())  # type: ignore[assignment]
    with pytest.raises(TTSUnavailable):
        call_firered_tts("hi")
