"""节点 9:inject_fx — 转场 + 视频特效注入(对应原文档 4.4 节)。

VIP 资源复用机制(Week 4 升级):
- 同时加载 ``templates/fx_template.json``(风格索引)+ ``templates/fx_resource_library.json``
  (真实 VIP 资源库,由 ``scripts/build_resource_library.py`` 从 pyJianYingDraft metadata
  一次性 dump 出来,见对照报告 §4.2 / §5)。
- 按 style_tag 选转场占位后,再用 ``pick_transition_by_name`` 把 name 解析成真实 19 位
  数字 resource_id。
- 模板库缺失时降级为空注入;fx_resource_library.json 缺失或 name 不命中时降级到占位
  resource_id(走 warning + error_log,但不抛异常)。

分镜边界:取 ``shot_plan.shots`` 的数量,按视频轨 segments 顺序插入 N-1 个转场。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from draft_ops.atomic_writer import atomic_write_draft
from jy_common.template_library import TemplateLibrary, load_resource_libraries
from state import WorkflowState


_VIP_ID_PATTERN = re.compile(r"^\d{19}$")


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


def _resolve_vip_transition(
    template_lib: TemplateLibrary, style_tag: str, fallback_name: str | None = None
) -> dict | None:
    """从模板里按 style_tag 挑转场占位 → 用 by_name 查 VIP 真 ID。

    命中 by_name:返回含真实 resource_id/effect_id/md5/default_duration_s/is_overlap 的 dict。
    未命中:返回占位 dict(resource_id 仍是 PLACEHOLDER_*)。
    fallback_name 仍查不到:返回 None。
    """
    placeholder = template_lib.pick_transition(style_tag)
    if not placeholder:
        # style_tag 选不到 → 直接用 fallback_name 查 by_name
        if fallback_name:
            return template_lib.pick_transition_by_name(fallback_name)
        return None

    name = placeholder.get("name") or fallback_name
    if not name:
        return placeholder

    vip = template_lib.pick_transition_by_name(name)
    if vip is None:
        return placeholder  # name 不在 VIP 库,降级到占位
    # 以 VIP 字段为准,保留模板里的额外字段(若有)
    out = dict(vip)
    out.setdefault("name", name)
    return out


def _resolve_vip_video_effect(
    template_lib: TemplateLibrary, style_tag: str
) -> dict | None:
    """同 _resolve_vip_transition,用于视频特效。"""
    placeholder = template_lib.pick_video_effect(style_tag)
    if not placeholder:
        return None
    name = placeholder.get("name")
    if not name:
        return placeholder
    vip = template_lib.pick_video_effect_by_name(name)
    if vip is None:
        return placeholder
    out = dict(vip)
    out.setdefault("name", name)
    return out


def inject_fx(state: WorkflowState) -> dict:
    """读取草稿 → 加载模板 → 在分镜边界注入转场 → 原子写回。"""
    draft_path = Path(state["draft_path"])
    draft = _load_draft(draft_path)
    template_lib = load_resource_libraries(
        fx_template="templates/fx_template.json",
        fx_resource="templates/fx_resource_library.json",
        text_resource="templates/text_resource_library.json",
    )

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

    warning_msgs: list[str] = []
    for i in range(n_boundaries):
        style_tag = (
            (shot_plan or {}).get("shots", [{}])[i].get("style_tag", "default")
            if isinstance(shot_plan, dict)
            else "default"
        )
        trans_obj = _resolve_vip_transition(template_lib, style_tag)
        if not trans_obj:
            warning_msgs.append(f"[node_09] boundary {i + 1} 未找到可用转场,跳过")
            continue
        if not _VIP_ID_PATTERN.match(trans_obj.get("resource_id", "")):
            warning_msgs.append(
                f"[node_09] boundary {i + 1} resource_id 不是 19 位数字 "
                f"({trans_obj.get('resource_id')!r}),沿用占位"
            )
        trans_obj = dict(trans_obj)
        trans_obj["id"] = f"transition-{i + 1}"
        transitions.append(trans_obj)

    # 视频特效:每个追加一次(全局,不重复),与 Week 3 行为一致
    effect_obj = _resolve_vip_video_effect(template_lib, "default")
    if effect_obj:
        if not _VIP_ID_PATTERN.match(effect_obj.get("resource_id", "")):
            warning_msgs.append(
                f"[node_09] video_effect resource_id 不是 19 位数字 "
                f"({effect_obj.get('resource_id')!r}),沿用占位"
            )
        effect_obj = dict(effect_obj)
        effect_obj["id"] = f"vfx-{len(video_effects) + 1}"
        video_effects.append(effect_obj)
    else:
        warning_msgs.append("[node_09] 未找到可用视频特效,跳过")

    atomic_write_draft(draft_path, draft)

    log = list(state.get("status_log", []) or []) + ["node_09_inject_fx_done"]
    errors = list(state.get("error_log", []) or [])
    errors.extend(warning_msgs)
    return {**state, "status_log": log, "error_log": errors}
