"""Phase 4 B 类本地化节点:storyline_group_clips(plan_v4 §5 阶段 2)。"""
from __future__ import annotations

import json
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.group_clips import group_clips
from nodes.storyline._common import append_status_tag, _resolve_outputs_root


def storyline_group_clips_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    filtered_path = state.get("storyline_filtered_clips")
    if not filtered_path:
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                "[storyline:group_clips] no filtered_clips",
            ],
            "status_log": append_status_tag(state, "storyline_group_clips_failed"),
        }

    try:
        filtered_doc = json.loads(Path(str(filtered_path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:group_clips] CONTRACT_INVALID: {e!r}",
            ],
            "status_log": append_status_tag(state, "storyline_group_clips_failed"),
        }

    try:
        result = group_clips(
            filtered_clips=filtered_doc.get("filtered_clips") or []
        )
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:group_clips] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(state, "storyline_group_clips_failed"),
        }

    out_path = out_dir / "groups.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return {
        "storyline_groups_artifact": str(out_path),
        "status_log": append_status_tag(state, "storyline_group_clips_done"),
    }
