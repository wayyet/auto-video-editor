"""AI 转场提示词生成(2026-11-12 noop 治理阶段2,设计文档 §2.2 任务4)。

FireRed 原版用**视觉 LLM** 看首尾帧生成转场描述;本项目现有 LLM 后端
(mcode-cli 等)是纯文本,不支持传图 —— 按既定决策改用**文本 LLM**:把相邻
两段的字幕/摘要 + 用户要求交给文本 LLM,按 FireRed 的英文转场 prompt 纪律
生成运动描述。LLM 不可用/失败时退**确定性模板拼接**,链路不断。

FireRed 纪律(zh system prompt 转述,b45011d):输出英文首尾帧 prompt;
固定前缀 "Smooth continuous single shot...";锁定运镜;强制过渡介质掩护
(光斑/运动模糊/遮挡转场);反幻觉后缀(不加人/不加字/不加水印)。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from storyline_capabilities.vlm_client import get_default_client

logger = logging.getLogger(__name__)

#: 提示词生成方式落进 artifact,便于审计
PROMPT_METHOD_LLM = "text_llm"
PROMPT_METHOD_FALLBACK = "deterministic_fallback"

_SYSTEM_PROMPT = (
    "你是视频转场提示词生成器。相邻两段画面之间要生成一段 AI 过渡视频,"
    "你会拿到前一段的结尾描述和后一段的开头描述。只输出一段英文提示词(纯文本,"
    "不要解释、不要 JSON),要求:\n"
    "1. 以 \"Smooth continuous single shot\" 开头;\n"
    "2. 锁定一种平稳的运镜(如 slow push-in / gentle pan),两侧画面靠它衔接;\n"
    "3. 必须安排一个自然的过渡介质掩护衔接(光斑 / 运动模糊 / 物体擦过 / 云雾);\n"
    "4. 结尾附加反幻觉约束: no text, no captions, no watermark, no new people or objects added.\n"
    "5. 全文不超过 80 个英文单词;只描述运动与过渡,不要出现具体人名/地名/品牌。"
)


def _clip_brief(clip: dict[str, Any]) -> str:
    """取一段画面的可读描述:captions / summary / transcript 任一非空。"""
    for key in ("captions", "summary", "transcript", "caption"):
        value = clip.get(key)
        if isinstance(value, list):
            value = " ".join(str(v) for v in value if str(v).strip())
        text = str(value or "").strip()
        if text:
            return text[:200]
    return ""


def deterministic_fallback_prompt(from_brief: str, to_brief: str) -> str:
    """确定性兜底提示词(零 LLM 依赖,内容保守通用)。"""
    fr = (from_brief or "the previous scene").replace("\n", " ")[:120]
    to = (to_brief or "the next scene").replace("\n", " ")[:120]
    return (
        "Smooth continuous single shot. The camera holds a steady slow push-in as "
        f"the scene moves from \"{fr}\" toward \"{to}\". "
        "A soft light flare and gentle motion blur cover the transition between the two shots. "
        "No text, no captions, no watermark, no new people or objects added."
    )


def compose_transition_prompt(
    *,
    from_clip: dict[str, Any],
    to_clip: dict[str, Any],
    user_request: str = "",
    client: Optional[Any] = None,
) -> tuple[str, str]:
    """生成转场运动提示词,返回 ``(prompt, method)``。

    method ∈ ``text_llm``(文本 LLM 生成)/ ``deterministic_fallback``(兜底拼接)。
    LLM 未配置(Stub)/异常/空返回 一律走兜底 —— 转场提示词是增强能力,不阻断。
    """
    from_brief = _clip_brief(from_clip)
    to_brief = _clip_brief(to_clip)
    fallback = deterministic_fallback_prompt(from_brief, to_brief)

    from storyline_capabilities.vlm_client import StubLLMClient

    effective = client if client is not None else get_default_client()
    if isinstance(effective, StubLLMClient):
        return fallback, PROMPT_METHOD_FALLBACK
    user_prompt = (
        f"前一段结尾画面描述: {from_brief or '(无描述)'}\n"
        f"后一段开头画面描述: {to_brief or '(无描述)'}\n"
        f"用户对视频的整体要求: {str(user_request or '').strip() or '(无)'}\n"
        "请按系统纪律生成过渡提示词。"
    )
    try:
        raw = effective.chat(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            media=None,
            json_schema=None,
            temperature=0.3,
            max_tokens=256,
        )
    except Exception as e:  # noqa: BLE001 — 兜底不抛
        logger.warning("转场提示词 LLM 生成失败,走确定性兜底: %r", e)
        return fallback, PROMPT_METHOD_FALLBACK
    prompt = str(raw or "").strip()
    if not prompt:
        return fallback, PROMPT_METHOD_FALLBACK
    # 基本卫生:去代码围栏与引号包裹;超长截断
    prompt = prompt.strip("`\"' \n").replace("```", "").strip()
    if len(prompt) > 800:
        prompt = prompt[:800]
    return prompt or fallback, PROMPT_METHOD_LLM


__all__ = [
    "PROMPT_METHOD_LLM",
    "PROMPT_METHOD_FALLBACK",
    "compose_transition_prompt",
    "deterministic_fallback_prompt",
]
