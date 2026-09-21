"""Phase 4/5 C 类本地化节点:storyline_plan_timeline_pro(plan_v4 §5 阶段 5)。

阶段 0 时是 ``_mcp_passthrough`` 壳子;阶段 5 起改为调本地化的
``storyline_capabilities.plan_timeline_pro.plan_timeline_pro`` — 无 vendored
依赖,纯 Python 简化版 TimeLine。

输入:
- ``storyline_groups_artifact`` (JSON 路径,含 ``groups`` 数组)
- ``storyline_script_artifact`` (JSON 路径,含 ``group_scripts`` 数组)
- ``storyline_voiceover_artifact`` (JSON 路径,含 ``voiceover`` 数组)
- ``storyline_bgm_selection`` (内联 dict)
- ``storyline_transition_plan`` (内联 dict)
- ``storyline_text_style_plan`` (内联 dict)
- ``storyline_targets`` (目标时长 / 比例)
- ``storyline_media_artifact`` (JSON 路径,含 ``media`` 数组;用于填
  ``source_media`` 字段)

输出:
- ``storyline_timeline_plan`` (CanonicalTimeline JSON 路径)
- ``status_log`` append ``storyline_plan_timeline_pro_done``
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from state import WorkflowState
from storyline.contract import StorylineErrorCode
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


def storyline_plan_timeline_pro_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    groups = _read_groups(state)
    source_media = _read_media(state)

    voiceover_doc = _read_json_artifact(state, "storyline_voiceover_artifact")
    bgm_selection = state.get("storyline_bgm_selection") or {}
    transition_plan = state.get("storyline_transition_plan") or {}
    text_style_plan = state.get("storyline_text_style_plan") or {}
    targets = state.get("storyline_targets") or {}

    job_id = str(state.get("session_id") or "unknown")

    if not groups:
        # 没有 group → 写最小空 timeline 让 join_storyline 兜底;不阻断
        out_path = out_dir / "timeline_plan.json"
        out_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "job_id": job_id,
                    "created_at_ms": 0,
                    "source_media": source_media or [
                        {
                            "media_id": "media_0001",
                            "file_uri": "file:///unknown",
                            "duration_ms": 35000,
                            "media_type": "video",
                        }
                    ],
                    "clips": [],
                    "audio": {"bgm_ref": None, "voiceover": {}},
                    "subtitles": {"zh": None, "en": None},
                    "options": {
                        "enable_ai_transition": False,
                        "enable_voiceover": False,
                        "max_duration_ms": int(targets.get("target_duration_ms") or 35000),
                    },
                    "method": "empty",
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
                state, "storyline_plan_timeline_pro_done_empty"
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
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:plan_timeline_pro] {StorylineErrorCode.TOOL_EXECUTION_FAILED}: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_plan_timeline_pro_failed"
            ),
        }

    out_path = out_dir / "timeline_plan.json"
    out_path.write_text(
        json.dumps(timeline, ensure_ascii=False, default=str), encoding="utf-8"
    )

    delta_log: list[str] = [
        "storyline_plan_timeline_pro_done",
        f"storyline_timeline_method={timeline.get('method', 'unknown')}",
    ]
    delta: dict = {
        "storyline_timeline_plan": str(out_path),
        "status_log": append_status_tag(state, *delta_log),
    }

    # warnings 写 status_log 便于联调识别;不阻断
    for w in timeline.get("warnings") or []:
        delta["status_log"] = append_status_tag(
            state, f"storyline_timeline_warning: {w}"
        )
    return delta