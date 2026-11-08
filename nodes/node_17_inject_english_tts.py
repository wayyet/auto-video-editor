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

P3b(2026-11-08):成功路径把英文配音写进**英文分支草稿** —— materials.audios
的 ``en-dub-1`` 素材 + ``en_voiceover`` 标记音轨(时长 clamp 到草稿总时长)。
英文草稿缺失/不可读/时长未知时降级记 error_log,不阻塞主链;fork 早退
(``draft_dir_en_branch`` 缺失)静默跳过。
"""

from __future__ import annotations

import uuid
from typing import Any

from assembly_capabilities.run_context import RunContext
from nodes._draft_io import load_draft, upsert_by_id, write_draft
from nodes._dual_draft import (
    EN_VOICEOVER_TRACK_NAME,
    en_draft_file,
    missing_en_note,
)
from state import WorkflowState
from video_edit_capabilities.speech_synthesize import speech_synthesize


# TTS 调用固定参数 — 9 工具迁移 §6.3 step 5 规定
_TTS_PREFERRED_PROVIDER = "firered"
_TTS_RETRIES = 3
_TTS_DEFAULT_SPEED: float | None = None
_TTS_DEFAULT_VOICE: str | None = None

# P3b:英文配音在英文分支草稿里的固定 id(segment/track/material)
_EN_DUB_MATERIAL_ID = "en-dub-1"
_EN_DUB_SEGMENT_ID = "segment-en-dub-1"
_EN_DUB_TRACK_ID = "track-audio-en-dub"
# 成功写轨的状态标记(status_log);其余说明走 error_log
_EN_VOICEOVER_WRITTEN_TAG = "node_17_en_voiceover_written"


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


def _write_en_voiceover_track(
    state: WorkflowState, audio_path: str, duration_ms: int
) -> list[str]:
    """P3b:把英文配音写进英文分支草稿(material + 音轨),返回说明列表。

    - 标记轨 ``name=EN_VOICEOVER_TRACK_NAME``(track 与 segment 同名),
      node_12 镜像按此标记保留;先移除同名旧轨,重放幂等。
    - segment 用原生 ``target_timerange``(微秒),时长 clamp 到草稿总时长。
    - 失败一律降级通过:返回的说明由调用方分流到 status_log / error_log,
      不抛异常、不阻塞主链。
    """
    notes: list[str] = []

    # fork 早退(从未走英文分支)→ 静默跳过,与 node_12 口径一致
    if not state.get("draft_dir_en_branch"):
        return notes

    en_file = en_draft_file(state)
    if en_file is None:
        notes.append(f"[node_17] {missing_en_note(state)}")
        return notes

    duration_ms = int(duration_ms or 0)
    if duration_ms <= 0:
        notes.append(
            f"[node_17] duration_ms 未知({duration_ms!r}),跳过写英文配音轨"
        )
        return notes

    try:
        en_draft = load_draft(en_file)
    except (FileNotFoundError, ValueError) as exc:
        notes.append(f"[node_17] 读取英文草稿失败,配音未写入草稿: {exc}")
        return notes

    draft_dur_us = int(en_draft.get("duration") or 0)
    duration_us = duration_ms * 1000
    if draft_dur_us > 0:
        duration_us = min(duration_us, draft_dur_us)

    # 1) audio material(id 幂等 upsert;失败/重放都不会翻倍)
    en_materials = en_draft.setdefault("materials", {})
    existing_audios = en_materials.setdefault("audios", [])
    upsert_by_id(
        existing_audios,
        {
            "id": _EN_DUB_MATERIAL_ID,
            "type": "audio",
            "name": EN_VOICEOVER_TRACK_NAME,
            "path": str(audio_path),
            "duration_ms": duration_ms,
        },
    )

    # 2) 音轨:先移除旧配音轨再追加(幂等;BGM 镜像轨由 node_12 负责)
    tracks = en_draft.setdefault("tracks", [])
    tracks[:] = [
        t for t in tracks
        if not (t.get("type") == "audio" and t.get("name") == EN_VOICEOVER_TRACK_NAME)
    ]
    tracks.append(
        {
            "type": "audio",
            "id": _EN_DUB_TRACK_ID,
            "name": EN_VOICEOVER_TRACK_NAME,
            "segments": [
                {
                    "id": _EN_DUB_SEGMENT_ID,
                    "material_id": _EN_DUB_MATERIAL_ID,
                    "name": EN_VOICEOVER_TRACK_NAME,
                    "target_timerange": {"start": 0, "duration": duration_us},
                }
            ],
        }
    )

    try:
        write_draft(en_file, en_draft)
    except (OSError, ValueError) as exc:
        notes.append(f"[node_17] 英文配音写入草稿失败: {exc}")
        return notes

    notes.append(_EN_VOICEOVER_WRITTEN_TAG)
    return notes


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

    # 阶段 2(2026-11-09 效果缺失修复):时长以 WAV 头为准(与 P3 中文适配器
    # 同口径)—— 计时权威是落盘文件本身;服务端返回值仅作解析失败时兜底。
    from storyline_capabilities.tts_runner import read_wav_duration_ms

    duration_ms = int(result.data.get("duration_ms") or 0)
    if audio_path:
        header_ms = read_wav_duration_ms(str(audio_path))
        if header_ms > 0:
            duration_ms = header_ms

    # P3b:把配音写进英文分支草稿(降级不阻塞,说明分流 status_log/error_log)
    status_log = ["node_17_inject_english_tts_done"]
    error_log: list[str] = []
    for note in _write_en_voiceover_track(
        state, str(audio_path or ""), duration_ms
    ):
        if note == _EN_VOICEOVER_WRITTEN_TAG:
            status_log.append(note)
        else:
            error_log.append(note)

    return {
        "en_audio_path": audio_path,
        "en_dub_audio_path": audio_path,
        "tts_run_id": uuid.uuid4().hex[:12],
        "tts_issue": None,
        "status_log": status_log,
        **({"error_log": error_log} if error_log else {}),
    }
