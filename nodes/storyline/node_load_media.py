"""Phase 4 A 类本地化节点:storyline_load_media(plan_v4 §5 阶段 1)。

阶段 0 时是 _mcp_passthrough 壳子;**阶段 1 起**改为调本地化的
``storyline_capabilities.load_media.load_media``(无 torch)。
"""
from __future__ import annotations

import json
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.load_media import load_media as cap_load_media
from nodes.storyline._common import append_status_tag


def _build_inputs(state: WorkflowState) -> list[dict]:
    """组装 inputs 列表 — 阶段 1 仅 video_input_path 一个;阶段 1+ 加 targets。

    与 vendored OpenStoryline copy ``LoadMediaInput`` 兼容。
    """
    return [{"path": state.get("video_input_path") or ""}]


def _outputs_root(state: WorkflowState) -> Path:
    from nodes.storyline._common import _resolve_outputs_root

    return _resolve_outputs_root(state)


def storyline_load_media_node(state: WorkflowState) -> dict:
    inputs = _build_inputs(state)
    outputs_root = _outputs_root(state)

    try:
        result = cap_load_media(inputs, include_hash=False)
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:load_media] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(state, "storyline_load_media_failed"),
        }

    # 写产物到 outputs/storyline/load_media.json
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / "load_media.json"
    artifact_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )

    return {
        "storyline_media_artifact": str(artifact_path),
        "status_log": append_status_tag(state, "storyline_load_media_done"),
    }
