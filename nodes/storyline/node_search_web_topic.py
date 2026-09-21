"""Phase 4 A 类本地化节点:storyline_search_web_topic(plan_v4 §5 阶段 1)。

DuckDuckGo html 端点(无 key),结果写到 storyline_web_topic_artifact。
"""
from __future__ import annotations

import json
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.search_web_topic import search_web_topic as cap_topic
from nodes.storyline._common import append_status_tag, _resolve_outputs_root


def storyline_search_web_topic_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = state.get("storyline_targets") or {}
    query = (
        (targets.get("query") if isinstance(targets, dict) else None) or ""
    ).strip()

    try:
        result = cap_topic(
            query=query,
            max_results=10,
            provider="duckduckgo_html",
        )
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:search_web_topic] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_search_web_topic_failed"
            ),
        }

    out_path = out_dir / "search_web_topic.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )

    return {
        "storyline_web_topic_artifact": str(out_path),
        "status_log": append_status_tag(state, "storyline_search_web_topic_done"),
    }
