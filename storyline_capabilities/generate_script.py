"""本地化 generate_script(plan_v4 §5 阶段 3 / B 类第二批)。

按 groups 数组逐组生成文案,产出 group_scripts / captions。
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


def generate_script(
    *,
    groups: list[dict[str, Any]],
    voiceover_style: str = "storyteller",
    lang: str = "zh",
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """为每个 group 生成一段文案 + 配音脚本。

    输出 ``{"group_scripts": [{group_id, narration, summary, ...}, ...], "lang": ...}``。
    """
    if not groups:
        return {"group_scripts": [], "lang": lang}

    sys_p = render_prompt("generate_script", "system", lang=lang)
    user_p = render_prompt(
        "generate_script",
        "user",
        lang=lang,
        voiceover_style=voiceover_style,
        groups="\n".join(
            f"- {g.get('group_id', '?')} theme={g.get('theme', '')} "
            f"clips={len(g.get('clips') or [])}"
            for g in groups
        ),
    )

    try:
        obj = chat_json(
            system_prompt=sys_p,
            user_prompt=user_p,
            schema={
                "type": "object",
                "properties": {
                    "scripts": {
                        "type": "array",
                    },
                },
            },
            client=client,
        )
    except Exception:  # noqa: BLE001
        obj = {}

    raw_scripts = list(obj.get("scripts") or [])

    out: list[dict[str, Any]] = []
    for g_idx, g in enumerate(groups):
        raw = raw_scripts[g_idx] if g_idx < len(raw_scripts) else {}
        narration = ""
        summary = ""
        if isinstance(raw, dict):
            narration = str(raw.get("narration", "") or "")
            summary = str(raw.get("summary", "") or "")
        elif isinstance(raw, str):
            narration = raw

        out.append(
            {
                "group_id": g.get("group_id"),
                "narration": narration,
                "summary": summary,
                "lang": lang,
                "voiceover_style": voiceover_style,
                "captions": g.get("captions", []),
            }
        )

    return {
        "group_scripts": out,
        "lang": lang,
        "voiceover_style": voiceover_style,
    }


__all__ = ["generate_script"]
