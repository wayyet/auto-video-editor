"""Phase 4/5 C 类旁支节点:storyline_plan_timeline_ai_transition(plan_v4 §5 阶段 5 / ADR-005)。

仅 ``STORYLINE_ENABLE_AI_TRANSITION=1`` 才走真实路径(plan §5 阶段 5)。本节
面默认不实现,flag 关闭时 noop;flag 开启时复用 ``plan_timeline_pro`` 主算法
+ 把 AI 转场 ``duration_ms`` 加进 timeline(扩展字段),不改变 CanonicalTimeline
contract 主结构。

输入(flag 开启时):
- ``storyline_groups_artifact``
- ``storyline_voiceover_artifact``
- ``storyline_bgm_selection``
- ``storyline_transition_plan``
- ``storyline_text_style_plan``
- ``storyline_ai_transition_artifact``
- ``storyline_targets``

输出(flag 开启时):
- ``storyline_timeline_plan`` (覆盖普通版的 timeline_plan 路径)
- ``status_log`` append ``storyline_plan_timeline_ai_transition_done``
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.ai_transition_client import (
    is_ai_transition_enabled,
)
from storyline_capabilities.plan_timeline_pro import plan_timeline_pro
from nodes.storyline._common import append_status_tag, _resolve_outputs_root

logger = logging.getLogger(__name__)


def _read_json_artifact(
    state: WorkflowState,
    key: str,
) -> dict | None:
    p = state.get(key)
    if not p:
        return None
    try:
        return json.loads(Path(str(p)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_groups(state: WorkflowState) -> list[dict]:
    doc = _read_json_artifact(state, "storyline_groups_artifact")
    if isinstance(doc, dict):
        return list(doc.get("groups") or [])
    return []


def _read_media(state: WorkflowState) -> list[dict]:
    doc = _read_json_artifact(state, "storyline_media_artifact")
    if isinstance(doc, dict):
        return list(doc.get("media") or [])
    return []


def _attach_ai_transition_meta(
    timeline: dict,
    ai_transition_doc: dict,
) -> dict:
    """把 ``storyline_ai_transition_artifact`` 的 items 写到 timeline 的
    ``ai_transitions`` 字段(扩展字段,mapper 不读,联调可查)。
    """
    items = list(ai_transition_doc.get("items") or []) if isinstance(ai_transition_doc, dict) else []
    if not items:
        return timeline
    timeline["ai_transitions"] = items
    return timeline


def storyline_plan_timeline_ai_transition_node(state: WorkflowState) -> dict:
    if not is_ai_transition_enabled():
        # 默认 noop — flag 关就完全跳过,不动 storyline_timeline_plan 字段
        return {
            "status_log": append_status_tag(
                state, "storyline_plan_timeline_ai_transition_skipped"
            ),
        }

    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    groups = _read_groups(state)
    source_media = _read_media(state)
    voiceover_doc = _read_json_artifact(state, "storyline_voiceover_artifact")
    bgm_selection = state.get("storyline_bgm_selection") or {}
    transition_plan = state.get("storyline_transition_plan") or {}
    text_style_plan = state.get("storyline_text_style_plan") or {}
    ai_transition_doc = _read_json_artifact(state, "storyline_ai_transition_artifact")
    targets = state.get("storyline_targets") or {}
    job_id = str(state.get("session_id") or "unknown")

    if not groups:
        # 与 plan_timeline_pro 一致:无 group → 写最小空 timeline
        out_path = out_dir / "timeline_plan_ai.json"
        out_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "job_id": job_id,
                    "clips": [],
                    "audio": {"bgm_ref": None, "voiceover": {}},
                    "subtitles": {"zh": None, "en": None},
                    "options": {
                        "enable_ai_transition": True,
                        "enable_voiceover": False,
                        "max_duration_ms": int(targets.get("target_duration_ms") or 35000),
                    },
                    "method": "empty_ai",
                    "warnings": [],
                },
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
        return {
            "storyline_timeline_plan": str(out_path),
            "status_log": append_status_tag(
                state, "storyline_plan_timeline_ai_transition_done_empty"
            ),
        }

    try:
        timeline = plan_timeline_pro(
            groups=groups,
            voiceover=voiceover_doc if isinstance(voiceover_doc, dict) else None,
            bgm_selection=bgm_selection if isinstance(bgm_selection, dict) else None,
            transition_plan=transition_plan if isinstance(transition_plan, dict) else None,
            text_style_plan=text_style_plan if isinstance(text_style_plan, dict) else None,
            targets=targets if isinstance(targets, dict) else {},
            source_media=source_media,
            job_id=job_id,
        )
        timeline = _attach_ai_transition_meta(timeline, ai_transition_doc or {})
        timeline["options"]["enable_ai_transition"] = True
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:plan_timeline_ai_transition] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_plan_timeline_ai_transition_failed"
            ),
        }

    out_path = out_dir / "timeline_plan_ai.json"
    out_path.write_text(
        json.dumps(timeline, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return {
        "storyline_timeline_plan": str(out_path),
        "status_log": append_status_tag(
            state, "storyline_plan_timeline_ai_transition_done"
        ),
    }