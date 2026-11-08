"""P3 TTS 适配器单测 — storyline TTSClient 协议 → jy_common FireRed 转换。

对应《第三期开发/相似功能节点分析与设计执行计划.md》§6-P3 验收要求:
- ``TTS_BACKEND`` 未设时仍是 ``StubTTSClient``;
- 设为 ``firered`` 时用适配器;
- 适配器把 ``jy_common`` 返回的 dict 转成 ``TTSResult``(duration 按 WAV 头);
- 失败写 ``error``,不抛异常。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jy_common.tts_client import TTSUnavailable
from nodes.storyline._tts_adapter import (
    StorylineFireRedTTSClient,
    install_tts_client_from_env,
)
from storyline_capabilities.tts_runner import (
    StubTTSClient,
    TTSClient,
    get_default_tts_client,
    read_wav_duration_ms,
    set_default_tts_client,
)


@pytest.fixture(autouse=True)
def _reset_default_tts_client():
    """每例结束后回落 lazy Stub,避免污染其他测试(模仿 test_tts_client.py)。"""
    yield
    set_default_tts_client(None)


# ---------------------------------------------------------------------------
# install_tts_client_from_env 装配
# ---------------------------------------------------------------------------
def test_install_default_stub_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TTS_BACKEND", raising=False)
    assert install_tts_client_from_env() == "stub"
    assert isinstance(get_default_tts_client(), StubTTSClient)


def test_install_firered_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_BACKEND", "firered")
    assert install_tts_client_from_env() == "firered"
    assert isinstance(get_default_tts_client(), StorylineFireRedTTSClient)


def test_install_unknown_backend_falls_back_to_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未知后端与 jy_common 侧同语义:降级,不抛错。"""
    monkeypatch.setenv("TTS_BACKEND", "weird_backend")
    assert install_tts_client_from_env() == "stub"
    assert isinstance(get_default_tts_client(), StubTTSClient)


# ---------------------------------------------------------------------------
# dict → TTSResult 转换
# ---------------------------------------------------------------------------
def _make_real_wav(tmp_path: Path, text: str = "适配器测试文本") -> Path:
    """用 StubTTSClient 生成一份真实 wav(静音但头合法),返回其路径。"""
    src = tmp_path / "src" / "firered_out.wav"
    src.parent.mkdir(parents=True, exist_ok=True)
    res = StubTTSClient().synthesize(text=text, wav_path=src, params={})
    assert res.error is None and src.exists()
    return src


def _fake_call(monkeypatch: pytest.MonkeyPatch, ret: Any, capture: list[dict] | None = None) -> None:
    """打桩 ``nodes.storyline._tts_adapter.call_firered_tts``。"""

    def fake(text: str, *, voice: str | None = None, speed: float | None = None) -> dict:
        if capture is not None:
            capture.append({"text": text, "voice": voice, "speed": speed})
        if isinstance(ret, Exception):
            raise ret
        return ret

    monkeypatch.setattr("nodes.storyline._tts_adapter.call_firered_tts", fake)


def test_adapter_converts_jy_dict_to_tts_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_real_wav(tmp_path)
    # 干扰值:服务返回的 duration_ms 与 WAV 头不一致,适配器必须以 WAV 头为准
    captured: list[dict] = []
    _fake_call(
        monkeypatch,
        {"audio_path": str(src), "duration_ms": 1234, "voice": "v", "speed": 1.0},
        capture=captured,
    )

    target = tmp_path / "out" / "voiceover_0001_1.wav"
    out = StorylineFireRedTTSClient().synthesize(
        text="hello", wav_path=target, params={"voice": "v1", "speed": "1.2"}
    )

    # 协议实现校验(runtime_checkable)
    assert isinstance(StorylineFireRedTTSClient(), TTSClient)
    # 参数提取与类型对齐:voice 字符串、speed 转成 float
    assert captured[0]["voice"] == "v1"
    assert captured[0]["speed"] == 1.2
    # 音频复制到协议要求的 wav_path
    assert out.wav_path == target
    assert target.exists()
    # duration 按 WAV 头,不是服务返回的 1234
    assert out.duration_ms == read_wav_duration_ms(target)
    assert out.duration_ms != 1234
    assert out.error is None
    assert out.provider == "firered"
    assert out.params_used == {"voice": "v1", "speed": "1.2"}


def test_adapter_wav_header_unreadable_falls_back_to_server_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """audio_path 是非 wav 文件:头解析失败 → 回落服务返回值。"""
    fake_audio = tmp_path / "src" / "not_a_wav.bin"
    fake_audio.parent.mkdir(parents=True, exist_ok=True)
    fake_audio.write_bytes(b"\x00\x01\x02\x03")
    _fake_call(
        monkeypatch,
        {"audio_path": str(fake_audio), "duration_ms": 4321, "voice": None, "speed": None},
    )
    target = tmp_path / "out" / "v.wav"
    out = StorylineFireRedTTSClient().synthesize(text="x", wav_path=target, params={})
    assert out.error is None
    assert out.duration_ms == 4321


def test_adapter_call_failure_writes_error_no_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_call(monkeypatch, TTSUnavailable("connection refused"))
    target = tmp_path / "out" / "v.wav"
    out = StorylineFireRedTTSClient().synthesize(text="x", wav_path=target, params={})
    assert out.error is not None and "connection refused" in out.error
    assert out.duration_ms == 0
    assert out.provider == "firered"


def test_adapter_missing_audio_path_writes_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for bad in ("", str(tmp_path / "nope" / "missing.wav")):
        _fake_call(monkeypatch, {"audio_path": bad, "duration_ms": 500})
        target = tmp_path / "out" / "v.wav"
        out = StorylineFireRedTTSClient().synthesize(text="x", wav_path=target, params={})
        assert out.error is not None and "audio_path" in out.error
        assert out.duration_ms == 0


def test_adapter_copy_failure_writes_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_real_wav(tmp_path)
    _fake_call(monkeypatch, {"audio_path": str(src), "duration_ms": 500})

    def boom(src_, dst_):  # noqa: ANN001 — monkeypatch 目标
        raise OSError("disk full")

    monkeypatch.setattr("nodes.storyline._tts_adapter.shutil.copyfile", boom)
    target = tmp_path / "out" / "v.wav"
    out = StorylineFireRedTTSClient().synthesize(text="x", wav_path=target, params={})
    assert out.error is not None and "wav copy failed" in out.error
    assert out.duration_ms == 0


def test_adapter_invalid_speed_falls_back_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_real_wav(tmp_path)
    captured: list[dict] = []
    _fake_call(
        monkeypatch,
        {"audio_path": str(src), "duration_ms": 500, "voice": None, "speed": None},
        capture=captured,
    )
    target = tmp_path / "out" / "v.wav"
    out = StorylineFireRedTTSClient().synthesize(
        text="x", wav_path=target, params={"speed": "not-a-number"}
    )
    assert out.error is None
    assert captured[0]["speed"] is None


# ---------------------------------------------------------------------------
# 阶段 2(2026-11-09 效果缺失修复):TTS_BACKEND 生产门禁(fail-closed)
# ---------------------------------------------------------------------------
def test_production_tts_backend_gate(monkeypatch) -> None:
    """APP_ENV=production 时 TTS_BACKEND 必须显式 firered,否则拒绝合成;
    开发/测试环境(APP_ENV 非 production)不 gate,行为不变。"""
    from nodes.storyline.node_generate_voiceover import (
        _production_tts_backend_violation,
        storyline_generate_voiceover_node,
    )

    monkeypatch.delenv("TTS_BACKEND", raising=False)

    monkeypatch.setenv("APP_ENV", "production")
    assert _production_tts_backend_violation() is not None  # 未设置 → 违规
    monkeypatch.setenv("TTS_BACKEND", "mock")
    assert _production_tts_backend_violation() is not None  # mock(stub)→ 违规
    monkeypatch.setenv("TTS_BACKEND", "firered")
    assert _production_tts_backend_violation() is None      # 显式 firered → 放行

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("TTS_BACKEND", "mock")
    assert _production_tts_backend_violation() is None      # 非 production 不 gate

    # 节点入口 fail-closed:production + mock → RuntimeError(不产假音频)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TTS_BACKEND", "mock")
    import pytest

    with pytest.raises(RuntimeError, match="TTS_BACKEND"):
        storyline_generate_voiceover_node({})
