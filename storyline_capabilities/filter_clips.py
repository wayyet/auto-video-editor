"""本地化 filter_clips(plan_v4 §5 阶段 2 / B 类第一批)。

按 understanding 评分 + LLM 决策过滤低质量镜头。
"""
from __future__ import annotations

from typing import Any, Optional

from storyline_capabilities.vlm_client import (
    LLMClient,
    StubLLMClient,
    chat_json,
    parse_json_loose,
)
from storyline_capabilities.prompts import render_prompt
from storyline_capabilities.duration_budget import build_filter_budget_text


_DEFAULT_KEEP_RATIO: float = 0.7  # 默认保留 70%


def _clip_duration_sec(clip: dict[str, Any]) -> float:
    """从 caption 的 ``source_ref``(毫秒)算该片段时长(秒);拿不到返回 0。"""
    ref = clip.get("source_ref") or {}
    start = ref.get("start")
    end = ref.get("end")
    if start is None or end is None:
        return 0.0
    try:
        return max(0.0, (float(end) - float(start)) / 1000.0)
    except (TypeError, ValueError):
        return 0.0


def _default_user_request(lang: str) -> str:
    return "无其他要求" if str(lang).lower().startswith("zh") else "No additional requirements"


def filter_clips(
    *,
    understanding_artifact: dict[str, Any],
    keep_ratio: float = _DEFAULT_KEEP_RATIO,
    lang: str = "zh",
    user_request: str = "",
    target_duration_ms: int = 0,
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """按 ``aes_score`` 排序 + LLM 复核,保留前 ``keep_ratio`` 比例的 clip。

    Args:
        understanding_artifact: ``understand_clips`` 的输出。
        keep_ratio: 默认保留比例。
        lang: prompt 语言。
        user_request: 用户对选料的额外要求;空时用中性默认文案。
        target_duration_ms: 成片目标总时长(毫秒);0 = 无预算。

    返回 ``{"filtered_clips": [...], "dropped_clips": [...], "method": "..."}``。
    """
    captions = (
        understanding_artifact.get("clip_captions") or []
        if isinstance(understanding_artifact, dict)
        else []
    )
    if not captions:
        return {"filtered_clips": [], "dropped_clips": [], "method": "no_input"}

    # 按 aes_score 排序(无 aes_score 视为 0)
    def score_key(c: dict[str, Any]) -> float:
        s = c.get("aes_score")
        if s is None:
            return 0.0
        try:
            return float(s)
        except (TypeError, ValueError):
            return 0.0

    sorted_captions = sorted(captions, key=score_key, reverse=True)
    keep_n = max(1, int(round(len(sorted_captions) * keep_ratio)))
    top = sorted_captions[:keep_n]
    dropped = sorted_captions[keep_n:]

    # 选 LLM 复核:让 LLM 给出 "keep / drop" 决策。本地 stub 返回空 dict → 全保留。
    # 模板需要 user_request / duration_budget / clip_captions 三个变量(D6:
    # 此前只传了名字对不上的 ``captions``,占位符原样残留给模型)。
    clip_captions = "\n".join(
        f"- {c.get('clip_id', '?')}: {c.get('caption', '')}" for c in captions
    )
    total_input_duration_sec = sum(_clip_duration_sec(c) for c in captions)
    duration_budget = build_filter_budget_text(
        target_duration_ms, total_input_duration_sec, lang
    )
    sys_p = render_prompt("filter_clips", "system", lang=lang)
    user_p = render_prompt(
        "filter_clips",
        "user",
        lang=lang,
        user_request=str(user_request or "").strip() or _default_user_request(lang),
        clip_captions=clip_captions,
        duration_budget=duration_budget,
    )
    try:
        llm_decisions = chat_json(
            system_prompt=sys_p,
            user_prompt=user_p,
            schema={
                "type": "object",
                "properties": {
                    "keep_clip_ids": {"type": "array"},
                    "drop_clip_ids": {"type": "array"},
                },
            },
            client=client,
        )
    except Exception:  # noqa: BLE001
        llm_decisions = {}

    keep_ids = set(llm_decisions.get("keep_clip_ids") or [])
    drop_ids = set(llm_decisions.get("drop_clip_ids") or [])

    if keep_ids or drop_ids:
        # LLM 强制覆盖默认 top-N
        final_keep = [c for c in captions if c.get("clip_id") in keep_ids] or top
        final_drop = [c for c in captions if c.get("clip_id") in drop_ids] or dropped
        method = "llm_overlay"
    else:
        final_keep = top
        final_drop = dropped
        method = "score_topN"

    return {
        "filtered_clips": final_keep,
        "dropped_clips": final_drop,
        "method": method,
        "keep_count": len(final_keep),
        "drop_count": len(final_drop),
    }


__all__ = ["filter_clips"]
