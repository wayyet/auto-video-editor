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


# 字体候选(本地资源库里只有这两个默认字体;``config.ASSETS_FONTS_DIR`` 有别的
# 字体时由节点壳子覆盖传入)。模板 ``elementrec_text`` 需要 ``{{candidates}}``。
DEFAULT_FONT_CANDIDATES: list[dict[str, str]] = [
    {"font_name": "SourceHanSansCN-Bold.otf", "lang": "zh"},
    {"font_name": "Roboto-Bold.ttf", "lang": "en"},
]


def _default_user_request(lang: str) -> str:
    return (
        "无其他要求" if str(lang).lower().startswith("zh") else "No additional requirements"
    )


def recommend_text(
    *,
    groups: list[dict[str, Any]],
    group_scripts: list[dict[str, Any]] | None = None,
    lang: str = "zh",
    candidates: list[dict[str, Any]] | None = None,
    user_request: str = "",
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """推荐每段的字幕样式。

    Args:
        groups: ``group_clips`` 的输出。
        group_scripts: ``generate_script`` 的输出(提供字幕文案)。
        lang: prompt 语言。
        candidates: 可选字体条目;None 时用 ``DEFAULT_FONT_CANDIDATES``。
        user_request: 用户对字体/字幕的额外要求。

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

    # 模板需要 scripts / candidates / user_request 三个变量(D6:此前只传了
    # scripts,font 候选与用户要求原样残留)。
    sys_p = render_prompt("elementrec_text", "system", lang=lang)
    user_p = render_prompt(
        "elementrec_text",
        "user",
        lang=lang,
        scripts="\n".join(
            (s.get("narration", "") or "")[:80] for s in (group_scripts or [])
        ),
        candidates=candidates if candidates is not None else DEFAULT_FONT_CANDIDATES,
        user_request=str(user_request or "").strip() or _default_user_request(lang),
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
