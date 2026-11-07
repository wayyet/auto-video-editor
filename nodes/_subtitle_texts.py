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

from jy_common.native_text import (
    TEXT_MATERIAL_TYPE,
    build_text_content_json,
    text_check_flag,
)

#: text 轨 render_index(pyJianYingDraft 口径:video 0 / audio 0 / text 15000)
_TEXT_TRACK_RENDER_INDEX = 15000


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
) -> dict[str, Any]:
    """构造一条 ``materials.texts`` 条目(微秒时间轴;原生键+私有键双形态)。

    Args:
        index: 从 0 开始的序号,生成 id 为 ``f"{id_prefix}-{index + 1}"``。
        content: 字幕文本。
        start_us / duration_us: ``target_timerange`` 起点与时长(微秒)。
        style: 样式 dict;内部做 deepcopy,每条字幕互不污染。单位是剪映原生
            口径(size 5.0 / border.width 40.0,见 node_08 校准注释)。
        track_name: 轨道名(中文 ``"Subtitles"``,英文 ``"Subtitles_en"``)。
        id_prefix: id 前缀(中文 ``"text"``,英文 ``"text-en"``)。
        extra: 追加字段(英文用来写 ``text_en``,与 16a 的 marker 约定一致)。
    """
    style_copy = copy.deepcopy(style)
    item: dict[str, Any] = {
        # ---- 原生必填键(剪映识别用)----
        "id": f"{id_prefix}-{index + 1}",
        "content": build_text_content_json(content, style_copy),
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


def build_text_track(text_materials: list[dict[str, Any]]) -> dict[str, Any]:
    """由 material 列表(含私有 target_timerange/style)构造原生 text 轨道。

    幂等约定:调用方负责**整轨替换**(与 materials.texts 整表替换同口径),
    重放得到同一结果。
    """
    segments: list[dict[str, Any]] = []
    for i, mat in enumerate(text_materials):
        if not isinstance(mat, dict):
            continue
        tr = mat.get("target_timerange") or {}
        start_us = int(tr.get("start") or 0)
        duration_us = int(tr.get("duration") or 0)
        if duration_us <= 0:
            continue
        segments.append(
            make_text_segment(
                index=i,
                material_id=str(mat.get("id") or ""),
                start_us=start_us,
                duration_us=duration_us,
                style=mat.get("style"),
            )
        )
    return {
        "attribute": 0,
        "flag": 0,
        "id": "track-text-1",
        "is_default_name": True,
        "name": "",
        "segments": segments,
        "type": "text",
    }


def sync_text_track(draft: dict[str, Any], text_materials: list[dict[str, Any]]) -> None:
    """把 text 轨道**整轨替换**为与 materials.texts 一致的形状(就地修改 draft)。

    幂等:重放(整表替换 texts + 整轨替换 track)得到同一结果;其余轨道不动。
    """
    tracks = draft.setdefault("tracks", []) or []
    kept = [t for t in tracks if isinstance(t, dict) and t.get("type") != "text"]
    kept.append(build_text_track(text_materials))
    draft["tracks"] = kept


__all__ = [
    "build_text_track",
    "make_text_material",
    "make_text_segment",
    "sync_text_track",
]
