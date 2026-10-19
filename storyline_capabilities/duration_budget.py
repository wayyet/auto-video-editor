"""成片目标总时长预算(本地化 vendored ``open_storyline.utils.duration_budget``)。

用户指定成片时长(如 35 秒)时,``storyline_targets.target_duration_ms`` 会传进来;
这里负责把它转成 prompt 里的预算文本,从**选料/分组**阶段就控制素材总量,
而不是等到 ``plan_timeline_pro`` 定剪时才发现超标。

为什么本地化一份而不直接 import vendored:
vendored 包在 ``openstoryline/src/open_storyline/`` 下,依赖 vendored venv 的
import 路径;本仓库的 storyline 能力层明确要求"无 vendored 依赖"。
纯 Python,语义与 vendored 逐字对齐(改文案时两处一起改)。

对应集成文档 D6:``{{duration_budget}}`` 占位符此前拿不到值,一直是空的。
"""
from __future__ import annotations

from typing import Any


def coerce_target_duration_ms(value: Any) -> int:
    """把工具入参转成正整数毫秒;非法/未提供一律返回 0(表示无预算)。"""
    try:
        ms = int(value) if value else 0
    except (TypeError, ValueError):
        return 0
    return ms if ms > 0 else 0


def build_filter_budget_text(
    target_duration_ms: int,
    total_input_duration_sec: float,
    lang: str,
) -> str:
    """filter_clips 用:保留片段总时长控制在预算的 1~2 倍,给后续定剪留裁剪余量。"""
    if not target_duration_ms:
        return ""
    budget_sec = target_duration_ms / 1000.0
    if str(lang).lower().startswith("zh"):
        return (
            f"成片目标总时长预算: {budget_sec:.1f} 秒;当前输入片段总时长约 {total_input_duration_sec:.1f} 秒。\n"
            f"请把保留片段的 duration 累加值控制在预算的 1~2 倍之间"
            f"(即 {budget_sec:.1f}~{budget_sec * 2:.1f} 秒),"
            f"优先保留美学分高、与用户要求最相关的片段。"
            f"该预算的优先级高于「保留 80%」的数量下限,"
            f"但仍需至少保留 5 个片段(总数不足 5 个则全部保留)。"
        )
    return (
        f"Target total duration budget for the final video: {budget_sec:.1f}s; "
        f"the total duration of input clips is about {total_input_duration_sec:.1f}s.\n"
        f"Keep the summed `duration` of retained clips within 1-2x the budget "
        f"({budget_sec:.1f}-{budget_sec * 2:.1f}s), prioritizing clips with high aesthetic "
        f"scores that best match the user request. This budget overrides the '80% retention' "
        f"floor, but still keep at least 5 clips (retain all if the total is 5 or fewer)."
    )


def build_group_budget_text(
    target_duration_ms: int,
    total_input_duration_sec: float,
    lang: str,
) -> str:
    """group_clips 用:所有分组时长累加不得超过预算的 110%,超出部分按叙事价值舍弃。"""
    if not target_duration_ms:
        return ""
    budget_sec = target_duration_ms / 1000.0
    if str(lang).lower().startswith("zh"):
        return (
            f"成片目标总时长预算: {budget_sec:.1f} 秒;当前输入片段总时长约 {total_input_duration_sec:.1f} 秒。\n"
            f"所有 group 的 duration 累加值不得超过预算的 110%(即 {budget_sec * 1.1:.1f} 秒)。"
            f"当素材总时长超出预算时,「全量使用」不再适用:请按叙事价值舍弃多余片段"
            f"(优先舍弃与主题弱相关、画面重复的),被舍弃的 clip_id 不要出现在任何 group 中。"
        )
    return (
        f"Target total duration budget for the final video: {budget_sec:.1f}s; "
        f"the total duration of input clips is about {total_input_duration_sec:.1f}s.\n"
        f"The summed `duration` of all groups must not exceed 110% of the budget "
        f"({budget_sec * 1.1:.1f}s). When the material total exceeds the budget, the "
        f"'use every clip' rule no longer applies: drop the least valuable clips "
        f"(weakly related to the theme or visually redundant first), and dropped "
        f"clip_ids must not appear in any group."
    )


__all__ = [
    "coerce_target_duration_ms",
    "build_filter_budget_text",
    "build_group_budget_text",
]
