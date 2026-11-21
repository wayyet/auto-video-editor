"""剪映原生 FX 素材构造器(2026-11-09 阶段 3/4/5)。

2026-11-22 SDK 链转正(六段 legacy 链退役):转场/贴纸/花字的 legacy 原生
构造器(``build_native_transition_material`` / ``build_native_sticker_material``
/ ``build_native_text_effect_material``)与配套清理前缀常量已删除 —— 这三段
效果经 DraftBuildSpec → node_11 pyJianYingDraft SDK 编译落草稿。本模块**保留**
仍有原生写入方的构造器:开场钩子 node_09b(视频特效/文本动画)、node_13 音量
淡入淡出、node_09b 的动画缓存守卫,以及全仓共用的 ``is_19_digit_resource_id``
判据(SDK 编译器/QA/web/回填脚本同源)。

目标形状来源(按可信度分层):

- **已实证**:segment 34 键形态与 ``extra_material_refs`` 引用通道取自本机剪映
  5.9.0.11632 真实样本(``storyline/native_draft.py`` 同源)。resource_id/
  effect_id/md5/时长元数据来自 ``vendor/pyJianYingDraft/metadata``,19 位数字
  resource_id。
- **待 GUI diff 复核**(字段名/取值以真实剪映 5.9.0.11632 手工样本为准):
  - video_effect 的 ``apply_target_type``/``enable_mask``/``render_index``/``value``;
  - material_animations 条目的 ``animations[].start/duration`` 单位与占位键;
  - audio_fades 的曲线类型枚举(现按 linear)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: audio_fades 我方 id 用字符串前缀判断(node_13 清理旧产物时只动这些,
#: 人工在剪映里加的同类素材 id 是 uuid,不会被误删)。
OUR_AUDIO_FADE_PREFIX = "audio-fade-"


def is_19_digit_resource_id(resource_id: Any) -> bool:
    """resource_id 是否为剪映 19 位数字 ID(占位/缺失 → False)。"""
    return (
        isinstance(resource_id, str)
        and len(resource_id) == 19
        and resource_id.isdigit()
    )


def build_native_video_effect_material(
    *,
    material_id: str,
    meta: dict[str, Any],
    value: float = 1.0,
) -> dict[str, Any]:
    """视频特效元数据行 → 原生 ``materials.video_effects`` 条目。

    Args:
        material_id: 确定性 id(约定 ``vfx-{segment 序号}``,一段一个特效素材)。
        meta: 资源库行(name/resource_id/effect_id/md5)。
        value: 强度(0~1;fx_template.json 的 intensity 由调用方透传)。
    """
    return {
        "id": material_id,
        "type": "video_effect",
        "apply_target_type": 0,  # 待实机复核
        "category_id": "",
        "category_name": "",  # 待实机复核:GUI 样本确认分类
        "effect_id": str(meta.get("effect_id") or ""),
        "enable_mask": True,
        "name": str(meta.get("name") or ""),
        "path": "",
        "platform": "all",
        "render_index": 0,
        "resource_id": str(meta.get("resource_id") or ""),
        "source_platform": 0,
        "start": 0,
        "value": float(value),
        "version": "",
    }


def text_animation_cache_available(
    anim_meta: dict[str, Any],
    cache_root: str | Path | None,
) -> bool:
    """动画元数据(effect_id+md5)对应的剪映本地缓存目录是否存在。

    剪映按 ``Cache/effect/<effect_id>/<md5>`` 解析文字动画资源;目录缺失时剪映
    会在**打开草稿时**按需下载,下载不完整/失败即「动画丢失」(2026-11-19
    根因实证:螺旋上升只落 4 个不完整文件)。写入侧先查,查不到就不承诺动画。

    注意:目录存在≠一定可渲染(坏包也占目录),所以本检查只是守卫的一半,
    另一半是池条目的 ``verified`` 标志(只能由 GUI 实证授予)。
    """
    effect_id = str(anim_meta.get("effect_id") or "")
    md5 = str(anim_meta.get("md5") or "")
    if not effect_id or not md5 or not cache_root:
        return False
    return (Path(cache_root) / effect_id / md5).is_dir()


def build_native_text_animation_material(
    *,
    material_id: str,
    anim_meta: dict[str, Any],
    kind: str = "in",
    duration_us: int = 0,
) -> dict[str, Any]:
    """文字动画元数据行 → 原生 ``materials.material_animations`` 条目。

    一个 text segment 一条 material_animations 条目;条目 id 挂到 text 轨段
    ``extra_material_refs``(node_10/10b)。

    Args:
        material_id: 确定性 id(约定 ``text-anim-{字幕 material id}``)。
        anim_meta: text_resource_library.json 行(name/duration_s/resource_id/effect_id/md5)。
        kind: "in" / "loop" / "out"。
        duration_us: 动画时长(µs);0 时按 anim_meta.duration_s 换算。
    """
    duration = int(duration_us) or int(round(float(anim_meta.get("duration_s") or 0.0) * 1_000_000))
    return {
        "id": material_id,
        "type": "sticker_animation",
        "multi_language_current": "none",
        "animations": [
            {
                "id": f"{material_id}:{kind}",
                "name": str(anim_meta.get("name") or ""),
                "type": kind,
                "start": 0,  # 待实机复核:相对片段起点的偏移口径
                "duration": duration,
                "resource_id": str(anim_meta.get("resource_id") or ""),
                "effect_id": str(anim_meta.get("effect_id") or ""),
                "md5": str(anim_meta.get("md5") or ""),
                "platform": "all",
                "category_id": "",
                "category_name": "",
                "material_type": "",
                "path": "",
                "trailable": False,
                "visible": False,
                "has_virtual": False,
                "matting_backwards": False,
            }
        ],
    }


def build_native_audio_fade_material(
    *,
    material_id: str,
    fade_in_us: int,
    fade_out_us: int,
) -> dict[str, Any]:
    """淡入淡出 → 原生 ``materials.audio_fades`` 条目(µs;曲线待实机复核)。

    条目 id 挂到音频 segment ``extra_material_refs``(node_13);不使用旧占位键
    ``fade_in_duration_us``/``track_id``。
    """
    return {
        "id": material_id,
        "type": "audio_fade",
        "fade_in_duration": int(max(0, fade_in_us)),
        "fade_out_duration": int(max(0, fade_out_us)),
        "fade_in_type": "linear",  # 待实机复核:曲线类型枚举
        "fade_out_type": "linear",
    }


def flower_effect_cache_available(
    flower_meta: dict[str, Any],
    cache_root: str | Path | None,
) -> bool:
    """花字元数据(effect_id+md5)对应的剪映本地缓存目录是否存在。

    花字资源包按 ``Cache/artistEffect/<effect_id>/<md5>`` 解析(**独立于**字幕
    动画的 ``Cache/effect`` —— 审查 R-04,`JIANYING_ARTIST_EFFECT_CACHE_ROOT`),
    Golden Sample 草稿 path 字段与 flower_cache_inventory.json 双重实证。

    注意:目录存在≠一定可渲染,本检查只是守卫的一半;另一半是池条目的
    ``verified`` + ``verification.cache_manifest_sha256`` 完整性比对
    (:func:`nodes._text_flower.flower_entry_status`,审查 R-05)。
    """
    effect_id = str(flower_meta.get("effect_id") or "")
    md5 = str(flower_meta.get("md5") or "")
    if not effect_id or not md5 or not cache_root:
        return False
    return (Path(cache_root) / effect_id / md5).is_dir()


__all__ = [
    "OUR_AUDIO_FADE_PREFIX",
    "build_native_audio_fade_material",
    "build_native_text_animation_material",
    "build_native_video_effect_material",
    "flower_effect_cache_available",
    "is_19_digit_resource_id",
    "text_animation_cache_available",
]
