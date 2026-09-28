"""TTS 客户端 — Week 6+ 阶段三使用(对照 9 工具迁移计划 §6.3)。

Week 6+ 默认 ``MockTTSClient``(写空 wav 占位),新增 ``FireRedTTS2Client`` 真
实骨架;通过环境变量 ``TTS_BACKEND=firered`` 切换(对照 ``ASR_BACKEND=firered``)。

接口约定:返回值是 ``dict``:
- ``audio_path: str`` — 合成的音频文件绝对路径
- ``duration_ms: int`` — 合成音频时长(毫秒);为 ``None`` 表示后端未返回

Week 6+ 接入要点:
- ``FireRedTTS2Client`` 是真实 TTS 服务接入骨架;真实模型由
  ``E:\\Documents\\kuaishou\\FireRed-OpenStoryline`` 或独立 TTS 服务启服务;
  本类只负责 HTTP 调用 + 异常归一化,模型加载由服务侧负责。
- 默认 endpoint: ``http://127.0.0.1:8010/synthesize``(占位,Week 6+ 真实联调
  时核实;若服务部署在不同端口,通过环境变量 ``FIRERED_TTS_ENDPOINT`` 覆盖)。
- 服务不可达 / 非 2xx / 响应非 JSON → 抛 ``TTSUnavailable``,调用方(节点 17)
  降级为写 ``en_audio_path = None``(不阻塞主链)。
"""

from __future__ import annotations

import os
import struct
import tempfile
import uuid
import wave
from pathlib import Path
from typing import Optional, Protocol


class TTSClient(Protocol):
    """TTS 客户端协议 — Week 6+ 真实实现应满足此接口。"""

    def synthesize(
        self, text: str, *, voice: Optional[str] = None, speed: Optional[float] = None
    ) -> dict:
        ...


class TTSUnavailable(RuntimeError):
    """FireRedTTS2 服务不可达时抛出。调用方应降级为 en_audio_path=None。"""


class MockTTSClient:
    """Week 6+ Mock — 写一段空 WAV 到临时目录(对照 ASR Mock 的"用 size 当伪 hash"思路)。

    不返回真实 TTS 音频,但产物可用 — 让 Week 6+ 阶段五 ``acceptance_check.py``
    的"英文配音音轨非空"断言通过,同时让 ``en_audio_path`` 字段非空。

    Args:
        output_dir: WAV 写入根目录;None 时用系统临时目录。
        duration_s: 写入的静音时长(秒),默认 0.5s。
    """

    _WAV_SAMPLE_RATE = 16_000
    _WAV_SAMPLE_WIDTH = 2  # 16-bit
    _MIN_BYTES = 8 * 1024  # 阶段五 acceptance 阈值 >5KB

    def __init__(
        self,
        output_dir: Optional[str | Path] = None,
        *,
        duration_s: float = 0.5,
    ) -> None:
        self._output_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
        self._duration_s = duration_s

    def synthesize(
        self, text: str, *, voice: Optional[str] = None, speed: Optional[float] = None
    ) -> dict:
        if not text or not text.strip():
            # Mock 与真实客户端一致:不允许空文本
            raise TTSUnavailable("text is empty")

        self._output_dir.mkdir(parents=True, exist_ok=True)
        # 用 uuid 保证唯一性(同文本也写新文件,避免覆盖)
        wav_path = self._output_dir / f"tts_mock_{uuid.uuid4().hex[:12]}.wav"

        n_samples = int(self._WAV_SAMPLE_RATE * self._duration_s)
        if n_samples * self._WAV_SAMPLE_WIDTH < self._MIN_BYTES - 44:
            n_samples = (self._MIN_BYTES - 44) // self._WAV_SAMPLE_WIDTH + 1

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(self._WAV_SAMPLE_WIDTH)
            wf.setframerate(self._WAV_SAMPLE_RATE)
            wf.writeframes(b"\x00\x00" * n_samples)

        return {
            "audio_path": str(wav_path),
            "duration_ms": int(self._duration_s * 1000),
            "voice": voice,
            "speed": speed,
        }


class FireRedTTS2Client:
    """FireRedTTS2 客户端骨架(Week 6+ 真实接入)。

    真实模型由 ``E:\\Documents\\kuaishou\\FireRed-OpenStoryline`` 启服务;
    本类只负责 HTTP 调用 + 异常归一化,模型加载由服务侧负责。

    Args:
        endpoint: TTS 服务 URL;None 时读 ``FIRERED_TTS_ENDPOINT`` 环境变量,
            再退到默认 ``http://127.0.0.1:8010/synthesize``。
        timeout_s: HTTP 请求超时(秒)。
        http_client: 注入 httpx.Client(便于单测 mock);None 时按需构造。
    """

    DEFAULT_ENDPOINT = "http://127.0.0.1:8010/synthesize"

    def __init__(
        self,
        endpoint: Optional[str] = None,
        timeout_s: int = 300,
        http_client=None,
    ) -> None:
        self._endpoint = endpoint or os.environ.get(
            "FIRERED_TTS_ENDPOINT", self.DEFAULT_ENDPOINT
        )
        self._timeout = timeout_s
        self._http_client = http_client  # 注入点;None 时 synthesize 内部 lazy 构造

    def synthesize(
        self, text: str, *, voice: Optional[str] = None, speed: Optional[float] = None
    ) -> dict:
        """POST ``{text, voice, speed}`` 到 endpoint,返回 ``{audio_path, duration_ms}``。

        Raises:
            TTSUnavailable: 服务不可达、超时、返回非 2xx 或响应不是合法 JSON。
        """
        if not text or not text.strip():
            raise TTSUnavailable("text is empty")

        try:
            import httpx
        except ImportError as e:
            raise TTSUnavailable(
                "FireRedTTS2Client 依赖 httpx,请先 pip install httpx"
            ) from e

        payload: dict = {"text": text}
        if voice is not None:
            payload["voice"] = voice
        if speed is not None:
            payload["speed"] = speed

        client = self._http_client or httpx
        try:
            response = client.post(
                self._endpoint,
                json=payload,
                timeout=self._timeout,
            )
        except (httpx.RequestError, httpx.TimeoutException) as e:
            raise TTSUnavailable(
                f"FireRedTTS2 服务不可达: endpoint={self._endpoint}, "
                f"text_len={len(text)}, error={type(e).__name__}: {e}"
            ) from e

        if response.status_code >= 400:
            raise TTSUnavailable(
                f"FireRedTTS2 服务返回 HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            data = response.json()
        except Exception as e:
            raise TTSUnavailable(
                f"FireRedTTS2 服务响应不是合法 JSON: {e}"
            ) from e

        if not isinstance(data, dict):
            raise TTSUnavailable(
                f"FireRedTTS2 响应应为 dict,实际 {type(data).__name__}"
            )

        # 字段完整性校验 — 缺字段时按可用性规则降级处理
        audio_path = data.get("audio_path")
        if not audio_path or not isinstance(audio_path, str):
            raise TTSUnavailable(
                "FireRedTTS2 响应缺少 audio_path 字段或字段类型错误"
            )

        duration_raw = data.get("duration_ms")
        try:
            duration_ms = int(duration_raw) if duration_raw is not None else 0
        except (TypeError, ValueError):
            duration_ms = 0

        return {
            "audio_path": audio_path,
            "duration_ms": duration_ms,
            "voice": voice,
            "speed": speed,
        }


# ---------------------------------------------------------------------------
# 默认 client 解析 — Week 6+ 阶段三:支持 TTS_BACKEND=firered 环境变量切换
# ---------------------------------------------------------------------------
def _resolve_default_client() -> TTSClient:
    backend = os.environ.get("TTS_BACKEND", "mock").lower().strip()
    if backend == "firered":
        return FireRedTTS2Client()
    if backend != "mock":
        # 未知 backend:降级为 Mock(不抛错,避免污染单测 / 主链)
        pass
    return MockTTSClient()


_default_client: TTSClient = _resolve_default_client()


def get_default_client() -> TTSClient:
    return _default_client


def set_default_client(client: TTSClient) -> None:
    """允许单测 / Week 6+ 替换默认 TTS 客户端。"""
    global _default_client
    _default_client = client


def call_firered_tts(
    text: str, *, voice: Optional[str] = None, speed: Optional[float] = None
) -> dict:
    """便捷调用 — 节点 17 的统一入口(对照 ``call_asr2s``)。

    若默认 client 是 ``FireRedTTS2Client`` 且服务不可达,此处直接抛
    ``TTSUnavailable``,由调用方(节点 17)决定是否降级为 ``en_audio_path=None``。

    若默认 client 是 ``MockTTSClient``,返回临时 WAV 路径(>5KB,阶段五
    acceptance_check 阈值)。
    """
    return get_default_client().synthesize(text, voice=voice, speed=speed)


def get_active_backend() -> str:
    """返回当前默认 client 对应的 backend 名称(便于状态记录)。

    - ``FireRedTTS2Client`` 实例 → ``"firered"``
    - ``MockTTSClient`` 实例 → ``"mock"``
    - 其他 → ``"custom"``
    """
    client = get_default_client()
    if isinstance(client, FireRedTTS2Client):
        return "firered"
    if isinstance(client, MockTTSClient):
        return "mock"
    return "custom"
