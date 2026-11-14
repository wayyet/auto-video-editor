"""``speech_synthesize`` + ``tts_generate`` — 9 工具迁移 §6.3。

骨架搬自 ``video-agent-kit 0.4.3 mcp/ve_tools/tts.py``(函数体 46-119 行),
原 ``call_tts_provider()`` 三选一分支已删;2026-11-14 恢复**双后端分派**:
``firered``(本地 FireRed :8010)与 ``jianying``(剪映朗读音色 SAMI 云端,
``jy_common/jianying_tts.py`` —— FireRed 本机从未部署后的默认后端)。

接口约定(对照 video-agent-kit):
- 输入 ``args: dict``,key 见下文
- 输出 ``ToolResult``(``assembly_capabilities.result.ToolResult``)
- ``ToolResult.data["audio_path"]``:合成音频文件绝对路径(成功时)
- ``ToolResult.data["duration_ms"]``:合成音频时长(成功时)
- ``ToolResult.text`` 以 ``[ERROR]`` 开头表示失败

迁移要点:
- ``RunContext`` 使用本仓库精简版(``assembly_capabilities.run_context``),
  没有 ``clean_env`` / ``check_endpoint``,所以环境变量读取直接用
  ``os.environ.get``,**不**走 ``clean_env`` 链
- 双 backend:``preferred_provider`` 保留解析以兼容旧调用方代码;``auto``
  跟随 ``TTS_BACKEND`` env,其余别名见 ``_KNOWN_PROVIDER_ALIASES``,未知名
  直接报 ``[ERROR] unknown preferred_provider``
- 重试退避:``retry_backoff_seconds`` * 2^attempt,默认 3.0(对照
  ``TTS_RETRY_BACKOFF_SECONDS=3.0``)
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext
from jy_common.tts_client import TTSUnavailable, call_firered_tts


TTS_DEFAULT_RETRIES = 3
TTS_MAX_RETRIES = 10
TTS_RETRY_BACKOFF_SECONDS = 3.0

#: preferred_provider 别名 → 后端(``auto`` 不在此表:跟随 TTS_BACKEND env)
_KNOWN_PROVIDER_ALIASES: dict[str, str] = {
    "cloud": "firered",
    "cloud_tts": "firered",
    "remote": "firered",
    "speech": "firered",
    "firered": "firered",
    "jianying": "jianying",
    "sami": "jianying",
}

# 剪映 SAMI 客户端单例(构造后复用 device_id/iid 解析结果)
_SAMI_CLIENT: Any = None


def _get_sami_client() -> Any:
    """懒构造剪映 SAMI 客户端(模块导入期不做文件 IO / 网络探测)。"""
    global _SAMI_CLIENT
    if _SAMI_CLIENT is None:
        from jy_common.jianying_tts import JianYingSamiClient

        _SAMI_CLIENT = JianYingSamiClient()
    return _SAMI_CLIENT


def _resolve_backend(preferred: str) -> str:
    """``preferred_provider`` → 实际后端(``"firered"`` / ``"jianying"``)。

    ``auto`` 跟随 ``TTS_BACKEND`` env(jianying/sami → 剪映,其余 firered)。
    """
    if preferred == "auto":
        backend = os.environ.get("TTS_BACKEND", "mock").lower().strip()
        return "jianying" if backend in ("jianying", "sami") else "firered"
    return _KNOWN_PROVIDER_ALIASES.get(preferred, "firered")


# ---------------------------------------------------------------------------
# 对外入口:tts_generate 是 speech_synthesize 的兼容性转发
# ---------------------------------------------------------------------------
def tts_generate(args: dict, ctx: RunContext) -> ToolResult:
    """Compatibility wrapper — 真实 TTS 走 speech_synthesize。

    仅 1 行转发,不复刻任何独立逻辑(对照 video-agent-kit
    ``tts.py:46-48``)。
    """
    return speech_synthesize(args, ctx)


# ---------------------------------------------------------------------------
# 主入口:speech_synthesize
# ---------------------------------------------------------------------------
def speech_synthesize(args: dict, ctx: RunContext) -> ToolResult:
    """合成 ``args["text"]`` 为语音文件;失败时返回 ``[ERROR]``。

    Args:
        args: 必填 ``text: str``,可选 ``voice: str`` / ``speed: float`` /
            ``retries: int`` / ``retry_backoff_seconds: float`` /
            ``preferred_provider: str``(``auto`` 跟随 TTS_BACKEND env;
            别名见 ``_KNOWN_PROVIDER_ALIASES``:firered/jianying/sami/...)。
        ctx: 精简版 ``RunContext``,本函数未使用,保留仅为签名兼容。

    Returns:
        ``ToolResult``。成功时 ``data["audio_path"]`` 是音频路径,
        ``data["duration_ms"]`` 是时长;失败时 ``text`` 以 ``[ERROR]`` 开头。
    """
    # ---- 入参校验 ----
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return ToolResult(text="[ERROR] text is required")
    text = text.strip()

    speed_value = args.get("speed")
    speed: float | None = None
    if speed_value is not None:
        speed = coerce_finite_number(speed_value)
        if speed is None:
            return ToolResult(text="[ERROR] speed must be numeric")
        if speed <= 0:
            return ToolResult(text="[ERROR] speed must be a finite number > 0")

    voice_value = args.get("voice")
    voice: str | None = None
    if voice_value is not None:
        voice_str = str(voice_value).strip()
        if voice_str:
            voice = voice_str

    preferred = str(args.get("preferred_provider") or "auto").strip().lower()
    if preferred != "auto" and preferred not in _KNOWN_PROVIDER_ALIASES:
        return ToolResult(
            text=f"[ERROR] unknown preferred_provider: {preferred}",
            data={"param_error": True},
        )

    retries_result = coerce_retry_count(args.get("retries"))
    if isinstance(retries_result, ToolResult):
        return retries_result
    retries = retries_result

    backoff_value = args.get("retry_backoff_seconds", TTS_RETRY_BACKOFF_SECONDS)
    backoff = coerce_finite_number(backoff_value)
    if backoff is None or backoff < 0:
        return ToolResult(
            text="[ERROR] retry_backoff_seconds must be a finite number >= 0",
            data={"param_error": True},
        )

    # ---- 重试循环 ----
    backend = _resolve_backend(preferred)
    started = time.time()
    last_error_text = "[ERROR] speech_synthesize failed"
    for attempt in range(retries + 1):
        try:
            if backend == "jianying":
                result = _get_sami_client().synthesize(
                    text, voice=voice, speed=speed
                )
            else:
                result = call_firered_tts(text, voice=voice, speed=speed)
        except TTSUnavailable as exc:
            last_error_text = f"[ERROR] {backend} TTS failed: {exc}"
            # 不区分 param_error:本函数入参已全部校验,这里都是 transient
            if attempt >= retries:
                break
            time.sleep(float(backoff) * (2 ** attempt))
            continue

        # 成功
        data: dict[str, Any] = {
            "audio_path": result.get("audio_path"),
            "duration_ms": result.get("duration_ms", 0),
            "selected_provider": backend,
            "retry_attempts": attempt,
            "max_retries": retries,
            "elapsed_seconds": round(time.time() - started, 3),
        }
        if voice is not None:
            data["voice"] = voice
        if speed is not None:
            data["speed"] = speed
        if backend == "jianying":
            # 剪映音色解析结果透出(voice=显示名,speaker=speaker_id;
            # 请求音色未知回落默认时客户端会另记 voice_requested)
            data["speaker"] = result.get("speaker")
            resolved_voice = result.get("voice")
            if resolved_voice:
                data["resolved_voice"] = resolved_voice
        return ToolResult(
            text=f"{backend} TTS synthesized {len(text)} chars in {data['elapsed_seconds']}s "
            f"({data['retry_attempts'] + 1} attempt(s))",
            data=data,
        )

    # 所有重试耗尽
    return ToolResult(
        text="[ERROR] all TTS attempts failed: " + last_error_text,
        data={
            "selected_provider": backend,
            "max_retries": retries,
            "failures": [{"attempt": i + 1, "error": last_error_text} for i in range(retries + 1)],
            "elapsed_seconds": round(time.time() - started, 3),
        },
    )


# ---------------------------------------------------------------------------
# Helpers(原 tts.py 私有,本仓库保留为模块级以便单测覆盖)
# ---------------------------------------------------------------------------
def coerce_finite_number(value: object) -> float | None:
    """``value`` → ``float``;若不可解析 / bool / inf / nan → ``None``。"""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def coerce_retry_count(value: object) -> int | ToolResult:
    """``value`` → ``int`` 形式的 retries;非法 → ``ToolResult([ERROR])``。"""
    if value is None:
        return TTS_DEFAULT_RETRIES
    if isinstance(value, bool):
        return ToolResult(text="[ERROR] retries must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return ToolResult(text="[ERROR] retries must be an integer")
    try:
        raw = float(value)
    except (TypeError, ValueError):
        raw = float(parsed)
    if not math.isfinite(raw) or raw != parsed:
        return ToolResult(text="[ERROR] retries must be an integer")
    if parsed < 0 or parsed > TTS_MAX_RETRIES:
        return ToolResult(text=f"[ERROR] retries must be in [0, {TTS_MAX_RETRIES}]")
    return parsed
