"""Phase 4 B 类本地化节点:storyline_understand_clips(plan_v4 §5 阶段 2)。"""
from __future__ import annotations

import json
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.understand_clips import understand_clips
from nodes.storyline._common import append_status_tag, _resolve_outputs_root


def storyline_understand_clips_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    media_path = state.get("storyline_media_artifact")
    shots_path = state.get("storyline_shots_artifact")
    if not media_path or not shots_path:
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                "[storyline:understand_clips] missing media_artifact or shots_artifact",
            ],
            "status_log": append_status_tag(
                state, "storyline_understand_clips_failed"
            ),
        }

    try:
        media_artifact = json.loads(Path(str(media_path)).read_text(encoding="utf-8"))
        shots_artifact = json.loads(Path(str(shots_path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:understand_clips] CONTRACT_INVALID: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_understand_clips_failed"
            ),
        }

    try:
        # lang 解析(顺序:state.storyline_targets.lang → "zh" 默认)
        targets = state.get("storyline_targets")
        lang = "zh"
        if isinstance(targets, dict) and isinstance(targets.get("lang"), str):
            lang = targets["lang"]

        result = understand_clips(
            split_shots_artifact=shots_artifact,
            media_artifact=media_artifact,
            lang=lang,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:understand_clips] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_understand_clips_failed"
            ),
        }

    out_path = out_dir / "understanding.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return {
        "storyline_understanding_artifact": str(out_path),
        "status_log": append_status_tag(state, "storyline_understand_clips_done"),
    }
