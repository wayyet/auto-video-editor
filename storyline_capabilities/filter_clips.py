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


def _source_of(clip: dict[str, Any]) -> str:
    """取 clip 所属的**源视频**标识(media_id);取不到返回空串。

    多视频时按源保底要用它分组(见 :func:`filter_clips`)。
    """
    for key in ("media_id", "source_media_id"):
        value = clip.get(key)
        if value:
            return str(value)
    ref = clip.get("source_ref")
    if isinstance(ref, dict) and ref.get("media_id"):
        return str(ref["media_id"])
    return ""


def _apply_per_source_floor(
    kept: list[dict[str, Any]],
    captions: list[dict[str, Any]],
    score_key,
) -> tuple[list[dict[str, Any]], int]:
    """每源保底:确保每个源视频至少有 1 个 clip 进入成片。

    多视频下,纯按 ``aes_score`` 池化排序会让一个高质量视频把其它源全部挤掉。
    这里给"没出现在 kept 里的源"补上该源得分最高的 clip。

    单源时该逻辑等价于原行为(唯一源的最高分 clip 本来就在 top 里)。

    Returns:
        ``(最终保留列表, 补进来的 clip 数)``。补进数为 0 时列表原样返回。
    """
    kept_ids = {str(c.get("clip_id") or "") for c in kept}
    kept_sources = {_source_of(c) for c in kept}
    all_sources = {_source_of(c) for c in captions}
    missing = sorted(s for s in all_sources - kept_sources if s)
    if not missing:
        return kept, 0

    extras: list[dict[str, Any]] = []
    for src in missing:
        candidates = [c for c in captions if _source_of(c) == src]
        if not candidates:
            continue
        best = max(candidates, key=score_key)
        if str(best.get("clip_id") or "") not in kept_ids:
            kept_ids.add(str(best.get("clip_id") or ""))
            extras.append(best)
    if not extras:
        return kept, 0
    # 保底 clip 按得分插回整体顺序(分数高的靠前)
    return sorted(kept + extras, key=score_key, reverse=True), len(extras)


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

    # 多源保底:每个源视频至少保留 1 个 clip(否则高分源会把其它源全挤掉)
    final_keep, added = _apply_per_source_floor(final_keep, captions, score_key)
    if added:
        # 补进了保底 clip → dropped 重算为"没被保留的全部"
        kept_ids = {str(c.get("clip_id") or "") for c in final_keep}
        final_drop = [
            c for c in captions if str(c.get("clip_id") or "") not in kept_ids
        ]

    return {
        "filtered_clips": final_keep,
        "dropped_clips": final_drop,
        "method": method,
        "keep_count": len(final_keep),
        "drop_count": len(final_drop),
    }


__all__ = ["filter_clips"]
