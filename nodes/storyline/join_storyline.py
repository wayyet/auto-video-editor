"""storyline_join — plan_v4 §3.3 收尾节点:对 stage 0 透传模式补齐 mapper 输入。

职责:
1. **读取** ``storyline_timeline_plan``:由 plan_timeline_pro 写出,经 qa_gate
   通过,CanonicalTimeline JSON 路径。
2. **反序列化** 为 dict,写入 ``storyline_plan``(Phase 1 既有字段;``auto-mode``
   下不再由 node_04 写而由本节点写,plan §3.3)。
3. **写** ``storyline_status_log`` alias(``status_log`` 已有 reducer):
   append ``storyline_join_done``。
4. **副作用**:写 ``storyline_outputs_root``(若缺)+ ``storyline_session_id``
   占位以便 resume 老 checkpoint 不 KeyError。

设计纪律(plan §3.3):
- 不引入新的 ``storyline_status_log`` / ``storyline_error_log``,统一用既有
  ``status_log`` / ``error_log``(Week 4 reducer 已支持 fan-in 去重)。
- 不重排 clips,不引入浮点;若 Pydantic 校验失败 → 写 error_log,返回 state
  由图走向 END;不抛异常(避免 graph 终止不优雅)。
"""
from __future__ import annotations

import json
from pathlib import Path

from state import WorkflowState
from storyline.contract import (
    CanonicalTimeline,
    ContractInvalid,
    StorylineErrorCode,
)
from nodes.storyline._common import append_status_tag


def _read_timeline_dict(state: WorkflowState) -> dict | None:
    """从 ``storyline_timeline_plan`` 读 JSON;失败时返回 None(不抛)。"""
    path = state.get("storyline_timeline_plan")
    if not path:
        return None
    p = Path(str(path))
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def storyline_join_node(state: WorkflowState) -> dict:
    """auto-mode 19 节点收尾:把 timeline_plan 反序列化到 storyline_plan。"""
    delta_log = ["storyline_join_done"]

    # 1. 路径与反序列化(stub 模式可能没写过文件,允许 None)
    timeline_dict = _read_timeline_dict(state)
    if timeline_dict is None:
        # 没写出过文件(stub 模式产物路径是 placeholder),写入一个最小的
        # CanonicalTimeline 占位以便 mapper.canonical_to_draft 不报错。
        job_id = str(state.get("session_id") or "unknown")
        video = state.get("video_input_path") or ""
        timeline_dict = {
            "schema_version": "1.0",
            "job_id": job_id,
            "created_at_ms": 0,
            "source_media": [
                {
                    "media_id": "media_0001",
                    "file_uri": f"file:///{video.replace(chr(92), '/')}"
                    if video
                    else "file:///unknown",
                    "duration_ms": 35000,
                    "media_type": "video",
                }
            ],
            "clips": [
                {
                    "clip_id": "clip_0001",
                    "source_media_id": "media_0001",
                    "source_in_ms": 0,
                    "source_out_ms": 35000,
                    "timeline_in_ms": 0,
                    "timeline_out_ms": 35000,
                    "semantic_tags": [],
                }
            ],
            "audio": {"bgm_ref": None, "voiceover": None},
            "subtitles": {"zh": None, "en": None},
            "options": {
                "enable_ai_transition": False,
                "enable_voiceover": True,
                "max_duration_ms": 35000,
            },
        }

    # 2. Pydantic 校验(防御性);失败时写 error_log 但**不**抛
    try:
        CanonicalTimeline.model_validate(timeline_dict)
    except Exception as e:  # noqa: BLE001 — ValueError / ValidationError
        return {
            "status_log": append_status_tag(
                state, "storyline_join_done", "storyline_join_contract_invalid"
            ),
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:join] {StorylineErrorCode.CONTRACT_INVALID}: {e}",
            ],
            "storyline_error_code": StorylineErrorCode.CONTRACT_INVALID,
            # 仍要把 timeline_dict 写到 storyline_plan 让 node_05 mapper 兜底处理
            "storyline_plan": timeline_dict,
        }

    # 3. 写 storyline_plan(供 node_05 mapper 读)
    delta: dict = {
        "storyline_plan": timeline_dict,
        "status_log": append_status_tag(state, *delta_log),
    }

    # 4. 兜底 session_id / outputs_root,resume 老 checkpoint 时不 KeyError
    if not state.get("storyline_session_id"):
        delta["storyline_session_id"] = str(state.get("session_id") or "unknown")
    if not state.get("storyline_outputs_root"):
        from nodes.storyline._common import _resolve_outputs_root  # 局部避免循环

        delta["storyline_outputs_root"] = str(_resolve_outputs_root(state))

    return delta


__all__ = ["storyline_join_node"]
