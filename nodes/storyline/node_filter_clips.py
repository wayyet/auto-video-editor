"""Phase 4 B 类本地化节点:storyline_filter_clips(plan_v4 §5 阶段 2)。"""
from __future__ import annotations

import json
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.filter_clips import filter_clips
from nodes.storyline._common import append_status_tag, _resolve_outputs_root


def storyline_filter_clips_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    understanding_path = state.get("storyline_understanding_artifact")
    if not understanding_path:
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                "[storyline:filter_clips] no understanding_artifact",
            ],
            "status_log": append_status_tag(state, "storyline_filter_clips_failed"),
        }

    try:
        understanding = json.loads(Path(str(understanding_path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:filter_clips] CONTRACT_INVALID: {e!r}",
            ],
            "status_log": append_status_tag(state, "storyline_filter_clips_failed"),
        }

    try:
        result = filter_clips(understanding_artifact=understanding)
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:filter_clips] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(state, "storyline_filter_clips_failed"),
        }

    out_path = out_dir / "filtered_clips.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return {
        "storyline_filtered_clips": str(out_path),
        "status_log": append_status_tag(state, "storyline_filter_clips_done"),
    }
