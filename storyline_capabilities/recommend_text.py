"""本地化 recommend_text(plan_v4 §5 阶段 3 / B 类第二批)。

推荐字幕样式(font / size / position)。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from storyline_capabilities.vlm_client import (
    LLMClient,
    StubLLMClient,
    chat_json,
    parse_json_loose,
)
from storyline_capabilities.prompts import render_prompt


def recommend_text(
    *,
    groups: list[dict[str, Any]],
    group_scripts: list[dict[str, Any]] | None = None,
    lang: str = "zh",
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """推荐每段的字幕样式。

    Output::

        {
            "items": [
                {"group_id": "...", "font_zh": "...", "font_en": "...",
                 "size": 24, "color": "#FFFFFF", "position": "bottom"},
                ...
            ],
            "default": {...},
        }

    阶段 3 默认实现:不调 LLM 也能给一个最小可用 stub
    (``{"font_zh": "SourceHanSansCN-Bold.otf", "font_en": "Roboto-Bold.ttf",
    "size": 24, "position": "bottom"}``)。
    """
    default = {
        "font_zh": "SourceHanSansCN-Bold.otf",
        "font_en": "Roboto-Bold.ttf",
        "size": 24,
        "color": "#FFFFFF",
        "position": "bottom",
    }
    if not groups:
        return {"items": [], "default": default, "method": "no_input"}

    sys_p = render_prompt("elementrec_text", "system", lang=lang)
    user_p = render_prompt(
        "elementrec_text",
        "user",
        lang=lang,
        scripts="\n".join(
            (s.get("narration", "") or "")[:80] for s in (group_scripts or [])
        ),
    )

    try:
        obj = chat_json(
            system_prompt=sys_p,
            user_prompt=user_p,
            schema={
                "type": "object",
                "properties": {
                    "items": {"type": "array"},
                    "default": {
                        "type": "object",
                        "properties": {
                            "font_zh": {"type": "string"},
                            "font_en": {"type": "string"},
                            "size": {"type": "integer"},
                            "position": {"type": "string"},
                        },
                    },
                },
            },
            client=client,
        )
    except Exception:  # noqa: BLE001
        obj = {}

    raw_items = list(obj.get("items") or [])
    default_out = obj.get("default") if isinstance(obj.get("default"), dict) else None

    items: list[dict[str, Any]] = []
    for idx, g in enumerate(groups):
        raw = raw_items[idx] if idx < len(raw_items) else {}
        if isinstance(raw, dict):
            item = {
                "group_id": g.get("group_id"),
                **{k: raw.get(k, default[k]) for k in default},
            }
        else:
            item = {"group_id": g.get("group_id"), **default}
        items.append(item)

    return {
        "items": items,
        "default": default_out or default,
        "method": "llm" if obj else "fallback",
    }


__all__ = ["recommend_text"]
