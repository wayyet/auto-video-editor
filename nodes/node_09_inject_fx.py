"""节点 9:inject_fx — 转场 + 视频特效注入(对应原文档 4.4 节)。

VIP 资源复用机制:
- 从 ``templates/fx_template.json`` 加载占位资源(Week 3) / 真实 resource_id(Week 4)
- 在每个分镜边界注入转场对象到 ``materials.transitions``
- 视频特效追加到 ``materials.video_effects``
- 模板库缺失时降级为空注入(Week 3 可接受)

分镜边界:取 ``shot_plan.shots`` 的数量,按视频轨 segments 顺序插入 N-1 个转场。
"""

from __future__ import annotations

import json
from pathlib import Path

from draft_ops.atomic_writer import atomic_write_draft
from jy_common.template_library import load_template_library
from state import WorkflowState


def _load_draft(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_video_track(draft: dict) -> dict:
    for track in draft.get("tracks", []):
        if track.get("type") == "video":
            return track
    tracks = draft.get("tracks", [])
    return tracks[0] if tracks else {"type": "video", "segments": []}


def _boundary_count(video_track: dict, shot_plan: dict | None) -> int:
    """分镜边界数 = max(segments 数量 - 1, shots 数量 - 1, 0)。"""
    seg_count = len(video_track.get("segments", []))
    shot_list = (shot_plan or {}).get("shots", []) if isinstance(shot_plan, dict) else []
    if seg_count > 1 and len(shot_list) > 1:
        return min(seg_count, len(shot_list)) - 1
    if seg_count > 1:
        return seg_count - 1
    if len(shot_list) > 1:
        return len(shot_list) - 1
    return 0


def inject_fx(state: WorkflowState) -> dict:
    """读取草稿 → 加载模板 → 在分镜边界注入转场 → 原子写回。"""
    draft_path = Path(state["draft_path"])
    draft = _load_draft(draft_path)
    template_lib = load_template_library("templates/fx_template.json")

    if template_lib.is_empty:
        log = list(state.get("status_log", []) or []) + ["node_09_inject_fx_no_template"]
        errors = list(state.get("error_log", []) or [])
        errors.append("[node_09] 模板库缺失,降级通过(Week 3 可接受)")
        return {**state, "status_log": log, "error_log": errors}

    materials = draft.setdefault("materials", {})
    transitions = materials.setdefault("transitions", [])
    video_effects = materials.setdefault("video_effects", [])

    video_track = _find_video_track(draft)
    shot_plan = state.get("shot_plan")
    n_boundaries = _boundary_count(video_track, shot_plan)

    for i in range(n_boundaries):
        style_tag = (shot_plan or {}).get("shots", [{}])[i].get("style_tag", "default") if isinstance(shot_plan, dict) else "default"
        trans_obj = template_lib.pick_transition(style_tag)
        if not trans_obj:
            continue
        trans_obj = dict(trans_obj)
        trans_obj["id"] = f"transition-{i + 1}"
        transitions.append(trans_obj)

    # Week 3:每个视频特效追加一次(全局,不重复)
    effect_obj = template_lib.pick_video_effect("default")
    if effect_obj:
        effect_obj = dict(effect_obj)
        effect_obj["id"] = f"vfx-{len(video_effects) + 1}"
        video_effects.append(effect_obj)

    atomic_write_draft(draft_path, draft)

    log = list(state.get("status_log", []) or []) + ["node_09_inject_fx_done"]
    return {**state, "status_log": log}