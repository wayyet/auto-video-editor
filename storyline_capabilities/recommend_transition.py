"""本地化 recommend_transition(plan_v4 §5 阶段 3 / B 类第二批)。

按 group 推荐每段之间的转场(默认 / 段落)。
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


def _default_user_request(lang: str) -> str:
    return (
        "无其他要求" if str(lang).lower().startswith("zh") else "No additional requirements"
    )


def recommend_transition(
    *,
    groups: list[dict[str, Any]],
    bgm_selection: dict[str, Any] | None = None,
    lang: str = "zh",
    user_request: str = "",
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """每段间推荐转场类型。

    Output::
        {
            "items": [{"group_id": "...", "transition_in": "fade", "transition_out": "cut"}],
            "default": "fade_in",
        }

    阶段 3 默认实现:不调模型也能给一个最朴素的 ``{default: 'fade_in', items: []}``
    让图跑通 + 节点壳子写 state;LLM 调用失败 / 无 client 时降级。
    """
    if not groups:
        return {"items": [], "default": "fade_in", "method": "no_input"}

    # 模板需要 user_request(D6:此前没传,占位符原样残留);
    # groups / bgm 仍一并传入,便于将来模板加字段时不用再改这里。
    sys_p = render_prompt("generate_ai_transition", "system", lang=lang)
    user_p = render_prompt(
        "generate_ai_transition",
        "user",
        lang=lang,
        user_request=str(user_request or "").strip() or _default_user_request(lang),
        groups="\n".join(
            f"- {g.get('group_id', '?')}: {g.get('theme', '')}"
            for g in groups
        ),
        bgm=str((bgm_selection or {}).get("bgm_ref") or "no_bgm"),
    )

    try:
        obj = chat_json(
            system_prompt=sys_p,
            user_prompt=user_p,
            schema={
                "type": "object",
                "properties": {
                    "items": {"type": "array"},
                    "default": {"type": "string"},
                },
            },
            client=client,
        )
    except Exception:  # noqa: BLE001
        obj = {}

    raw_items = list(obj.get("items") or [])
    default = str(obj.get("default") or "fade_in")

    items: list[dict[str, Any]] = []
    for idx, g in enumerate(groups):
        raw = raw_items[idx] if idx < len(raw_items) else {}
        if isinstance(raw, dict):
            transition_in = str(raw.get("transition_in") or "fade_in")
            transition_out = str(raw.get("transition_out") or "fade_out")
        else:
            transition_in = "fade_in"
            transition_out = "fade_out"
        items.append(
            {
                "group_id": g.get("group_id"),
                "transition_in": transition_in,
                "transition_out": transition_out,
            }
        )

    return {
        "items": items,
        "default": default,
        "method": "llm" if obj else "fallback",
    }


__all__ = ["recommend_transition"]
