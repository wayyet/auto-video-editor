"""Phase 4 C 类本地化节点:storyline_select_bgm(plan_v4 §5 阶段 4)。

阶段 0 时是 ``_mcp_passthrough`` 壳子;阶段 4 起改为调本地化的
``storyline_capabilities.select_bgm.select_bgm`` — 无 MCP、无 vendored venv。

输入:
- ``storyline_groups_artifact`` (JSON 路径,含 ``groups`` 数组)
- ``storyline_script_artifact`` (JSON 路径,含 ``group_scripts`` 数组)
- ``storyline_targets.bgm_request`` (用户对 BGM 的要求,可空)
- ``storyline_voiceover_artifact`` (供 generate_voiceover 时长对齐,可选)

输出:
- ``storyline_bgm_selection`` (内联 dict,plan §3.2 字段约定)
- ``status_log`` append ``storyline_select_bgm_done``
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.select_bgm import select_bgm
from nodes.storyline._common import append_status_tag, _resolve_outputs_root

logger = logging.getLogger(__name__)


def _read_groups(state: WorkflowState) -> list[dict]:
    p = state.get("storyline_groups_artifact")
    if not p:
        return []
    try:
        doc = json.loads(Path(str(p)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(doc, dict):
        return list(doc.get("groups") or [])
    return []


def _read_script(state: WorkflowState) -> list[dict]:
    p = state.get("storyline_script_artifact")
    if not p:
        return []
    try:
        doc = json.loads(Path(str(p)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(doc, dict):
        return list(doc.get("group_scripts") or [])
    return []


def _extract_user_request(state: WorkflowState) -> str:
    targets = state.get("storyline_targets") or {}
    if isinstance(targets, dict):
        for k in ("bgm_request", "music_request", "user_request"):
            v = targets.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def storyline_select_bgm_node(state: WorkflowState) -> dict:
    groups = _read_groups(state)
    scripts = _read_script(state)
    user_request = _extract_user_request(state)

    narration_text = "\n".join(
        (g.get("narration") or "").strip() for g in scripts if g.get("narration")
    )

    try:
        result = select_bgm(
            user_request=user_request,
            groups=groups,
            narration_text=narration_text,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"select_bgm capability exception: {e!r}")
        result = {
            "bgm_ref": "bgm_fallback",
            "bgm_path": "",
            "beats": [],
            "duration_ms": 35000,
            "tempo": "medium",
            "default_volume": 0.0,
            "reason": f"select_bgm exception: {e!r}",
            "method": "fallback",
            "stub": True,
        }

    delta: dict = {
        "storyline_bgm_selection": result,
        "status_log": append_status_tag(
            state,
            f"storyline_select_bgm_done:bgm_ref={result.get('bgm_ref', '?')}",
        ),
    }

    # stub 兜底时告知调试;不阻断下游
    if result.get("stub"):
        delta["error_log"] = [
            *list(state.get("error_log", []) or []),
            f"[storyline:select_bgm] BGM_FALLBACK: bgm_ref={result.get('bgm_ref', '?')} reason={result.get('reason', '')}",
        ]
    return delta