"""Phase 4 B 类本地化节点:storyline_recommend_transition(plan_v4 §5 阶段 3)。"""
from __future__ import annotations

import json
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.recommend_transition import recommend_transition
from nodes.storyline._common import append_status_tag, _resolve_outputs_root


def storyline_recommend_transition_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 读 groups
    groups: list[dict] = []
    g_path = state.get("storyline_groups_artifact")
    if g_path:
        try:
            doc = json.loads(Path(str(g_path)).read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                groups = list(doc.get("groups") or [])
        except (OSError, json.JSONDecodeError):
            groups = []

    # 读 bgm
    bgm_selection: dict | None = None
    bgm_field = state.get("storyline_bgm_selection")
    if bgm_field and isinstance(bgm_field, dict):
        bgm_selection = bgm_field

    try:
        result = recommend_transition(groups=groups, bgm_selection=bgm_selection)
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:recommend_transition] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_recommend_transition_failed"
            ),
        }

    return {
        "storyline_transition_plan": result,
        "status_log": append_status_tag(
            state, "storyline_recommend_transition_done"
        ),
    }
