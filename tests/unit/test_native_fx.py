"""jy_common/native_fx.py 单测 — 原生 FX 素材构造器的形态与确定性。

2026-11-22 SDK 链转正:转场/贴纸/花字的 legacy 构造器已退役(改走 DraftBuildSpec
→ node_11 SDK 编译),对应测试一并删除;保留构造器 = 开场钩子(node_09b)与
node_13 仍在用的视频特效/文本动画/音频淡入淡出 + 全仓共用判据。

字段 schema 的"待实机复核"部分只锁存在性,不锁取值(取值以剪映 GUI diff 为准)。
"""

from __future__ import annotations

from jy_common.native_fx import (
    OUR_AUDIO_FADE_PREFIX,
    build_native_audio_fade_material,
    build_native_text_animation_material,
    build_native_video_effect_material,
    is_19_digit_resource_id,
    text_animation_cache_available,
)

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
    """我方前缀常量与构造器约定一致(node_13 清理逻辑依赖这一点)。"""
    assert OUR_AUDIO_FADE_PREFIX == "audio-fade-"
    fade = build_native_audio_fade_material(material_id="audio-fade-b", fade_in_us=0, fade_out_us=0)
    assert fade["id"].startswith(OUR_AUDIO_FADE_PREFIX)


# ------------------------------------ 缓存可用性判定边界(2026-11-19 审查 P2-04)


def test_text_animation_cache_available_boundaries(tmp_path) -> None:
    """缓存判定:目录不存在/是文件而非目录/空 effect_id/空 md5/空根 → 一律 False。"""
    root = tmp_path / "cache"
    root.mkdir()
    meta_ok = {"effect_id": "E1", "md5": "M1"}
    assert text_animation_cache_available(meta_ok, root) is False, "缓存目录不存在"

    (root / "E1" / "M1").mkdir(parents=True)
    assert text_animation_cache_available(meta_ok, root) is True, "目录存在且为目录"

    (root / "E2").mkdir()
    (root / "E2" / "M2").write_text("not a dir")
    assert text_animation_cache_available({"effect_id": "E2", "md5": "M2"}, root) is False, "md5 路径是文件而非目录"

    assert text_animation_cache_available({"effect_id": "", "md5": "M1"}, root) is False
    assert text_animation_cache_available({"effect_id": "E1", "md5": ""}, root) is False
    assert text_animation_cache_available({"effect_id": None, "md5": None}, root) is False
    assert text_animation_cache_available({}, root) is False
    assert text_animation_cache_available(meta_ok, "") is False, "缓存根为空串"
    assert text_animation_cache_available(meta_ok, None) is False, "缓存根为 None"
    assert text_animation_cache_available(meta_ok, tmp_path / "no-such-root") is False, "缓存根不存在"
