"""node_17_inject_english_tts — 9 工具迁移 §6.3 阶段三。

Week 5 stub 节点 ``node_17_inject_english_tts_stub`` 升级为真实 TTS 调用,
改名为 ``node_17_inject_english_tts``(对照 9 工具迁移 §6.4 graph.py rename)。

行为:
1. 从 ``state["subtitle_segments_en"]`` 读英文字幕段,拼成一段文本
2. 调 ``speech_synthesize({"text": text, "preferred_provider": "firered",
   "retries": 3}, ctx)``(对照 9 工具迁移 §6.3 step 5)
3. 成功 → ``en_audio_path`` = 音频路径(同步写 ``en_dub_audio_path``)
4. 失败 → ``en_audio_path = None``、``error_log`` 追加一条,**不阻塞主链**

失败不写静音 WAV 占位(Week 5 stub 的行为)— 主链 ``join_before_delivery``
之后 ``en_audio_path = None`` 是合法状态,阶段六联调由人工决策是否需要补
音频。Week 6+ 真实联调时若需要占位可在调用方统一处理。

Week 5 字段命名约定保留:同时写 ``en_audio_path``(新,Week 5 验收读)与
``en_dub_audio_path``(旧,Week 4 已写),两个字段值相同,保证兼容。
"""

from __future__ import annotations

import uuid
from typing import Any

from assembly_capabilities.run_context import RunContext
from state import WorkflowState
from video_edit_capabilities.speech_synthesize import speech_synthesize


# TTS 调用固定参数 — 9 工具迁移 §6.3 step 5 规定
_TTS_PREFERRED_PROVIDER = "firered"
_TTS_RETRIES = 3
_TTS_DEFAULT_SPEED: float | None = None
_TTS_DEFAULT_VOICE: str | None = None


def _join_segments_text(segments_en: list[dict[str, Any]]) -> str:
    """``subtitle_segments_en`` → 单段文本,用空格拼接。

    对照 9 工具迁移 §6.3 step 5:
    ``" ".join(s.get("text_en", "") for s in segments_en)``
    """
    parts: list[str] = []
    for seg in segments_en or []:
        text = str(seg.get("text_en") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def node_17_inject_english_tts(state: WorkflowState) -> dict:
    """LangGraph 节点(Week 6+):真实调 TTS → 写 ``en_audio_path`` / ``en_dub_audio_path``。

    只返回变更字段,避免 fan-in 时与其他分支并发写同一字段。失败时
    ``en_audio_path = None`` + ``error_log`` 追加一条,但不返回
    ``interrupt``,允许主链继续到 ``join_before_delivery``。

    Args:
        state: 需含 ``subtitle_segments_en``(list),其他字段为可选。

    Returns:
        dict,只含变更字段:
        - ``en_audio_path``: 合成音频路径(失败为 ``None``)
        - ``en_dub_audio_path``: 同上(Week 4 兼容字段)
        - ``tts_run_id``: 本次调用的 uuid 短码(状态审计用)
        - ``tts_issue``: 失败原因(成功为 ``None``,便于状态审计,不阻塞)
        - ``status_log`` / ``error_log``: delta-only
    """
    segments_en = list(state.get("subtitle_segments_en") or [])
    text = _join_segments_text(segments_en)

    if not text:
        # 没有可合成文本(空字幕段 / 缺字段)→ 不阻塞,直接返回 None
        return {
            "en_audio_path": None,
            "en_dub_audio_path": None,
            "tts_run_id": uuid.uuid4().hex[:12],
            "tts_issue": "empty_text: no text_en segments to synthesize",
            "status_log": ["node_17_inject_english_tts_done"],
        }

    ctx = RunContext()
    result = speech_synthesize(
        {
            "text": text,
            "voice": _TTS_DEFAULT_VOICE,
            "speed": _TTS_DEFAULT_SPEED,
            "preferred_provider": _TTS_PREFERRED_PROVIDER,
            "retries": _TTS_RETRIES,
        },
        ctx,
    )

    if result.text.startswith("[ERROR]"):
        # 失败 → 不阻塞主链,error_log 追加一条 + en_audio_path = None
        issue = result.text
        return {
            "en_audio_path": None,
            "en_dub_audio_path": None,
            "tts_run_id": uuid.uuid4().hex[:12],
            "tts_issue": issue,
            "error_log": [f"[node_17] TTS failed: {issue}"],
            "status_log": ["node_17_inject_english_tts_done"],
        }

    # 成功 → 写 en_audio_path + en_dub_audio_path(双写兼容)
    audio_path = result.data.get("audio_path")
    return {
        "en_audio_path": audio_path,
        "en_dub_audio_path": audio_path,
        "tts_run_id": uuid.uuid4().hex[:12],
        "tts_issue": None,
        "status_log": ["node_17_inject_english_tts_done"],
    }
