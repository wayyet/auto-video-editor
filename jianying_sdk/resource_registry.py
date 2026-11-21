"""SDK 枚举与项目资源库的运行时交叉验证(迁移设计文档 §4.6;阶段0/1)。

阶段0 的 ``scripts/build_sdk_compatibility.py`` 产出
``templates/sdk_resource_compatibility.json``(**ID 级**映射交叉表);本模块
是它的运行时消费方:编译器按 effect_id 解析 SDK 枚举成员,解析不到或交叉表
状态不支持时返回 None / 抛错,**绝不按名称猜映射**(§13.2)。

- ``resolve_transition(effect_id)`` → ``TransitionType`` 成员。
- ``resolve_video_effect(effect_id)`` → (场景/人物, 成员)。
- ``resolve_video_animation(effect_id)`` → (in/out/group, 成员)。
- ``resolve_text_animation(effect_id)`` → (in/out/loop, 成员)。
- ``compat_status(category, effect_id)`` → 阶段0 交叉表状态(supported /
  unsupported_missing_in_sdk / ...)。

所有解析只认 effect_id;resource_id 一致性由阶段0 交叉表预先核对
(``supported`` 状态即"双 ID 一致"),运行时不再重复校验。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jianying_sdk._sdk_loader import sdk

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPAT_TABLE_PATH = REPO_ROOT / "templates" / "sdk_resource_compatibility.json"

#: 交叉表 category → SDK 枚举名(动画类枚举决定 kind 语义)
_CATEGORY_ENUMS: dict[str, tuple[str, ...]] = {
    "transition": ("TransitionType",),
    "video_effect": ("VideoSceneEffectType", "VideoCharacterEffectType"),
    "text_intro": ("TextIntro",),
    "text_loop": ("TextLoopAnim",),
    "text_outro": ("TextOutro",),
}


@dataclass(frozen=True)
class ResolvedResource:
    """一次成功解析的 SDK 资源(枚举成员 + 类别 + 动画 kind)。"""

    category: str
    enum_name: str
    effect_id: str
    resource_id: str
    member: Any
    #: in/out/group/loop(仅动画类;非动画为 None)
    anim_kind: str | None = None


def load_compat_table() -> dict:
    """读阶段0 交叉表(文件缺失/损坏 fail-closed raise)。"""
    if not COMPAT_TABLE_PATH.is_file():
        raise RuntimeError(
            f"SDK 资源交叉表缺失: {COMPAT_TABLE_PATH};"
            "请先执行 scripts/build_sdk_compatibility.py(阶段0 交付物)"
        )
    return json.loads(COMPAT_TABLE_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _effect_id_index() -> dict[str, dict[str, list[Any]]]:
    """category → effect_id → [SDK 枚举成员,...](枚举成员 value 携带元数据)。"""
    jy = sdk()
    from pyJianYingDraft import (
        GroupAnimationType,
        IntroType,
        OutroType,
        TextIntro,
        TextLoopAnim,
        TextOutro,
        TransitionType,
        VideoCharacterEffectType,
        VideoSceneEffectType,
    )

    enums: dict[str, tuple] = {
        "TransitionType": TransitionType,
        "VideoSceneEffectType": VideoSceneEffectType,
        "VideoCharacterEffectType": VideoCharacterEffectType,
        "IntroType": IntroType,
        "OutroType": OutroType,
        "GroupAnimationType": GroupAnimationType,
        "TextIntro": TextIntro,
        "TextLoopAnim": TextLoopAnim,
        "TextOutro": TextOutro,
    }
    index: dict[str, dict[str, list[Any]]] = {}
    for category, enum_names in _CATEGORY_ENUMS.items():
        bucket: dict[str, list[Any]] = {}
        for enum_name in enum_names:
            for member in enums[enum_name]:
                bucket.setdefault(str(member.value.effect_id), []).append(member)
        index[category] = bucket
    return index


def compat_status(category: str, effect_id: str) -> str | None:
    """查阶段0 交叉表状态;effect_id 不在表内返回 None。"""
    table = load_compat_table()
    block = table.get("categories", {}).get(category)
    if not block:
        return None
    for item in block.get("items", []):
        if str(item.get("effect_id")) == str(effect_id):
            return str(item.get("status"))
    return None


def _resolve(category: str, effect_id: str, *, require_supported: bool) -> ResolvedResource | None:
    members = _effect_id_index().get(category, {}).get(str(effect_id))
    if not members:
        return None
    member = members[0]
    if require_supported:
        status = compat_status(category, effect_id)
        if status != "supported":
            return None
    anim_kind = None
    enum_name = type(member).__name__
    if enum_name == "IntroType" or category == "text_intro":
        anim_kind = "in"
    elif enum_name == "OutroType" or category == "text_outro":
        anim_kind = "out"
    elif enum_name == "GroupAnimationType":
        anim_kind = "group"
    elif category == "text_loop":
        anim_kind = "loop"
    return ResolvedResource(
        category=category,
        enum_name=enum_name,
        effect_id=str(member.value.effect_id),
        resource_id=str(member.value.resource_id),
        member=member,
        anim_kind=anim_kind,
    )


def resolve_transition(effect_id: str, *, require_supported: bool = True) -> ResolvedResource | None:
    """按 effect_id 解析转场;交叉表非 supported 时返回 None(快门/水墨两案)。"""
    return _resolve("transition", effect_id, require_supported=require_supported)


def resolve_video_effect(effect_id: str, *, require_supported: bool = True) -> ResolvedResource | None:
    """按 effect_id 解析视频特效(场景/人物合并索引)。"""
    return _resolve("video_effect", effect_id, require_supported=require_supported)


def resolve_video_animation(
    effect_id: str, *, require_supported: bool = False
) -> ResolvedResource | None:
    """按 effect_id 在视频动画三枚举(intro/outro/group)中解析。

    视频动画当前没有项目侧资源库(阶段5 引入),默认只要求 SDK 枚举存在
    (``require_supported=False``);启用项目资源库后由调用方传 True。
    """
    for enum_name, kind in (("IntroType", "in"), ("OutroType", "out"), ("GroupAnimationType", "group")):
        for member in _enum_members(enum_name):
            if str(member.value.effect_id) == str(effect_id):
                return ResolvedResource(
                    category="video_animation",
                    enum_name=enum_name,
                    effect_id=str(member.value.effect_id),
                    resource_id=str(member.value.resource_id),
                    member=member,
                    anim_kind=kind,
                )
    return None


def resolve_text_animation(
    effect_id: str, *, require_supported: bool = True
) -> ResolvedResource | None:
    """按 effect_id 在文本动画三枚举(TextIntro/Outro/LoopAnim)中解析。

    kind 优先取交叉表 category(intro/loop/outro),保证与项目资源库口径一致。
    """
    table = load_compat_table()
    for category in ("text_intro", "text_loop", "text_outro"):
        block = table.get("categories", {}).get(category, {})
        hit = any(
            str(item.get("effect_id")) == str(effect_id) for item in block.get("items", [])
        )
        if not hit:
            continue
        if require_supported:
            status = compat_status(category, effect_id)
            if status != "supported":
                return None
        members = _effect_id_index()[category].get(str(effect_id), [])
        if not members:
            return None
        member = members[0]
        kind = {"text_intro": "in", "text_loop": "loop", "text_outro": "out"}[category]
        return ResolvedResource(
            category=category,
            enum_name=type(member).__name__,
            effect_id=str(member.value.effect_id),
            resource_id=str(member.value.resource_id),
            member=member,
            anim_kind=kind,
        )
    return None


@lru_cache(maxsize=None)
def _enum_members(enum_name: str) -> tuple:
    """按名字取 SDK 枚举的成员元组(供跨枚举查找)。"""
    import importlib

    module = importlib.import_module("pyJianYingDraft.metadata")
    enum_type = getattr(module, enum_name)
    return tuple(enum_type)


def reset_cache() -> None:
    """清空模块级缓存(测试隔离用)。"""
    _effect_id_index.cache_clear()
    _enum_members.cache_clear()


__all__ = [
    "COMPAT_TABLE_PATH",
    "ResolvedResource",
    "compat_status",
    "load_compat_table",
    "resolve_text_animation",
    "resolve_transition",
    "resolve_video_animation",
    "resolve_video_effect",
    "reset_cache",
]
