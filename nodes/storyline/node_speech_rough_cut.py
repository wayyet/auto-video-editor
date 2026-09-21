"""Phase 4/5 C 类本地化节点:storyline_speech_rough_cut(plan_v4 §5 阶段 5 条件旁支)。

``local_asr → speech_rough_cut``:基于 ASR 结果做"按语音切段的粗剪"。

阶段 0 时是 ``_mcp_passthrough`` 壳子;阶段 5 起改为调本地化的
``storyline_capabilities.speech_rough_cut.speech_rough_cut`` — 纯 LLM 调用,
无 vendored 依赖。

输入:
- ``storyline_asr_artifact`` (JSON 路径,含 ``segments`` 数组)
- ``storyline_shots_artifact`` (JSON 路径,回切镜头参考)

输出:
- ``storyline_rough_cut_artifact`` (粗剪分段 JSON 路径)
- ``status_log`` append ``storyline_speech_rough_cut_done``
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.speech_rough_cut import speech_rough_cut
from nodes.storyline._common import append_status_tag, _resolve_outputs_root

logger = logging.getLogger(__name__)


def _read_asr_segments(state: WorkflowState) -> list[dict]:
    p = state.get("storyline_asr_artifact")
    if not p:
        return []
    try:
        doc = json.loads(Path(str(p)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(doc, dict):
        return list(doc.get("segments") or [])
    return []


def storyline_speech_rough_cut_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    segments = _read_asr_segments(state)

    if not segments:
        # ASR 空 → 没有可粗剪的句;写空 artifact,跳过(plan §4.4)
        out_path = out_dir / "rough_cut.json"
        out_path.write_text(
            json.dumps(
                {"segments": [], "method": "no_input"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {
            "storyline_rough_cut_artifact": str(out_path),
            "status_log": append_status_tag(
                state, "storyline_speech_rough_cut_skipped"
            ),
        }

    ctx_text = " ".join(str(s.get("text") or "") for s in segments)
    rough_out: list[dict] = []
    history: list[dict] = []
    preceding = ""
    for i, sent in enumerate(segments):
        following = (
            str(segments[i + 1].get("text") or "")
            if i + 1 < len(segments)
            else ""
        )
        try:
            res = speech_rough_cut(
                asr_sentence=sent,
                ctx_text=ctx_text,
                history=history,
                preceding=preceding,
                following=following,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"speech_rough_cut segment {i} exception: {e!r}")
            continue
        for seg in res.get("segments") or []:
            seg_out = dict(seg)
            seg_out["asr_sentence_index"] = i
            rough_out.append(seg_out)
        # history 累积(plan §5 阶段 5)
        history.append({"reason": res.get("reason"), "res": res.get("segments")})
        preceding = (sent.get("text") or "").strip()

    out_path = out_dir / "rough_cut.json"
    out_path.write_text(
        json.dumps(
            {"segments": rough_out, "method": "sequential"},
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    return {
        "storyline_rough_cut_artifact": str(out_path),
        "status_log": append_status_tag(
            state,
            f"storyline_speech_rough_cut_done:segments={len(rough_out)}",
        ),
    }