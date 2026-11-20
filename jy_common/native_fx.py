"""剪映原生 FX 素材构造器(2026-11-09 阶段 3/4/5:转场/视频特效/文字动画/贴纸/淡入淡出)。

目标形状来源(按可信度分层,见下"待实机复核清单"):

- **已实证**:segment 34 键形态与 ``extra_material_refs`` 引用通道取自本机剪映
  5.9.0.11632 真实样本(``storyline/native_draft.py`` 同源);贴纸挂段先例为
  node_11(Week 3 起)。resource_id/effect_id/md5/时长元数据来自
  ``vendor/pyJianYingDraft/metadata``(与本仓 templates/*_resource_library.json
  同源 dump),19 位数字 resource_id。
- **pyJianYingDraft 序列化惯例**:material 的 ``type`` 键、确定性字段命名
  (TransitionMeta/EffectMeta/AnimationMeta 字段 → 原生键)。
- **待 GUI diff 复核**(字段名/取值以真实剪映 5.9.0.11632 手工样本为准,见
  docs/第三期开发 计划文档 §8 阶段 3/4/5 的逆向行动项):
  - transition 的 ``category_id``/``category_name``/``is_overlap``;
  - video_effect 的 ``apply_target_type``/``enable_mask``/``render_index``/``value``;
  - material_animations 条目的 ``animations[].start/duration`` 单位与占位键;
  - audio_fades 的曲线类型枚举(现按 linear);
  - sticker material 全部字段(贴纸当前 fail-closed,真实 ID 回填后才落草稿)。

material id 由调用方显式传入(约定:工作流自产素材用可识别前缀
``transition-N`` / ``vfx-N`` / ``text-anim-*`` / ``audio-fade-*`` / ``sticker-*``,
确定性 → 重放幂等,且前缀模式让清理逻辑能区分"我方旧产物"与人工添加的素材;
人工素材的 id 是剪映 uuid,不落这些前缀)。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: 我方写入的原生素材 id 前缀/模式(node_09/10/11/13 清理旧产物时只动这些,
#: 人工在剪映里加的同类素材 id 是 uuid,不会被误删)。
OUR_TRANSITION_PATTERN = re.compile(r"^transition-\d+$")
OUR_VIDEO_EFFECT_PATTERN = re.compile(r"^vfx-\d+$")
#: material_animations / audio_fades / stickers 我方 id 用字符串前缀判断。
OUR_TEXT_ANIMATION_PREFIX = "text-anim-"
OUR_STICKER_PREFIX = "sticker-"
OUR_AUDIO_FADE_PREFIX = "audio-fade-"

#: 原生花字(2026-11-21)我方标记:material **id** 是 UUIDv5(Golden Sample 同形,
#: 审查 R-03 —— 前缀 id 无 GUI 兼容证据,故不以 id 前缀识别我方),改用私有标记键。
#: 与 materials.texts 条目携带工作流私有字段同一先例;剪映重写草稿若剥掉该键,
#: 清理会退化为"少清理"(孤儿由 QA 挂账),绝不误删人工素材(无标记键)。
OUR_TEXT_FLOWER_PRIVATE_KEY = "autovideo_role"
OUR_TEXT_FLOWER_ROLE = "text-flower"


def is_19_digit_resource_id(resource_id: Any) -> bool:
    """resource_id 是否为剪映 19 位数字 ID(占位/缺失 → False)。"""
    return (
        isinstance(resource_id, str)
        and len(resource_id) == 19
        and resource_id.isdigit()
    )


def build_native_transition_material(
    *,
    material_id: str,
    meta: dict[str, Any],
    duration_us: int,
) -> dict[str, Any]:
    """转场元数据行 → 原生 ``materials.transitions`` 条目。

    Args:
        material_id: 确定性 id(约定 ``transition-{边界序号}``,1-based)。
        meta: 资源库行(name/is_vip/resource_id/effect_id/md5/default_duration_s/is_overlap)。
        duration_us: 转场时长(µs);调用方负责钳制到相邻片段时长内。
    """
    return {
        "id": material_id,
        "type": "transition",
        "duration": int(duration_us),
        "category_id": "",  # 待实机复核:GUI 样本确认分类 id
        "category_name": "转场",
        "effect_id": str(meta.get("effect_id") or ""),
        "name": str(meta.get("name") or ""),
        "resource_id": str(meta.get("resource_id") or ""),
        "is_overlap": bool(meta.get("is_overlap") or False),
    }


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


def build_native_sticker_material(
    *,
    material_id: str,
    resource_id: str,
    duration_us: int,
    name: str = "",
) -> dict[str, Any]:
    """贴纸资源 → 原生 ``materials.stickers`` 条目。

    ⚠️ 调用方必须先通过 19 位 resource_id 校验(node_11 对 PLACEHOLDER_* fail-closed,
    不落占位素材);本构造器不做二次校验。
    """
    return {
        "id": material_id,
        "type": "sticker",
        "source_platform": 0,
        "duration": int(duration_us),
        "category_id": "",
        "category_name": "",
        "name": str(name),
        "resource_id": str(resource_id),
        "sticker_id": "",  # 待实机复核:真实贴纸样本的 id 字段口径
        "platform": "all",
        "version": "",
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


def build_native_text_effect_material(
    *,
    material_id: str,
    flower_meta: dict[str, Any],
    artist_effect_cache_root: str | Path,
) -> dict[str, Any]:
    """花字池条目 → 原生 ``materials.effects`` 条目(type=text_effect)。

    字段形状以 Golden Sample 为准(2026-11-18 console-52373c6f9e11 中文稿 UI
    应用「互动立体」实证,经两次 UI 保存存活;docs/第七期开发/花字审计证据_
    20261118/zh_draft_flower_extract.json),不从字幕动画结构推断。

    - ``path`` 由当前运行环境的缓存根动态构造(Golden Sample 同款正斜杠
      POSIX 形态),不得硬编码采集机器路径(审查 §4.4)。
    - material **id 由调用方传入**(约定:UUIDv5 确定性派生,见
      ``nodes._text_flower.our_flower_material_id``);附带我方私有标记键,
      清理逻辑据此区分"我方旧产物"与人工 UUID 花字。

    Raises:
        ValueError: 花字元数据缺 effect_id/resource_id/name,或缓存目录缺失
            (fail-closed,调用方应先用 ``flower_effect_cache_available`` + 状态
            守卫筛选;本构造器做最后一道字段防线)。
    """
    effect_id = str(flower_meta.get("effect_id") or "")
    resource_id = str(flower_meta.get("resource_id") or "")
    name = str(flower_meta.get("name") or "")
    if not effect_id or not resource_id or not name:
        raise ValueError(
            f"花字元数据不完整(effect_id/resource_id/name 需非空): {flower_meta!r}"
        )
    md5 = str(flower_meta.get("md5") or "")
    cache_dir = Path(artist_effect_cache_root) / effect_id / md5 if md5 else None
    if cache_dir is None or not cache_dir.is_dir():
        raise ValueError(f"花字资源包缓存目录缺失: {cache_dir}({name})")
    return {
        "id": material_id,
        "type": "text_effect",
        "adjust_params": [],
        "algorithm_artifact_path": "",
        "apply_target_type": 0,
        "bloom_params": None,
        "category_id": "panel-text-flower",
        "category_name": "花字",
        "color_match_info": {
            "source_feature_path": "",
            "target_feature_path": "",
            "target_image_path": "",
        },
        "effect_id": effect_id,
        "enable_skin_tone_correction": False,
        "exclusion_group": [],
        "face_adjust_params": [],
        "formula_id": "",
        "intensity_key": "",
        "multi_language_current": "",
        "name": name,
        "panel_id": "",
        "path": cache_dir.as_posix(),
        "platform": "all",
        "request_id": "",
        "resource_id": resource_id,
        "source_platform": 1,
        "sub_type": "none",
        "time_range": None,
        "value": 1.0,
        "version": "",
        OUR_TEXT_FLOWER_PRIVATE_KEY: OUR_TEXT_FLOWER_ROLE,
    }


__all__ = [
    "OUR_AUDIO_FADE_PREFIX",
    "OUR_STICKER_PREFIX",
    "OUR_TEXT_ANIMATION_PREFIX",
    "OUR_TEXT_FLOWER_PRIVATE_KEY",
    "OUR_TEXT_FLOWER_ROLE",
    "OUR_TRANSITION_PATTERN",
    "OUR_VIDEO_EFFECT_PATTERN",
    "build_native_audio_fade_material",
    "build_native_sticker_material",
    "build_native_text_animation_material",
    "build_native_text_effect_material",
    "build_native_transition_material",
    "build_native_video_effect_material",
    "flower_effect_cache_available",
    "is_19_digit_resource_id",
]
