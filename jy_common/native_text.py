"""剪映原生文本素材的 content JSON 结构与双形态读取(2026-11-07 文本链原生升级)。

原生 ``materials.texts[].content`` 是一段 JSON 字符串(参考实现
isYangs/jianying-editor-skill,MIT;TextSegment.export_material):
``{"styles": [{"fill": {...solid}, "range": [0, n], "size": 8.0, "bold": false,
"strokes": [...]}], "text": "文本"}``。工作流私有形态(重构前的
``content`` = 纯文本 + ``target_timerange`` + ``style``)与原生形态并存:

- **写出侧**(node_08/08b):material 带原生必填键 + 私有键(target_timerange/
  style/track_name/text_en)—— 下游 SRT/导出/标记者继续吃私有键;人工在剪映
  保存后私有键被丢,读取侧走原生兜底(text 轨道段 + content JSON);
- **读取侧**(node_16/checkpoint3/draft_exporter/draft_integrity):一律经
  :func:`text_display_text` 取显示文本,计时优先私有键、缺则落轨道段。

单位口径(见 node_08 ``_JIANYING_DEFAULT_STYLE`` 注释):size 5.0 / border.width
40.0 / transform_y -0.8 是 jianying-add-subtitles 在真实剪映校准过的剪映原生
单位,strokes 宽度按 pyJianYingDraft 口径 ``width/100*0.2`` 换算。
"""

from __future__ import annotations

import json
from typing import Any

#: 描边宽度换算(pyJianYingDraft:TextBorder.width 40.0 → 导出 0.08)
STROKE_WIDTH_FACTOR = 0.002

#: 原生 text material 的固定字段(参考实现模板;style 相关键由写出侧填)
TEXT_MATERIAL_TYPE = "text"


def build_text_content_json(text: str, style: dict[str, Any] | None) -> str:
    """(文本, 工作流 style) → 原生 content JSON 字符串。"""
    style = style or {}
    color = [float(c) for c in (style.get("color") or [1.0, 1.0, 1.0])]
    border = style.get("border") or {}
    stroke_width = float(border.get("width") or 0.0) * STROKE_WIDTH_FACTOR
    block: dict[str, Any] = {
        "fill": {
            "alpha": 1.0,
            "content": {
                "render_type": "solid",
                "solid": {"alpha": 1.0, "color": color},
            },
        },
        "range": [0, len(text)],
        "size": float(style.get("size") or 5.0),
        "bold": bool(style.get("bold") or False),
        "italic": False,
        "underline": False,
    }
    if stroke_width > 0:
        block["strokes"] = [
            {
                "content": {"solid": {"alpha": 1.0, "color": [0.0, 0.0, 0.0]}},
                "width": stroke_width,
            }
        ]
    return json.dumps({"styles": [block], "text": text}, ensure_ascii=False)


def text_check_flag(style: dict[str, Any] | None) -> int:
    """原生 check_flag 位运算(参考实现:基值 7,描边 |8,背景 |16,阴影 |32)。"""
    flag = 7
    style = style or {}
    if float((style.get("border") or {}).get("width") or 0.0) > 0:
        flag |= 8
    return flag


def native_content_text(content: Any) -> str | None:
    """原生 content(JSON 字符串)→ 显示文本;非原生形态返回 None。"""
    if not isinstance(content, str) or not content.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(content)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        return data["text"]
    return None


def text_display_text(entry: dict[str, Any]) -> str:
    """双形态取显示文本:原生 content JSON 解析优先,纯文本形态兜底。"""
    native = native_content_text(entry.get("content"))
    if native is not None:
        return native
    return str(entry.get("content") or "")


__all__ = [
    "STROKE_WIDTH_FACTOR",
    "TEXT_MATERIAL_TYPE",
    "build_text_content_json",
    "native_content_text",
    "text_check_flag",
    "text_display_text",
]
