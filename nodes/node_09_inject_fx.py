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

import re
from pathlib import Path

from jy_common.template_library import TemplateLibrary, load_resource_libraries
from nodes._draft_io import apply_and_write, load_draft
from nodes._dual_draft import ZH, dual_draft_targets, missing_en_note
from state import WorkflowState


_VIP_ID_PATTERN = re.compile(r"^\d{19}$")


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


def _upsert_by_id(items: list[dict], item: dict) -> None:
    """按 ``id`` 覆盖写入,而不是 append(幂等:重放不会叠出重复条目)。

    2026-10 K2=B 加固:本节点现在横跨关卡②,resume 路径变多,纯 append 会让
    ``materials.transitions`` / ``video_effects`` 每重放一次就多一份。
    """
    item_id = item.get("id")
    for idx, existing in enumerate(items):
        if existing.get("id") == item_id:
            items[idx] = item
            return
    items.append(item)


def _inject_fx_into(
    draft: dict,
    template_lib: TemplateLibrary,
    shot_plan: dict | None,
    tag: str,
) -> list[str]:
    """把转场 + 视频特效注入**一份**草稿(就地改 ``draft``),返回告警列表。

    中英两份草稿共用这一个实现(``tag`` 只用于日志前缀)。

    转场数量只取决于草稿自身的 video segments 与 ``shot_plan``:两份草稿的
    视频时间轴完全一致(fork 自同一份快照②),所以两边会得到**同样的条数**。
    """
    warning_msgs: list[str] = []

    materials = draft.setdefault("materials", {})
    transitions = materials.setdefault("transitions", [])
    video_effects = materials.setdefault("video_effects", [])

    video_track = _find_video_track(draft)
    n_boundaries = _boundary_count(video_track, shot_plan)

    for i in range(n_boundaries):
        style_tag = (
            (shot_plan or {}).get("shots", [{}])[i].get("style_tag", "default")
            if isinstance(shot_plan, dict)
            else "default"
        )
        trans_obj = _resolve_vip_transition(template_lib, style_tag)
        if not trans_obj:
            warning_msgs.append(f"[{tag}] boundary {i + 1} 未找到可用转场,跳过")
            continue
        if not _VIP_ID_PATTERN.match(trans_obj.get("resource_id", "")):
            warning_msgs.append(
                f"[{tag}] boundary {i + 1} resource_id 不是 19 位数字 "
                f"({trans_obj.get('resource_id')!r}),沿用占位"
            )
        trans_obj = dict(trans_obj)
        trans_obj["id"] = f"transition-{i + 1}"
        _upsert_by_id(transitions, trans_obj)

    # 视频特效:固定 id 的单条全局特效(按 id 覆盖 → 重放幂等)
    effect_obj = _resolve_vip_video_effect(template_lib, "default")
    if effect_obj:
        if not _VIP_ID_PATTERN.match(effect_obj.get("resource_id", "")):
            warning_msgs.append(
                f"[{tag}] video_effect resource_id 不是 19 位数字 "
                f"({effect_obj.get('resource_id')!r}),沿用占位"
            )
        effect_obj = dict(effect_obj)
        effect_obj["id"] = "vfx-default"
        _upsert_by_id(video_effects, effect_obj)
    else:
        warning_msgs.append(f"[{tag}] 未找到可用视频特效,跳过")

    return warning_msgs


def inject_fx(state: WorkflowState) -> dict:
    """读取草稿 → 加载模板 → 在分镜边界注入转场 → 原子写回。

    K2=B(2026-10):对**中文主草稿 + 英文分支草稿**各注入一次(见 ``nodes/_dual_draft.py``)。
    两份草稿在同一节点内顺序处理,不跨超步,所以返回值契约不变(整份 state),
    也不会给 LangGraph 引入新的同超步写冲突。

    返回值里的 ``status_log`` / ``error_log`` 与重构前一致地追加在用户已有内容之后,
    只是可能多出英文草稿那一轮的 done_tag 与告警。
    """
    status_log = list(state.get("status_log", []) or [])
    error_log = list(state.get("error_log", []) or [])

    template_lib = load_resource_libraries(
        fx_template="templates/fx_template.json",
        fx_resource="templates/fx_resource_library.json",
        text_resource="templates/text_resource_library.json",
    )

    if template_lib.is_empty:
        status_log.append("node_09_inject_fx_no_template")
        error_log.append("[node_09] 模板库缺失,降级通过(Week 3 可接受)")
        return {**state, "status_log": status_log, "error_log": error_log}

    targets = dual_draft_targets(state)
    if not targets:
        # node_05 早退时草稿不存在(D7,2026-10-20 修):降级跳过,别抛 FileNotFoundError
        # 把整个 worker 带走。
        error_log.append("[node_09] state.draft_path 缺失,跳过转场注入(降级通过)")
        status_log.append("node_09_inject_fx_skipped")
        return {**state, "status_log": status_log, "error_log": error_log}

    shot_plan = state.get("shot_plan")
    en_file = Path(state["draft_dir_en_branch"]) / "draft_content.json" if state.get("draft_dir_en_branch") else None
    for tag, draft_file in targets:
        draft = load_draft(draft_file)
        error_log.extend(_inject_fx_into(draft, template_lib, shot_plan, f"node_09_{tag}"))
        # Week 5:参数从 draft_path 提升为 draft_path.parent,safe_write_draft 双写
        status_log.extend(
            apply_and_write(
                draft_file,
                draft,
                "node_09",
                done_tag=(
                    "node_09_inject_fx_done" if tag == ZH else "node_09_inject_fx_en_done"
                ),
            )
        )

    if len(targets) == 1 and en_file is not None:
        error_log.append(missing_en_note(state))

    return {**state, "status_log": status_log, "error_log": error_log}
