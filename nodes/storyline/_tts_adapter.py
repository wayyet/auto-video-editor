"""P3 TTS 适配器:storyline TTSClient 协议 → jy_common FireRed 后端。

对应《第三期开发/相似功能节点分析与设计执行计划.md》§5.4 P3:

- 实现 ``storyline_capabilities.tts_runner.TTSClient`` 协议,内部转调
  ``jy_common.tts_client.call_firered_tts``(与 node_17 英文配音同一后端);
- 失败语义对齐 ``StubTTSClient``:不抛异常,写 ``TTSResult.error``;
- ``duration_ms`` 以 **WAV 头**为准(``read_wav_duration_ms``),头解析失败
  回落服务返回值;
- FireRed 服务把音频写在它返回的 ``audio_path``(本机服务,默认 :8010),
  适配器负责复制到协议要求的 ``wav_path``;
- 装配:``install_tts_client_from_env()`` 仅在 ``TTS_BACKEND=firered`` 时
  安装本适配器,否则回落 lazy ``StubTTSClient`` —— 默认行为不变。

本模块同时 import ``jy_common`` 与 ``storyline_capabilities``(均在节点层
允许的依赖内);两者都只依赖 stdlib,无循环导入。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from jy_common.tts_client import call_firered_tts
from storyline_capabilities.tts_runner import TTSResult, read_wav_duration_ms

_PROVIDER_NAME = "firered"


class StorylineFireRedTTSClient:
    """storyline ``TTSClient`` 协议的 FireRed 适配器(jy_common dict → TTSResult)。

    ``params`` 由 LLM 推断(schema 见 ``generate_voiceover._infer_tts_params``),
    这里只取 firered 支持的 ``voice`` / ``speed`` 并做防御性类型对齐;其余键
    原样保留在 ``params_used`` 里便于日志排查。
    """

    def synthesize(
        self, *, text: str, wav_path: Path, params: dict[str, Any]
    ) -> TTSResult:
        params_used = dict(params or {})
        wav_path = Path(wav_path)
        voice = str(params_used["voice"]) if params_used.get("voice") else None
        try:
            speed = (
                float(params_used["speed"])
                if params_used.get("speed") is not None
                else None
            )
        except (TypeError, ValueError):
            speed = None

        try:
            result = call_firered_tts(text, voice=voice, speed=speed)
        except Exception as e:  # noqa: BLE001 — TTSUnavailable/网络/解析错误一律降级
            return TTSResult(
                wav_path=wav_path,
                duration_ms=0,
                provider=_PROVIDER_NAME,
                error=f"firered tts failed: {e!r}",
                params_used=params_used,
            )

        audio_path_raw = str(result.get("audio_path") or "")
        audio_path = Path(audio_path_raw) if audio_path_raw else None
        if audio_path is None or not audio_path.is_file():
            return TTSResult(
                wav_path=wav_path,
                duration_ms=0,
                provider=_PROVIDER_NAME,
                error=f"firered audio_path missing or not a file: {result.get('audio_path')!r}",
                params_used=params_used,
            )

        # 服务写好的音频 → 复制到协议要求的 wav_path(本机服务,路径可直达)
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if audio_path.resolve() != wav_path.resolve():
                shutil.copyfile(audio_path, wav_path)
        except OSError as e:
            return TTSResult(
                wav_path=wav_path,
                duration_ms=0,
                provider=_PROVIDER_NAME,
                error=f"wav copy failed: {e!r}",
                params_used=params_used,
            )

        # WAV 头为准;头解析失败(非 PCM 等)回落服务返回值,再不行 0。
        duration_ms = read_wav_duration_ms(wav_path)
        if duration_ms <= 0:
            duration_ms = int(result.get("duration_ms") or 0)
        return TTSResult(
            wav_path=wav_path,
            duration_ms=duration_ms,
            provider=_PROVIDER_NAME,
            params_used=params_used,
        )


def install_tts_client_from_env() -> str:
    """按 ``TTS_BACKEND`` 安装 storyline 缺省 TTS 客户端(P3 §5.4)。

    ``firered`` → 安装 ``StorylineFireRedTTSClient``;其他/未设 → ``None``
    (回落 ``get_default_tts_client`` 的 lazy ``StubTTSClient``,默认不变)。

    Returns:
        实际生效的 backend 名(``"firered"`` / ``"stub"``),便于状态日志。
    """
    from storyline_capabilities.tts_runner import set_default_tts_client

    backend = os.environ.get("TTS_BACKEND", "mock").lower().strip()
    if backend == "firered":
        set_default_tts_client(StorylineFireRedTTSClient())
        return "firered"
    set_default_tts_client(None)
    return "stub"
