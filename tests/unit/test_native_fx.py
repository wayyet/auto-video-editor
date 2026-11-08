"""jy_common/native_fx.py 单测 — 原生 FX 素材构造器的形态与确定性。

字段 schema 的"待实机复核"部分只锁存在性,不锁取值(取值以剪映 GUI diff 为准,
见 docs/第三期开发 计划文档 §8 阶段 3/4/5 逆向行动项)。
"""

from __future__ import annotations

from jy_common.native_fx import (
    OUR_AUDIO_FADE_PREFIX,
    OUR_TEXT_ANIMATION_PREFIX,
    is_19_digit_resource_id,
    build_native_audio_fade_material,
    build_native_sticker_material,
    build_native_text_animation_material,
    build_native_transition_material,
    build_native_video_effect_material,
)

_TRANSITION_META = {
    "name": "叠化",
    "is_vip": False,
    "resource_id": "6724845717472416269",
    "effect_id": "322577",
    "md5": "2d641adc4bb63e37e3a0067d8c8cc3c3",
    "default_duration_s": 0.5,
    "is_overlap": True,
}

_VFX_META = {
    "name": "抖动模糊",
    "is_vip": True,
    "resource_id": "6934095526273668642",
    "effect_id": "1692216",
    "md5": "aa",
}


def test_is_19_digit_resource_id() -> None:
    assert is_19_digit_resource_id("6724845717472416269")
    assert not is_19_digit_resource_id("PLACEHOLDER_X")
    assert not is_19_digit_resource_id("12345")
    assert not is_19_digit_resource_id(None)
    assert not is_19_digit_resource_id(6724845717472416269)


def test_transition_material_shape() -> None:
    m = build_native_transition_material(
        material_id="transition-1", meta=_TRANSITION_META, duration_us=500_000
    )
    assert m["id"] == "transition-1"
    assert m["type"] == "transition"
    assert m["duration"] == 500_000
    assert m["name"] == "叠化"
    assert m["resource_id"] == "6724845717472416269"
    assert m["effect_id"] == "322577"
    assert m["is_overlap"] is True
    # 待实机复核字段存在即可
    assert "category_id" in m and "category_name" in m


def test_video_effect_material_shape() -> None:
    m = build_native_video_effect_material(
        material_id="vfx-1", meta=_VFX_META, value=0.3
    )
    assert m["id"] == "vfx-1"
    assert m["type"] == "video_effect"
    assert m["resource_id"] == _VFX_META["resource_id"]
    assert m["value"] == 0.3
    for key in ("apply_target_type", "enable_mask", "render_index", "platform", "source_platform"):
        assert key in m


def test_text_animation_material_shape() -> None:
    meta = {
        "name": "新年打字机",
        "is_vip": True,
        "duration_s": 1.0,
        "resource_id": "7165663668530434830",
        "effect_id": "1618962",
        "md5": "cc",
    }
    m = build_native_text_animation_material(material_id="text-anim-text-zh-1", anim_meta=meta)
    assert m["id"] == "text-anim-text-zh-1"
    assert m["type"] == "sticker_animation"
    assert m["multi_language_current"] == "none"
    (anim,) = m["animations"]
    assert anim["type"] == "in"
    assert anim["name"] == "新年打字机"
    assert anim["duration"] == 1_000_000  # duration_s 换算 µs
    assert anim["resource_id"] == meta["resource_id"]
    # 显式时长覆盖 duration_s
    m2 = build_native_text_animation_material(
        material_id="text-anim-x", anim_meta=meta, duration_us=250_000
    )
    assert m2["animations"][0]["duration"] == 250_000


def test_sticker_material_shape() -> None:
    m = build_native_sticker_material(
        material_id="sticker-text-zh-1",
        resource_id="7325648367747338802",
        duration_us=1_500_000,
        name="赞",
    )
    assert m["id"] == "sticker-text-zh-1"
    assert m["type"] == "sticker"
    assert m["resource_id"] == "7325648367747338802"
    assert m["duration"] == 1_500_000


def test_audio_fade_material_shape() -> None:
    m = build_native_audio_fade_material(
        material_id="audio-fade-seg1", fade_in_us=2_000_000, fade_out_us=3_000_000
    )
    assert m["id"] == "audio-fade-seg1"
    assert m["type"] == "audio_fade"
    assert m["fade_in_duration"] == 2_000_000
    assert m["fade_out_duration"] == 3_000_000
    assert m["fade_in_type"] == "linear"  # 待实机复核
    assert m["fade_out_type"] == "linear"
    # 负值钳 0
    m2 = build_native_audio_fade_material(
        material_id="x", fade_in_us=-1, fade_out_us=0
    )
    assert m2["fade_in_duration"] == 0


def test_our_id_prefixes_are_stable() -> None:
    """我方前缀常量与构造器约定一致(清理逻辑依赖这一点)。"""
    assert OUR_TEXT_ANIMATION_PREFIX == "text-anim-"
    assert OUR_AUDIO_FADE_PREFIX == "audio-fade-"
    anim = build_native_text_animation_material(
        material_id="text-anim-a", anim_meta={"name": "x", "resource_id": "1"}
    )
    assert anim["id"].startswith(OUR_TEXT_ANIMATION_PREFIX)
    fade = build_native_audio_fade_material(material_id="audio-fade-b", fade_in_us=0, fade_out_us=0)
    assert fade["id"].startswith(OUR_AUDIO_FADE_PREFIX)
