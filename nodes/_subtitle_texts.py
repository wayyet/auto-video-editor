"""字幕文本素材构造 — node_08(中文)与 node_08b(英文)共用。

只负责"一条 materials.texts 条目 + text 轨道段长什么样",不碰 I/O、不碰 state。

2026-11-07 文本链原生升级:material 在保留工作流私有键(target_timerange/
style/track_name/text_en,供 SRT 重建/导出/16a marker 继续消费)的同时补齐
剪映原生必填键(content=styles JSON 字符串等,字段模板见
``jy_common/native_text.py`` docstring);并新增 :func:`make_text_segment` /
:func:`build_text_track` 把字幕铺到原生 **text 轨道**上 —— 关卡③ 人工才能在
剪映时间轴里看到并修改英文字幕(此前字幕只躺在 materials 桶里,剪映不可见)。
"""

from __future__ import annotations

import copy
from typing import Any

import config
from jy_common.native_text import (
    TEXT_MATERIAL_TYPE,
    build_text_content_json,
    native_content_text,
    text_check_flag,
)
from jy_common.subtitle_wrap import layout_warnings, wrap_subtitle_text

#: text 轨 render_index(pyJianYingDraft 口径:video 0 / audio 0 / text 15000)
_TEXT_TRACK_RENDER_INDEX = 15000


def canvas_width_from_draft(draft: dict[str, Any] | None) -> int | None:
    """从草稿 dict 读 ``canvas_config.width``;非法/缺失返回 None(调用方回落 config)。

    2026-11-18 P2-03:折行宽度优先用**目标草稿自身的画布**,config.CANVAS_WIDTH
    (1080)只作兼容回退 —— 以后支持多画布草稿时,不能把配置宽度错用到
    不同画布的草稿上。
    """
    if not isinstance(draft, dict):
        return None
    try:
        width = int((draft.get("canvas_config") or {}).get("width") or 0)
    except (TypeError, ValueError):
        return None
    return width if width > 0 else None


def make_text_material(
    *,
    index: int,
    content: str,
    start_us: int,
    duration_us: int,
    style: dict[str, Any],
    track_name: str,
    id_prefix: str = "text",
    extra: dict[str, Any] | None = None,
    canvas_width: int | None = None,
) -> dict[str, Any]:
    """构造一条 ``materials.texts`` 条目(微秒时间轴;原生键+私有键双形态)。

    Args:
        index: 从 0 开始的序号,生成 id 为 ``f"{id_prefix}-{index + 1}"``。
        content: 字幕文本(**原文**)。写进原生 content 前会按画布宽×字号
            自动折行(超宽处内嵌 ``\\n``,见 :mod:`jy_common.subtitle_wrap`);
            私有键(text_zh/text_en)保持原文 —— 16a 翻译指纹与 SRT 语义
            不受影响。
        start_us / duration_us: ``target_timerange`` 起点与时长(微秒)。
        style: 样式 dict;内部做 deepcopy,每条字幕互不污染。单位是剪映原生
            口径(size 5.0 / border.width 40.0,见 node_08 校准注释)。
        track_name: 轨道名(中文 ``"Subtitles"``,英文 ``"Subtitles_en"``)。
        id_prefix: id 前缀(中文 ``"text"``,英文 ``"text-en"``)。
        extra: 追加字段(英文用来写 ``text_en``,与 16a 的 marker 约定一致)。
        canvas_width: 目标草稿画布宽(px);None/非法回落 ``config.CANVAS_WIDTH``
            (2026-11-18 P2-03:优先草稿自身画布,配置值只作兼容回退)。
    """
    style_copy = copy.deepcopy(style)
    # 2026-11-18 折行:渲染 content 按画布宽×字号内嵌 \n,修复单行超宽被画布
    # 两端裁剪(真机 AutoVideo_console-31a8a8e9edd2 实证;根因/标定见
    # jy_common/subtitle_wrap.py docstring)。只折渲染口径 —— 调用方经 extra
    # 写入的私有键(text_zh/text_en)是原文,翻译/导出链照旧。
    effective_width = canvas_width if isinstance(canvas_width, int) and canvas_width > 0 else config.CANVAS_WIDTH
    render_text = wrap_subtitle_text(
        content,
        size=float(style_copy.get("size") or 5.0),
        canvas_width=effective_width,
    )
    item: dict[str, Any] = {
        # ---- 原生必填键(剪映识别用)----
        "id": f"{id_prefix}-{index + 1}",
        "content": build_text_content_json(render_text, style_copy),
        "typesetting": 0,
        "alignment": 1,
        "letter_spacing": 0.0,
        "line_spacing": 0.02,
        "line_feed": 1,
        "line_max_width": 0.82,
        "force_apply_line_max_width": False,
        "check_flag": text_check_flag(style_copy),
        "type": TEXT_MATERIAL_TYPE,
        "global_alpha": 1.0,
        # ---- 工作流私有键(下游消费;剪映保存后丢弃,读取侧有原生兜底)----
        "target_timerange": {"start": int(start_us), "duration": int(duration_us)},
        "style": style_copy,
        "track_name": track_name,
    }
    if extra:
        item.update(extra)
    # 2026-11-18 P2-04:折行后布局质量告警(行数/垂直占位/阅读速度)。
    # 只打印提示,不改内容、不重排时间轴 —— 超长 cue 是否重切属另案设计变更。
    try:
        cues = layout_warnings(
            render_text,
            size=float(style_copy.get("size") or 5.0),
            canvas_width=effective_width,
            duration_us=int(duration_us) if duration_us else None,
        )
    except Exception:  # pragma: no cover - 告警是尽力而为,不得影响素材构造
        cues = []
    for w in cues:
        print(f"[subtitle-layout] {item['id']}: {w}")
    return item


def make_text_segment(
    *,
    index: int,
    material_id: str,
    start_us: int,
    duration_us: int,
    style: dict[str, Any] | None = None,
    id_prefix: str = "text-seg",
) -> dict[str, Any]:
    """构造一条原生 **text 轨道段**(34 键形态;render_index 15000)。

    键集与真实样本 segment 一致;文本段差异键:clip 携带 transform.y 下沉位
    (默认 -0.8),hdr_settings/uniform_scale 与样本 effect 段同形态置 None。
    """
    style = style or {}
    seg_id = f"{id_prefix}-{index + 1}"
    return {
        "caption_info": None,
        "cartoon": False,
        "clip": {
            "alpha": 1.0,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": float(style.get("transform_y") or -0.8)},
        },
        "common_keyframes": [],
        "enable_adjust": False,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": False,
        "enable_smart_color_adjust": False,
        "extra_material_refs": [],
        "group_id": "",
        "hdr_settings": None,
        "id": seg_id,
        "intensifies_audio": False,
        "is_placeholder": False,
        "is_tone_modify": False,
        "keyframe_refs": [],
        "last_nonzero_volume": 1.0,
        "material_id": material_id,
        "render_index": _TEXT_TRACK_RENDER_INDEX,
        "responsive_layout": {
            "enable": False, "horizontal_pos_layout": 0, "size_layout": 0,
            "target_follow": "", "vertical_pos_layout": 0,
        },
        "reverse": False,
        "source_timerange": None,
        "speed": 1.0,
        "target_timerange": {"start": int(start_us), "duration": int(duration_us)},
        "template_id": "",
        "template_scene": "default",
        "track_attribute": 0,
        "track_render_index": 0,
        "uniform_scale": None,
        "visible": True,
        "volume": 1.0,
    }


# 2026-11-22 SDK 链转正:legacy 的 build_text_track / sync_text_track(原生
# text 轨道构造/整轨替换)已删除 —— 字幕经 subtitle_specs_from_materials 进
# DraftBuildSpec,node_11 SDK 编译统一落轨;开场钩子 node_09b 的标题轨自拼
# 不经此模块轨道函数(见其模块 docstring)。


def subtitle_specs_from_materials(
    text_materials: list[dict[str, Any]],
    *,
    language: str = "zh",
) -> list["SubtitleSpec"]:
    """本节点 materials.texts 条目 → SDK 编译计划(第10期阶段3)。

    material 双形态里现成携带全部所需数据:id(subtitle_id)/
    target_timerange(时间)/ style(私有键,style_mapper 输入)/ 原文私有键
    (text_zh/text_en)/ content JSON(折行后的渲染文本)。SDK 编译器据此构建
    ``TextSegment``;业务语义不变,只是草稿对象的构建职责移交给 SDK。

    Returns:
        ``SubtitleSpec`` 列表(时长非正的条目跳过;空输入返回空列表)。
    """
    from jianying_sdk.spec import SubtitleSpec

    specs: list[SubtitleSpec] = []
    for mat in text_materials:
        if not isinstance(mat, dict):
            continue
        tr = mat.get("target_timerange") or {}
        start_us = int(tr.get("start") or 0)
        duration_us = int(tr.get("duration") or 0)
        if duration_us <= 0:
            continue
        lang = "en" if language == "en" else "zh"
        original = str(mat.get("text_en") if lang == "en" else mat.get("text_zh") or "")
        if not original:
            original = native_content_text(mat.get("content")) or ""
        specs.append(
            SubtitleSpec(
                subtitle_id=str(mat.get("id") or f"subtitle-{len(specs) + 1}"),
                text=original,
                start_us=start_us,
                end_us=start_us + duration_us,
                language=lang,
                style=mat.get("style"),
                wrap_content=native_content_text(mat.get("content")),
            )
        )
    return specs


__all__ = [
    "canvas_width_from_draft",
    "make_text_material",
    "make_text_segment",
    "subtitle_specs_from_materials",
]
