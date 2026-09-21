"""Phase 4 A 类本地化节点:storyline_split_shots(plan_v4 §5 阶段 1)。

简化版(帧差)切分。TransNetV2 阶段 5 接入(plan_v4 §5 阶段 5 决策),本节点
阶段 1/2/3 都用本地 frame_difference_split_shots 走主 venv(无需 torch)。
"""
from __future__ import annotations

import json
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.split_shots import split_shots as cap_split_shots
from nodes.storyline._common import append_status_tag, _resolve_outputs_root


def storyline_split_shots_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    media_artifact = state.get("storyline_media_artifact")
    if not media_artifact:
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                "[storyline:split_shots] CONTRACT_INVALID: no storyline_media_artifact",
            ],
            "status_log": append_status_tag(
                state, "storyline_split_shots_failed_no_artifact"
            ),
        }

    # 读 manifest(可能是 load_media 或 search_media 输出)
    media_artifact_path = Path(str(media_artifact))
    if not media_artifact_path.exists():
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:split_shots] TOOL_NOT_FOUND: {media_artifact_path}",
            ],
            "status_log": append_status_tag(
                state, "storyline_split_shots_failed_no_file"
            ),
        }

    try:
        manifest = json.loads(media_artifact_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:split_shots] CONTRACT_INVALID: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_split_shots_failed_bad_artifact"
            ),
        }

    media_list = manifest.get("media", []) if isinstance(manifest, dict) else []
    try:
        result = cap_split_shots(media_list)  # list 输入
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:split_shots] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(state, "storyline_split_shots_failed"),
        }

    out_path = out_dir / "split_shots.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )

    return {
        "storyline_shots_artifact": str(out_path),
        "status_log": append_status_tag(state, "storyline_split_shots_done"),
    }
