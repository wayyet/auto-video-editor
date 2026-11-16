"""nodes/_cover_title.resolve_zh_title 单测(2026-11-17 P2,修订计划 §5.6)。

覆盖:原生文本轨计时权威 / 素材数组顺序变化不改变标题 / 旧形态兜底 /
native JSON content 双形态解析 / 无标题占位 / 英文与品牌名原样保留 / 同 start 稳定序。
"""

from __future__ import annotations

import json

from nodes._cover_title import DEFAULT_TITLE, resolve_zh_title


def _native_text_material(mid: str, text: str) -> dict:
    return {
        "id": mid,
        "type": "text",
        "content": json.dumps({"styles": [{}], "text": text}, ensure_ascii=False),
    }


def test_native_track_earliest_start_wins() -> None:
    """原生形态:文本轨最早 start 的素材是标题,与数组顺序无关。"""
    draft = {
        "materials": {"texts": [
            _native_text_material("text-zh-b", "第二句"),
            _native_text_material("text-zh-a", "开场标题"),
        ]},
        "tracks": [{"type": "text", "segments": [
            {"material_id": "text-zh-b", "target_timerange": {"start": 3_000_000, "duration": 1_000_000}},
            {"material_id": "text-zh-a", "target_timerange": {"start": 0, "duration": 1_000_000}},
        ]}],
    }
    assert resolve_zh_title(draft) == "开场标题"


def test_materials_order_change_does_not_change_title() -> None:
    """materials.texts 数组顺序打乱 → 标题不变(修订计划 §5.6 核心诉求)。"""
    base_tracks = [{"type": "text", "segments": [
        {"material_id": "text-zh-a", "target_timerange": {"start": 0, "duration": 1_000_000}},
        {"material_id": "text-zh-b", "target_timerange": {"start": 3_000_000, "duration": 1_000_000}},
    ]}]
    mats_a = [_native_text_material("text-zh-a", "开场标题"), _native_text_material("text-zh-b", "第二句")]
    mats_b = list(reversed(mats_a))
    assert resolve_zh_title({"materials": {"texts": mats_a}, "tracks": base_tracks}) == \
        resolve_zh_title({"materials": {"texts": mats_b}, "tracks": base_tracks}) == \
        "开场标题"


def test_legacy_form_uses_own_timerange() -> None:
    """旧形态(链路自产、未经剪映保存):texts 自带 target_timerange,最早者胜。"""
    draft = {"materials": {"texts": [
        {"id": "t1", "content": "后句", "target_timerange": {"start": 5_000_000, "duration": 1_000_000}},
        {"id": "t2", "content": "标题句", "target_timerange": {"start": 0, "duration": 1_000_000}},
    ]}}
    assert resolve_zh_title(draft) == "标题句"


def test_plain_content_supported() -> None:
    """旧形态纯文本 content(非 JSON)同样可解析。"""
    draft = {"materials": {"texts": [{"id": "t1", "content": "纯文本标题"}]}}
    assert resolve_zh_title(draft) == "纯文本标题"


def test_no_texts_returns_default() -> None:
    assert resolve_zh_title({"materials": {"texts": []}}) == DEFAULT_TITLE
    assert resolve_zh_title({}) == DEFAULT_TITLE


def test_english_and_brand_names_kept() -> None:
    """标题含英文/品牌名 → 原样返回(不由 resolver 改写)。"""
    draft = {"materials": {"texts": [{"id": "t1", "content": "iPhone 17 发布会速览"}]}}
    assert resolve_zh_title(draft) == "iPhone 17 发布会速览"


def test_tie_break_by_material_id_stable() -> None:
    """start 相同 → 按 material_id 稳定排序(与数组顺序无关)。"""
    draft = {
        "materials": {"texts": [
            _native_text_material("text-zh-b", "B 标题"),
            _native_text_material("text-zh-a", "A 标题"),
        ]},
        "tracks": [{"type": "text", "segments": [
            {"material_id": "text-zh-b", "target_timerange": {"start": 0, "duration": 1_000_000}},
            {"material_id": "text-zh-a", "target_timerange": {"start": 0, "duration": 1_000_000}},
        ]}],
    }
    assert resolve_zh_title(draft) == "A 标题"


def test_empty_display_text_skipped() -> None:
    """content 为空/空白 → 跳过,继续找后续素材。"""
    draft = {"materials": {"texts": [
        {"id": "t1", "content": "   "},
        {"id": "t2", "content": "有效标题"},
    ]}}
    assert resolve_zh_title(draft) == "有效标题"
