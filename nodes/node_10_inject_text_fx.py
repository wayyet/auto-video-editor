"""节点 10:inject_text_fx — 花字样式追加(对应原文档 4.5 节)。

对 ``materials.texts`` 中每条字幕追加样式字段(描边/投影/入场动画)。
Week 3 仅写基础字段;``entrance_animation`` 默认 None,
Week 4 从 ``AVAILABLE_ASSETS.md`` 选定具体合法枚举值(原文档 9 节"可承受延后")。
"""

from __future__ import annotations

import json
from pathlib import Path

from draft_ops.atomic_writer import atomic_write_draft
from jy_common.template_library import load_template_library
from state import WorkflowState


def _load_draft(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def inject_text_fx(state: WorkflowState) -> dict:
    """读取草稿 → 为每条字幕追加描边/投影/入场动画字段 → 原子写回。"""
    draft_path = Path(state["draft_path"])
    draft = _load_draft(draft_path)
    template_lib = load_template_library("templates/text_style_template.json")

    default_style = template_lib.default_text_style() if not template_lib.is_empty else {}
    # 兜底样式:即使模板库缺失,也写最小可用样式
    if not default_style:
        default_style = {
            "outline": True,
            "shadow": True,
            "entrance_animation": None,
        }

    texts = draft.get("materials", {}).get("texts", [])
    for text_obj in texts:
        style = text_obj.setdefault("style", {})
        style.update(default_style)

    atomic_write_draft(draft_path, draft)

    log = list(state.get("status_log", []) or []) + ["node_10_inject_text_fx_done"]
    return {**state, "status_log": log}