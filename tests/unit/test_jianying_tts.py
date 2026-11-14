"""剪映 SAMI TTS 客户端单测(TTS_BACKEND=jianying,2026-11-14)。

覆盖:
(a) ``resolve_speaker``:中文名 / speaker_id / 未知 / 空 → 回落语义
(b) ``default_speaker``:env TTS_VOICE_ZH / TTS_VOICE_EN 覆盖 + 默认值
(c) ``build_atempo_filter``:单级 / 越界拆级 / 非法值
(d) ``build_start_task``:SAMI StartTask 消息形状(appkey/app_id/payload)
(e) ``read_device_identity``:fake 用户数据目录解析 / env 覆盖 / 兜底常量
(f) ``JianYingSamiClient.synthesize`` happy path(fake ws + fake ffmpeg):
    二进制帧收集、TaskFinished、ogg→wav、WAV 头时长、音色解析
(g) 失败路径:TaskFailed / 收帧超时 / 无音频 / 转码失败 / 0 时长 / 空文本
    → 一律 ``TTSUnavailable``
(h) 重试:第 1 次失败第 2 次成功;耗尽后报错带重试次数
(i) 音色回退:未知音色回落默认,``voice_requested`` 留痕
(j) CSV 音色表资产完整性(热门音色可解析)
(k) ``TTS_BACKEND=jianying`` → ``tts_client`` 默认 client 分派
(l) ``speech_synthesize`` jianying/sami 别名分派(mock sami client)

网络全部走注入位(ws_factory / ffmpeg_runner),不打真网。
"""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path
from typing import Any

import pytest

from jy_common import jianying_tts
from jy_common.jianying_tts import (
    DEFAULT_WS_URL,
    JianYingSamiClient,
    PROVIDER,
    build_atempo_filter,
    build_start_task,
    default_speaker,
    read_device_identity,
    resolve_speaker,
)


# ---------------------------------------------------------------------------
# 测试用具
# ---------------------------------------------------------------------------
class _FakeWS:
    """最小 websockets 连接替身:按脚本回放帧。"""

    def __init__(self, frames: list[Any], *, hang_after: bool = False) -> None:
        self._frames = list(frames)
        self._hang_after = hang_after
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> Any:
        if self._frames:
            return self._frames.pop(0)
        if self._hang_after:
            await asyncio.sleep(3600)
        await asyncio.sleep(3600)  # 不应到达:wait_for 会先超时


class _FakeConnect:
    """``websockets.connect`` 替身:记录 url/headers,回放同一份帧脚本。"""

    last_kwargs: dict[str, Any] = {}
    last_url: str = ""

    def __init__(
        self,
        frames: list[Any],
        *,
        hang_after: bool = False,
        connect_error: Exception | None = None,
    ) -> None:
        self._frames = frames
        self._hang_after = hang_after
        self._connect_error = connect_error
        self.ws: _FakeWS | None = None

    def __call__(self, url: str, **kwargs: Any) -> "_FakeConnect":
        _FakeConnect.last_url = url
        _FakeConnect.last_kwargs = kwargs
        return self

    async def __aenter__(self) -> _FakeWS:
        if self._connect_error is not None:
            raise self._connect_error
        self.ws = _FakeWS(self._frames, hang_after=self._hang_after)
        return self.ws

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _wav_writer_runner(duration_s: float = 0.3) -> Any:
    """fake ffmpeg runner:把 ogg 输入无视,直接在目标路径写合法 WAV。"""

    def runner(cmd: list[str]) -> None:
        wav = Path(cmd[-1])
        wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(b"\x00\x00" * int(24000 * duration_s))

    return runner


def _boom_runner(cmd: list[str]) -> None:
    raise RuntimeError("ffmpeg exploded")


def _happy_frames(chunks: list[bytes] | None = None) -> list[Any]:
    body = chunks if chunks is not None else [b"oggdata1", b"oggdata2"]
    return [*body, json.dumps({"event": "TaskFinished"})]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉音色/身份相关 env,保证用例封闭。"""
    for name in (
        "TTS_VOICE_ZH",
        "TTS_VOICE_EN",
        "SAMI_TTS_DEVICE_ID",
        "SAMI_TTS_IID",
        "SAMI_TTS_WS_URL",
        "SAMI_TTS_INSECURE_SSL",
        "JY_USER_DATA_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# (a) resolve_speaker
# ---------------------------------------------------------------------------
def test_resolve_speaker_by_chinese_name() -> None:
    assert resolve_speaker("清亮男声") == (
        "zh_male_iclvop_xiaolinkepu",
        "清亮男声",
    )
    assert resolve_speaker("英语女王") == ("en_female_kat_clone2", "英语女王")


def test_resolve_speaker_by_id() -> None:
    assert resolve_speaker("zh_male_iclvop_xiaolinkepu") == (
        "zh_male_iclvop_xiaolinkepu",
        "清亮男声",
    )


def test_resolve_speaker_unknown_and_empty() -> None:
    assert resolve_speaker("不存在的音色") == (None, None)
    assert resolve_speaker("") == (None, None)
    assert resolve_speaker(None) == (None, None)


# ---------------------------------------------------------------------------
# (b) default_speaker
# ---------------------------------------------------------------------------
def test_default_speaker_defaults() -> None:
    assert default_speaker("zh") == "清亮男声"
    assert default_speaker("en") == "英语女王"
    assert default_speaker("") == "清亮男声"  # 非(en*)都按中文


def test_default_speaker_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_VOICE_ZH", "沉稳解说")
    monkeypatch.setenv("TTS_VOICE_EN", "zh_male_iclvop_xiaolinkepu")
    assert default_speaker("zh") == "沉稳解说"
    assert default_speaker("en") == "zh_male_iclvop_xiaolinkepu"


# ---------------------------------------------------------------------------
# (c) build_atempo_filter
# ---------------------------------------------------------------------------
def test_build_atempo_filter_single_level() -> None:
    assert build_atempo_filter(1.2) == "atempo=1.2"
    assert build_atempo_filter(1.0) == "atempo=1"


def test_build_atempo_filter_splits_out_of_range() -> None:
    assert build_atempo_filter(4.0) == "atempo=2,atempo=2"
    assert build_atempo_filter(0.25) == "atempo=0.5,atempo=0.5"


def test_build_atempo_filter_invalid_speed_raises() -> None:
    for bad in (0, -1.5):
        with pytest.raises(ValueError):
            build_atempo_filter(bad)


# ---------------------------------------------------------------------------
# (d) build_start_task
# ---------------------------------------------------------------------------
def test_build_start_task_message_shape() -> None:
    msg = json.loads(build_start_task("旁白文本", "zh_male_iclvop_xiaolinkepu", "tid1"))
    assert msg["app_id"] == jianying_tts.SAMI_APP_ID == "3704"
    assert msg["appkey"] == jianying_tts.SAMI_APP_KEY == "IZjhUeAYwP"
    assert msg["event"] == "StartTask"
    assert msg["namespace"] == "TTS"
    assert msg["task_id"] == "tid1"
    assert msg["message_id"] == "tid1_0"
    payload = json.loads(msg["payload"])
    assert payload["text"] == "旁白文本"
    assert payload["speaker"] == "zh_male_iclvop_xiaolinkepu"
    assert payload["audio_config"]["format"] == "ogg_opus"
    assert payload["audio_config"]["sample_rate"] == 24000
    # 紧凑分隔符(与参考实现口径一致)
    assert ", " not in msg["payload"]
    assert '", "' not in msg["payload"]


# ---------------------------------------------------------------------------
# (e) read_device_identity
# ---------------------------------------------------------------------------
def test_read_device_identity_from_fake_user_data(tmp_path: Path) -> None:
    (tmp_path / "TTNet").mkdir()
    (tmp_path / "TTNet" / "tt_net_config.config").write_text(
        'a device_id&#123456789 b', encoding="utf-8"
    )
    (tmp_path / "Log").mkdir()
    (tmp_path / "Log" / "a.log").write_text("... iid=987654321 ...", encoding="utf-8")
    assert read_device_identity(tmp_path) == ("123456789", "987654321")


def test_read_device_identity_ttnet_escaped_separator(tmp_path: Path) -> None:
    """TTNet 真实形态:``device_id&#*1941114026465464``(数字前隔着字面
    ``*``;参考实现的 ``&#*(\\d+)`` 匹配不到,这里必须放宽)。"""
    (tmp_path / "TTNet").mkdir()
    (tmp_path / "TTNet" / "tt_net_config.config").write_text(
        'device_id&#*1941114026465464@$*tnc_cmd&#*10000@$*', encoding="utf-8"
    )
    (tmp_path / "Log").mkdir()
    dev, _iid = read_device_identity(tmp_path)
    assert dev == "1941114026465464"


def test_read_device_identity_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMI_TTS_DEVICE_ID", "111")
    monkeypatch.setenv("SAMI_TTS_IID", "222")
    assert read_device_identity(tmp_path) == ("111", "222")


def test_read_device_identity_missing_dir_falls_back(tmp_path: Path) -> None:
    dev, iid = read_device_identity(tmp_path / "nope")
    assert dev == jianying_tts._FALLBACK_DEVICE_ID
    assert iid == jianying_tts._FALLBACK_IID


def test_client_lazy_identity_and_explicit_override() -> None:
    client = JianYingSamiClient(device_id="dev-1", iid="iid-1")
    assert client._ensure_identity() == ("dev-1", "iid-1")


# ---------------------------------------------------------------------------
# (f) synthesize happy path(fake ws + fake ffmpeg)
# ---------------------------------------------------------------------------
def test_synthesize_happy_path(tmp_path: Path) -> None:
    frames = _happy_frames([b"AAAA", b"BBBB"])
    factory = _FakeConnect(frames)
    client = JianYingSamiClient(
        ws_factory=factory,
        ffmpeg_runner=_wav_writer_runner(0.3),
        device_id="dev-9",
        iid="iid-9",
        retries=1,
    )
    out = client.synthesize("旁白一句", voice="清亮男声")

    # ws 协议:url 带 device_id/iid;UA 伪装剪映;消息先 StartTask 后 FinishTask
    assert _FakeConnect.last_url == (
        f"{DEFAULT_WS_URL}?device_id=dev-9&iid=iid-9"
    )
    headers = _FakeConnect.last_kwargs["additional_headers"]
    assert "JianyingPro/5.9.0.11632" in headers["User-Agent"]
    assert "app_id:3704" in headers["User-Agent"]
    sent = factory.ws.sent if factory.ws else []
    assert len(sent) == 2
    start_msg = json.loads(sent[0])
    assert start_msg["event"] == "StartTask"
    assert json.loads(start_msg["payload"])["speaker"] == "zh_male_iclvop_xiaolinkepu"
    assert json.loads(sent[1])["event"] == "FinishTask"

    # 结果:WAV 落盘 + WAV 头时长(0.3s),音色解析信息齐全
    audio = Path(out["audio_path"])
    assert audio.exists() and audio.suffix == ".wav"
    assert abs(out["duration_ms"] - 300) <= 50
    assert out["voice"] == "清亮男声"
    assert out["speaker"] == "zh_male_iclvop_xiaolinkepu"
    assert out["provider"] == PROVIDER == "jianying"
    assert "voice_requested" not in out


def test_synthesize_accepts_speaker_id_form(tmp_path: Path) -> None:
    factory = _FakeConnect(_happy_frames())
    client = JianYingSamiClient(
        ws_factory=factory,
        ffmpeg_runner=_wav_writer_runner(),
        device_id="d",
        iid="i",
        retries=1,
    )
    out = client.synthesize("x", voice="en_female_kat_clone2")
    assert out["speaker"] == "en_female_kat_clone2"
    assert out["voice"] == "英语女王"


def test_synthesize_splits_binary_chunks_before_finish(tmp_path: Path) -> None:
    """TaskFinished 前任意切分的二进制帧都要收集(full 帧序=ogg 字节)。"""
    factory = _FakeConnect(_happy_frames([b"chunk-%d" % i for i in range(5)]))
    client = JianYingSamiClient(
        ws_factory=factory,
        ffmpeg_runner=_wav_writer_runner(),
        device_id="d",
        iid="i",
        retries=1,
    )
    out = client.synthesize("x")
    assert Path(out["audio_path"]).exists()


# ---------------------------------------------------------------------------
# (g) 失败路径
# ---------------------------------------------------------------------------
def test_synthesize_task_failed_raises(tmp_path: Path) -> None:
    from jy_common.tts_client import TTSUnavailable

    frames = [json.dumps({"event": "TaskFailed", "status_text": "bad speaker", "status_code": 4501})]
    client = JianYingSamiClient(
        ws_factory=_FakeConnect(frames),
        ffmpeg_runner=_wav_writer_runner(),
        device_id="d",
        iid="i",
        retries=1,
    )
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("x")
    assert "bad speaker" in str(exc_info.value)
    assert "4501" in str(exc_info.value)


def test_synthesize_recv_timeout_raises(tmp_path: Path) -> None:
    from jy_common.tts_client import TTSUnavailable

    client = JianYingSamiClient(
        ws_factory=_FakeConnect([], hang_after=True),
        ffmpeg_runner=_wav_writer_runner(),
        device_id="d",
        iid="i",
        retries=1,
        recv_timeout_s=0.05,
    )
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("x")
    assert "收帧超时" in str(exc_info.value)


def test_synthesize_no_audio_raises(tmp_path: Path) -> None:
    from jy_common.tts_client import TTSUnavailable

    frames = [json.dumps({"event": "TaskFinished"})]
    client = JianYingSamiClient(
        ws_factory=_FakeConnect(frames),
        ffmpeg_runner=_wav_writer_runner(),
        device_id="d",
        iid="i",
        retries=1,
    )
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("x")
    assert "无音频数据" in str(exc_info.value)


def test_synthesize_connect_error_raises_after_retries(tmp_path: Path) -> None:
    from jy_common.tts_client import TTSUnavailable

    client = JianYingSamiClient(
        ws_factory=_FakeConnect([], connect_error=OSError("conn refused")),
        ffmpeg_runner=_wav_writer_runner(),
        device_id="d",
        iid="i",
        retries=2,
    )
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("x")
    assert "重试 2 次" in str(exc_info.value)


def test_synthesize_ffmpeg_failure_raises(tmp_path: Path) -> None:
    from jy_common.tts_client import TTSUnavailable

    client = JianYingSamiClient(
        ws_factory=_FakeConnect(_happy_frames()),
        ffmpeg_runner=_boom_runner,
        device_id="d",
        iid="i",
        retries=1,
    )
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("x")
    assert "转码失败" in str(exc_info.value)


def test_synthesize_zero_duration_wav_raises(tmp_path: Path) -> None:
    from jy_common.tts_client import TTSUnavailable

    client = JianYingSamiClient(
        ws_factory=_FakeConnect(_happy_frames()),
        ffmpeg_runner=_wav_writer_runner(0.0),
        device_id="d",
        iid="i",
        retries=1,
    )
    with pytest.raises(TTSUnavailable) as exc_info:
        client.synthesize("x")
    assert "时长解析为 0" in str(exc_info.value)


def test_synthesize_empty_text_raises() -> None:
    client = JianYingSamiClient(device_id="d", iid="i")
    from jy_common.tts_client import TTSUnavailable

    with pytest.raises(TTSUnavailable):
        client.synthesize("   ")


# ---------------------------------------------------------------------------
# (h) 重试语义
# ---------------------------------------------------------------------------
def test_synthesize_retry_recovers_on_second_attempt(tmp_path: Path) -> None:
    attempts: list[int] = []

    class _FlakyFactory:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, url: str, **kwargs: Any) -> "_FlakyFactory":
            self.calls += 1
            attempts.append(self.calls)
            if self.calls == 1:
                raise OSError("transient")
            return _FakeConnect(_happy_frames())(url, **kwargs)

    flaky = _FlakyFactory()
    client = JianYingSamiClient(
        ws_factory=flaky,
        ffmpeg_runner=_wav_writer_runner(),
        device_id="d",
        iid="i",
        retries=2,
    )
    out = client.synthesize("x")
    assert flaky.calls == 2
    assert Path(out["audio_path"]).exists()


# ---------------------------------------------------------------------------
# (i) 音色回退 + voice_requested 留痕
# ---------------------------------------------------------------------------
def test_synthesize_unknown_voice_falls_back_to_default(tmp_path: Path) -> None:
    factory = _FakeConnect(_happy_frames())
    client = JianYingSamiClient(
        ws_factory=factory,
        ffmpeg_runner=_wav_writer_runner(),
        device_id="d",
        iid="i",
        retries=1,
    )
    out = client.synthesize("x", voice="胡编的音色")
    assert out["speaker"] == "zh_male_iclvop_xiaolinkepu"  # 默认清亮男声
    assert out["voice"] == "清亮男声"
    assert out["voice_requested"] == "胡编的音色"


def test_synthesize_none_voice_uses_default(tmp_path: Path) -> None:
    factory = _FakeConnect(_happy_frames())
    client = JianYingSamiClient(
        ws_factory=factory,
        ffmpeg_runner=_wav_writer_runner(),
        device_id="d",
        iid="i",
        retries=1,
    )
    out = client.synthesize("x")
    assert out["voice"] == "清亮男声"
    assert "voice_requested" not in out


def test_synthesize_speed_passed_to_atempo(tmp_path: Path) -> None:
    captured: list[list[str]] = []

    def runner(cmd: list[str]) -> None:
        captured.append(cmd)
        _wav_writer_runner()(cmd)

    client = JianYingSamiClient(
        ws_factory=_FakeConnect(_happy_frames()),
        ffmpeg_runner=runner,
        device_id="d",
        iid="i",
        retries=1,
    )
    client.synthesize("x", speed=1.5)
    assert "atempo=1.5" in captured[0]
    client.synthesize("x", speed="not-a-number")  # 非法 speed → None → 不变速
    assert all("atempo" not in " ".join(cmd) for cmd in captured[1:])


# ---------------------------------------------------------------------------
# (j) CSV 音色表资产完整性
# ---------------------------------------------------------------------------
def test_speaker_csv_has_popular_voices() -> None:
    by_id, by_name = jianying_tts._load_speakers()
    assert len(by_id) >= 100  # 全表 163 条;防资产被误删/截断
    assert "zh_male_iclvop_xiaolinkepu" in by_id  # 清亮男声(截图1选中)
    assert "en_female_kat_clone2" in by_id  # 英语女王(表内唯一英文音色)
    assert by_name["清亮男声"] == "zh_male_iclvop_xiaolinkepu"


# ---------------------------------------------------------------------------
# (k) tts_client 后端分派
# ---------------------------------------------------------------------------
def test_tts_client_dispatch_jianying(monkeypatch: pytest.MonkeyPatch) -> None:
    import jy_common.tts_client as tts_mod
    from jy_common.tts_client import MockTTSClient, get_active_backend, set_default_client

    monkeypatch.setenv("TTS_BACKEND", "jianying")
    assert isinstance(tts_mod._resolve_default_client(), JianYingSamiClient)
    monkeypatch.setenv("TTS_BACKEND", "sami")
    assert isinstance(tts_mod._resolve_default_client(), JianYingSamiClient)

    set_default_client(JianYingSamiClient(device_id="d", iid="i"))
    assert get_active_backend() == "jianying"
    set_default_client(MockTTSClient())
    assert get_active_backend() == "mock"


# ---------------------------------------------------------------------------
# (l) speech_synthesize 别名分派
# ---------------------------------------------------------------------------
class _FakeSamiClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def synthesize(self, text, *, voice=None, speed=None):
        self.calls.append({"text": text, "voice": voice, "speed": speed})
        return {
            "audio_path": "/tmp/fake.wav",
            "duration_ms": 800,
            "voice": "清亮男声",
            "speaker": "zh_male_iclvop_xiaolinkepu",
            "provider": "jianying",
        }


def test_speech_synthesize_sami_alias_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    from assembly_capabilities.run_context import RunContext
    from video_edit_capabilities import speech_synthesize as ss_mod

    fake = _FakeSamiClient()
    monkeypatch.setattr(ss_mod, "_get_sami_client", lambda: fake)
    out = ss_mod.speech_synthesize(
        {"text": "hi", "voice": "清亮男声", "preferred_provider": "sami", "retries": 0},
        RunContext(),
    )
    assert not out.text.startswith("[ERROR]"), out.text
    assert out.data["selected_provider"] == "jianying"
    assert out.data["speaker"] == "zh_male_iclvop_xiaolinkepu"
    assert out.data["resolved_voice"] == "清亮男声"
    assert fake.calls[0]["voice"] == "清亮男声"


def test_speech_synthesize_auto_follows_tts_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assembly_capabilities.run_context import RunContext
    from video_edit_capabilities import speech_synthesize as ss_mod

    fake = _FakeSamiClient()
    monkeypatch.setattr(ss_mod, "_get_sami_client", lambda: fake)
    monkeypatch.setenv("TTS_BACKEND", "jianying")
    out = ss_mod.speech_synthesize({"text": "hi", "retries": 0}, RunContext())
    assert out.data["selected_provider"] == "jianying"
    assert len(fake.calls) == 1  # auto → sami client,未走 firered
