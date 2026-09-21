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


_DEFAULT_KEEP_RATIO: float = 0.7  # 默认保留 70%


def filter_clips(
    *,
    understanding_artifact: dict[str, Any],
    keep_ratio: float = _DEFAULT_KEEP_RATIO,
    lang: str = "zh",
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """按 ``aes_score`` 排序 + LLM 复核,保留前 ``keep_ratio`` 比例的 clip。

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
    sys_p = render_prompt("filter_clips", "system", lang=lang)
    user_p = render_prompt(
        "filter_clips",
        "user",
        lang=lang,
        captions="\n".join(
            f"- {c.get('clip_id', '?')}: {c.get('caption', '')}" for c in captions
        ),
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
