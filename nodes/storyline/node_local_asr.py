"""Phase 4/5 C 类本地化节点:storyline_local_asr(plan_v4 §5 阶段 5 条件旁支)。

``LM → local_asr`` 边的终点(条件:``有语音``)。

阶段 0 时是 ``_mcp_passthrough`` 壳子;阶段 5 起改为调本地化的
``storyline_capabilities.asr_runner.transcribe_media`` — 默认 stub(返回空
segments),``STORYLINE_ASR_MODE=vendored`` 时走 vendored venv subprocess 调
funasr/torchaudio。

输入:
- ``storyline_media_artifact`` (JSON 路径,含 ``media`` 数组)

输出:
- ``storyline_asr_artifact`` (ASR JSON 路径)
- ``status_log`` append ``storyline_local_asr_done``
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.asr_runner import (
    ASR_MODE,
    transcribe_media,
)
from nodes.storyline._common import append_status_tag, _resolve_outputs_root

logger = logging.getLogger(__name__)


def _read_media(state: WorkflowState) -> list[dict]:
    p = state.get("storyline_media_artifact")
    if not p:
        return []
    try:
        doc = json.loads(Path(str(p)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(doc, dict):
        return list(doc.get("media") or [])
    return []


def _pick_audio_media(media: list[dict]) -> dict | None:
    """挑第一个 ``has_audio=True`` 的视频,否则第一个视频;无则 None。"""
    for m in media:
        if (m.get("media_type") or "").lower() == "video":
            meta = m.get("metadata") or {}
            if isinstance(meta, dict) and meta.get("has_audio"):
                return m
    for m in media:
        if (m.get("media_type") or "").lower() == "video":
            return m
    return None


def storyline_local_asr_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    media = _read_media(state)
    audio = _pick_audio_media(media)

    # 1. 无视频 → 写空 ASR(stub),不写 error_log(因为可能根本没语音)
    if not audio:
        empty_path = out_dir / "asr.json"
        empty_path.write_text(
            json.dumps(
                {"segments": [], "text": "", "lang": "zh", "provider": "stub"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {
            "storyline_asr_artifact": str(empty_path),
            "status_log": append_status_tag(state, "storyline_local_asr_skipped"),
        }

    # 2. 调本地 asr_runner(默认 stub;ASR_MODE=vendored 时走 vendored 子进程)
    try:
        result = transcribe_media(
            media_path=Path(str(audio.get("path") or "")),
            output_dir=out_dir,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:local_asr] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_local_asr_failed"
            ),
        }

    # 3. 写产物 + delta
    artifact_path = Path(result.get("artifact") or (out_dir / "asr.json"))
    if not artifact_path.exists():
        # 防御:transcribe 没写出文件 → 补一份 stub,标记 failed
        artifact_path.write_text(
            json.dumps(
                {"segments": [], "text": "", "lang": "zh", "provider": result.get("provider", "stub")},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    delta_log = [
        "storyline_local_asr_done",
        f"storyline_local_asr_mode={ASR_MODE}",
        f"storyline_local_asr_segments={len(result.get('segments') or [])}",
    ]
    delta: dict = {
        "storyline_asr_artifact": str(artifact_path),
        "status_log": append_status_tag(state, *delta_log),
    }

    if result.get("error"):
        delta["error_log"] = [
            *list(state.get("error_log", []) or []),
            f"[storyline:local_asr] ASR_PARTIAL: {result['error']}",
        ]
    return delta