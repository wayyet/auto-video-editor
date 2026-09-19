"""节点 10:inject_text_fx — 花字样式追加(对应原文档 4.5 节)。

对 ``materials.texts`` 中每条字幕追加样式字段(描边/投影/入场动画)。
Week 4 升级后:从 ``templates/text_resource_library.json`` 取真实 VIP 入场动画,
entrance_animation 从 None 改为含 resource_id 的 dict(剪映客户端会按 resource_id 联网下载)。

资源库缺失或未命中时,entrance_animation 仍为 None(降级,与 Week 3 行为一致)。
"""

from __future__ import annotations

import json
from pathlib import Path

from draft_ops.atomic_writer import atomic_write_draft
from jy_common.template_library import load_resource_libraries
from state import WorkflowState


# 默认入场动画名 — 优先取第一/前几个 VIP 入场动画(常见"打字机"/"渐显")。
# 若该 name 在资源库中未命中,降级为 None。
_DEFAULT_INTRO_NAMES = ("居中打字机", "渐次出现", "渐显", "打字机 II")


def _pick_default_intro_animation(template_lib) -> dict | None:
    """按顺序尝试默认名字,首个命中即返回。"""
    if template_lib.is_empty or not template_lib.has_text_lib:
        return None
    for name in _DEFAULT_INTRO_NAMES:
        anim = template_lib.pick_text_animation("intro", name)
        if anim is not None:
            return anim
    # 全都没命中 → 取资源库里第一个 VIP 入场动画
    intro_list = template_lib.list_text_animations("intro", vip_only=True)
    if not intro_list:
        return None
    return template_lib.pick_text_animation("intro", intro_list[0])


def _load_draft(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def inject_text_fx(state: WorkflowState) -> dict:
    """读取草稿 → 为每条字幕追加描边/投影/入场动画字段 → 原子写回。"""
    draft_path = Path(state["draft_path"])
    draft = _load_draft(draft_path)
    template_lib = load_resource_libraries(
        fx_template="templates/fx_template.json",
        fx_resource="templates/fx_resource_library.json",
        text_resource="templates/text_resource_library.json",
    )

    default_style = template_lib.default_text_style() if not template_lib.is_empty else {}
    # 兜底样式:即使模板库缺失,也写最小可用样式
    if not default_style:
        default_style = {
            "outline": True,
            "shadow": True,
            "entrance_animation": None,
        }

    # Week 4 升级:把 entrance_animation 替换成真实 VIP 入场动画对象
    if default_style.get("entrance_animation") is None:
        # 优先读模板里声明的 entrance_animation_name
        target_name = default_style.pop("entrance_animation_name", None)
        if target_name and not template_lib.is_empty and template_lib.has_text_lib:
            anim = template_lib.pick_text_animation("intro", target_name)
            if anim is not None:
                default_style["entrance_animation"] = anim
        if default_style.get("entrance_animation") is None:
            vip_anim = _pick_default_intro_animation(template_lib)
            if vip_anim is not None:
                default_style["entrance_animation"] = vip_anim

    texts = draft.get("materials", {}).get("texts", [])
    for text_obj in texts:
        style = text_obj.setdefault("style", {})
        style.update(default_style)

    atomic_write_draft(draft_path, draft)

    log = list(state.get("status_log", []) or []) + ["node_10_inject_text_fx_done"]
    return {**state, "status_log": log}
