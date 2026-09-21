"""Phase 4 A 类本地化节点:storyline_search_media(plan_v4 §5 阶段 1)。

Pexels 搜索:从 ``os.environ.get("PEXELS_API_KEY")`` 读 key,空时返回 ``error_code="MISSING_KEY"``。
把搜索结果写本地 manifest;merge 到 ``storyline_media_artifact``。
"""
from __future__ import annotations

import json
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.search_media import search_media as cap_search_media
from nodes.storyline._common import append_status_tag, _resolve_outputs_root


def storyline_search_media_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    media_dir = outputs_root / "search_media"
    media_dir.mkdir(parents=True, exist_ok=True)

    targets = state.get("storyline_targets") or {}
    query = (
        (targets.get("query") if isinstance(targets, dict) else None) or ""
    ).strip()
    photo_number = (
        int(targets.get("photo_number", 5)) if isinstance(targets, dict) else 5
    )
    video_number = (
        int(targets.get("video_number", 5)) if isinstance(targets, dict) else 5
    )

    try:
        result = cap_search_media(
            pexels_api_key=None,  # 内部走 PEXELS_API_KEY env
            query=query,
            media_dir=media_dir,
            photo_number=photo_number,
            video_number=video_number,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:search_media] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(state, "storyline_search_media_failed"),
        }

    manifest_path = media_dir / "search_media.json"
    manifest_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )

    return {
        "storyline_media_artifact": str(manifest_path),
        "status_log": append_status_tag(state, "storyline_search_media_done"),
    }
