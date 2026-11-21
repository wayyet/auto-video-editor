"""工作流字幕样式 → SDK 文本样式映射(迁移设计文档 §4.1/§6.1;阶段3)。

单位口径(全部是**剪映原生口径**,与 jianying-add-subtitles 真机校准值同源,
见 node_08 ``_JIANYING_DEFAULT_STYLE`` 注释与 ``jy_common/native_text.py``):

- ``size``(5.0)→ ``TextStyle.size``:同一原生单位(SDK 默认 8.0,导出直接进
  content JSON 的 ``styles[0].size``,两侧同形);
- ``color``(0-1 RGB 三元组)→ ``TextStyle.color``:直传;
- ``border.width``(40.0)→ ``TextBorder.width``:SDK 内部做 ``width/100*0.2``
  换算(= 项目 ``STROKE_WIDTH_FACTOR`` 0.002 的同一换算),导出 0.08;
- ``transform_y``(-0.8)→ ``ClipSettings.transform_y``:SDK 单位同为"半个画布高";
- ``align``:项目 material 写死 ``alignment: 1``(居中),映射为 ``TextStyle.align=1``;
- 折行:项目在 spec 侧用 ``subtitle_wrap`` 内嵌 ``\n``,SDK ``auto_wrapping``
  恒 False(渲染口径一致,``type`` 恒为 ``text``)。

验证纪律(§4.1):以真机 golden sample 做同屏 A/B 是操作员任务;结构级
等价(同 style 下 SDK 导出 content JSON == make_text_material 产物)由单测锁定。
"""
from __future__ import annotations

from typing import Any

from jianying_sdk.spec import SubtitleSpec

#: 项目 material 固定键 → SDK TextStyle 参数(material 写死值的同源映射)
_DEFAULT_ALIGN = 1
_DEFAULT_MAX_LINE_WIDTH = 0.82


def style_to_text_style_kwargs(style: dict[str, Any] | None) -> dict[str, Any]:
    """工作流 style dict → ``TextStyle`` 构造 kwargs(纯函数,不构造 SDK 对象)。"""
    style = style or {}
    color = style.get("color") or [1.0, 1.0, 1.0]
    if not isinstance(color, (list, tuple)) or len(color) != 3:
        color = [1.0, 1.0, 1.0]
    align = style.get("align")
    if align not in (0, 1, 2):
        align = _DEFAULT_ALIGN
    return {
        "size": float(style.get("size") or 5.0),
        "bold": bool(style.get("bold") or False),
        "italic": bool(style.get("italic") or False),
        "underline": bool(style.get("underline") or False),
        "color": (float(color[0]), float(color[1]), float(color[2])),
        "alpha": 1.0,
        "align": int(align),
        "letter_spacing": 0,
        "line_spacing": 0,  # SDK 导出 0.02 基底,与项目 material line_spacing=0.02 一致
        "auto_wrapping": False,  # 折行由 subtitle_wrap 内嵌 \n 完成,SDK 不自动换行
        "max_line_width": _DEFAULT_MAX_LINE_WIDTH,
    }


def border_kwargs(style: dict[str, Any] | None) -> dict[str, Any] | None:
    """工作流 ``style.border`` → ``TextBorder`` kwargs;width<=0 时 None(无描边)。"""
    style = style or {}
    border = style.get("border") or {}
    width = float(border.get("width") or 0.0)
    if width <= 0:
        return None
    color = border.get("color") or [0.0, 0.0, 0.0]
    if not isinstance(color, (list, tuple)) or len(color) != 3:
        color = [0.0, 0.0, 0.0]
    return {
        "alpha": 1.0,
        "color": (float(color[0]), float(color[1]), float(color[2])),
        "width": width,
    }


def transform_y_of(style: dict[str, Any] | None) -> float:
    """字幕位置下沉位(material 私有 style.transform_y,默认 -0.8)。"""
    style = style or {}
    try:
        return float(style.get("transform_y") if style.get("transform_y") is not None else -0.8)
    except (TypeError, ValueError):
        return -0.8


def subtitle_text_segment_kwargs(sub: SubtitleSpec) -> dict[str, Any]:
    """SubtitleSpec → ``TextSegment`` 构造 kwargs(style/border/clip_settings)。

    文本本体由编译器取 ``wrap_content or text``;这里只出样式参数。
    """
    style = sub.style or {}
    kwargs: dict[str, Any] = {
        "style": style_to_text_style_kwargs(style),
    }
    border = border_kwargs(style)
    if border:
        kwargs["border"] = border
    kwargs["clip_settings"] = {"transform_y": transform_y_of(style)}
    return kwargs


__all__ = [
    "border_kwargs",
    "style_to_text_style_kwargs",
    "subtitle_text_segment_kwargs",
    "transform_y_of",
]
