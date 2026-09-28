"""``speech_synthesize`` + ``tts_generate`` — 9 工具迁移 §6.3。

骨架搬自 ``video-agent-kit 0.4.3 mcp/ve_tools/tts.py``(函数体 46-119 行),
**已删除**原 ``call_tts_provider()`` 三选一分支
(``remote_speech_synthesize`` / ``zcode_official_speech_synthesize`` /
``cloud_tts_tts``),改为唯一一条 ``call_firered_tts`` 直连(对照
``call_asr2s``)。

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
- 单 backend:``preferred_provider`` / ``allowed_providers`` 入参保留解析
  以兼容旧调用方代码,但实际不再分派,只接受 ``cloud_tts`` / ``firered`` /
  ``auto``,其他直接报 ``[ERROR] unknown preferred_provider``
- 重试退避:``retry_backoff_seconds`` * 2^attempt,默认 3.0(对照
  ``TTS_RETRY_BACKOFF_SECONDS=3.0``)
"""

from __future__ import annotations

import math
import time
from typing import Any

from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext
from jy_common.tts_client import TTSUnavailable, call_firered_tts


TTS_DEFAULT_RETRIES = 3
TTS_MAX_RETRIES = 10
TTS_RETRY_BACKOFF_SECONDS = 3.0

# 单 backend:仅识别这些 preferred_provider 名称(其他视为 [ERROR])
_KNOWN_PROVIDER_ALIASES: dict[str, str] = {
    "auto": "firered",
    "cloud": "firered",
    "cloud_tts": "firered",
    "remote": "firered",
    "speech": "firered",
    "firered": "firered",
}


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
            ``preferred_provider: str``(只为兼容旧调用方,实际只接受
            ``firered`` / ``cloud_tts`` / ``auto`` 之一)。
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
    if preferred not in _KNOWN_PROVIDER_ALIASES:
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
    started = time.time()
    last_error_text = "[ERROR] speech_synthesize failed"
    for attempt in range(retries + 1):
        try:
            result = call_firered_tts(text, voice=voice, speed=speed)
        except TTSUnavailable as exc:
            last_error_text = f"[ERROR] firered TTS failed: {exc}"
            # 不区分 param_error:本函数入参已全部校验,这里都是 transient
            if attempt >= retries:
                break
            time.sleep(float(backoff) * (2 ** attempt))
            continue

        # 成功
        data: dict[str, Any] = {
            "audio_path": result.get("audio_path"),
            "duration_ms": result.get("duration_ms", 0),
            "selected_provider": "firered",
            "retry_attempts": attempt,
            "max_retries": retries,
            "elapsed_seconds": round(time.time() - started, 3),
        }
        if voice is not None:
            data["voice"] = voice
        if speed is not None:
            data["speed"] = speed
        return ToolResult(
            text=f"firered TTS synthesized {len(text)} chars in {data['elapsed_seconds']}s "
            f"({data['retry_attempts'] + 1} attempt(s))",
            data=data,
        )

    # 所有重试耗尽
    return ToolResult(
        text="[ERROR] all TTS attempts failed: " + last_error_text,
        data={
            "selected_provider": "firered",
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
