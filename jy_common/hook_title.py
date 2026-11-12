"""开场钩子标题选取 + 免费资源元数据(node_09b 用;node_14/15 后续可复用标题函数)。

标题来源优先级(原计划 §7.10 / 第四期计划 §3.2 流程第 3 步):

1. ``state["hook_title"]``(可选键,控制台 / 命令行显式传入;strip 后非空即用,
   **不再切句不截断** —— 显式标题是操作员的明确意图,长度自担);
2. 第 1 条字幕文本切到第一个标点之前,最多 12 字;
3. 都没有 → ``None``,调用方记 ``…_noop:no_title``,开场特效仍保留。

开场特效与标题入场动画的资源行**内置**在本模块:这些是免费(is_vip=False)
条目,权威定义在 vendor 上游 metadata(本仓 ``templates/fx_resource_library.json``
/ ``text_resource_library.json`` 的 dump 只含 VIP 条目,不含它们)。
"""

from __future__ import annotations

from typing import Any

#: 开场大字标题的最大长度(第 2 优先级切句路径的截断上限;显式传入不截断)。
HOOK_TITLE_MAX_LEN = 12

#: 切句标点:句中遇到即截断(半角/全角逗号、顿号、句号、分号、叹号、问号)。
_HOOK_PUNCTUATION = tuple("，,、。；;！!？?")

#: 开场特效候选(免费档):默认「CCD闪光」,备选「闪白」。
#: 来源:vendor/pyJianYingDraft/metadata/video_scene_effect.py:30 / :3337,
#: EffectMeta(name, is_vip=False, resource_id, effect_id, md5)。
HOOK_EFFECT_CANDIDATES: tuple[dict[str, Any], ...] = (
    {
        "name": "CCD闪光",
        "is_vip": False,
        "resource_id": "7130585796020539941",
        "effect_id": "4007303",
        "md5": "cf66e86f715060df7a2e9268382697d4",
    },
    {
        "name": "闪白",
        "is_vip": False,
        "resource_id": "6706773500792672781",
        "effect_id": "634761",
        "md5": "f0804cb2cb4e88a036ecf87dcf031cf0",
    },
)

#: 标题入场动画候选(免费档):默认「冲屏位移」,备选「放大」「渐显」。
#: 来源:vendor/pyJianYingDraft/metadata/text_intro.py:9 / :49 / :58,
#: AnimationMeta(name, is_vip=False, duration_s, resource_id, effect_id, md5)。
HOOK_ANIMATION_CANDIDATES: tuple[dict[str, Any], ...] = (
    {
        "name": "冲屏位移",
        "is_vip": False,
        "duration_s": 0.5,
        "resource_id": "7078181271393800711",
        "effect_id": "1643884",
        "md5": "fd73ffc26a3f02fa6d957a94b590623a",
    },
    {
        "name": "放大",
        "is_vip": False,
        "duration_s": 0.5,
        "resource_id": "6724919499042066958",
        "effect_id": "1644264",
        "md5": "cf0f072aa31d3884ba90362af063f55a",
    },
    {
        "name": "渐显",
        "is_vip": False,
        "duration_s": 0.5,
        "resource_id": "6724916044072227332",
        "effect_id": "1644304",
        "md5": "40859aa05ff9f3e3a3f0de7bfead1c42",
    },
)


def pick_hook_title(state: dict[str, Any], first_text: str | None) -> str | None:
    """返回开场大字标题;三个来源都拿不到时返回 ``None``。

    Args:
        state: 工作流 state(读可选键 ``hook_title``,调用方一律 ``state.get``)。
        first_text: 第 1 条字幕的显示文本(可为 ``None`` / 空白)。
    """
    explicit = state.get("hook_title")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    if not isinstance(first_text, str):
        return None
    text = first_text.strip()
    if not text:
        return None

    cut = len(text)
    for i, ch in enumerate(text):
        if ch in _HOOK_PUNCTUATION:
            cut = i
            break
    title = text[:cut].strip()
    if not title:
        # 文本以标点开头(如「,开头」)—— 切完为空,视为无可用标题
        return None
    return title[:HOOK_TITLE_MAX_LEN]


__all__ = [
    "HOOK_ANIMATION_CANDIDATES",
    "HOOK_EFFECT_CANDIDATES",
    "HOOK_TITLE_MAX_LEN",
    "pick_hook_title",
]
