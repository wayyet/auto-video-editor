"""本地化 group_clips(plan_v4 §5 阶段 2 / B 类第一批)。

把 filter 出来的 clip 按叙事节奏 / 主题分组,产出 groups 数组;每个 group
含 voiceover subtitle 候选(由 group_scripts 在 generate_script 阶段补齐)。

设计纪律:
- Plan §2.2:group_clips 输出会被 qa_gate retry 回退;qa_gate 仅依赖
  storyline_timeline_plan,group_clips 失败不会触发 retry — 让它**降级**到
  "每个 clip 一个 group",而不是抛。
- B 类一律不抛 — 把错误写到 error_log,通过 state diff 返回。
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


_DEFAULT_GROUP_SIZE: int = 3


def group_clips(
    *,
    filtered_clips: list[dict[str, Any]],
    group_size: int = _DEFAULT_GROUP_SIZE,
    lang: str = "zh",
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """按 ``group_size`` 把 clips 切片成 group;每个 group 包含 LLM 推荐主题。

    输入:``filter_clips`` 输出的 ``filtered_clips`` 列表。
    输出 ``{"groups": [{group_id, media_refs, theme, ...}], "method": ...}``。

    降级:LLM 不可用时按固定 3-clip/组切片。
    """
    if not filtered_clips:
        return {"groups": [], "method": "empty_input"}

    # 1. 默认切片
    raw_groups: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for clip in filtered_clips:
        cur.append(clip)
        if len(cur) >= group_size:
            raw_groups.append(cur)
            cur = []
    if cur:
        raw_groups.append(cur)

    # 2. 让 LLM 给出每个 group 的主题词(可选)
    sys_p = render_prompt("group_clips", "system", lang=lang)
    user_p = render_prompt(
        "group_clips",
        "user",
        lang=lang,
        groups="\n".join(
            f"- group_{g_idx + 1}: "
            + ", ".join(
                (c.get("caption", "") or "")[:50] for c in g
            )
            for g_idx, g in enumerate(raw_groups)
        ),
    )

    try:
        llm_obj = chat_json(
            system_prompt=sys_p,
            user_prompt=user_p,
            schema={
                "type": "object",
                "properties": {
                    "themes": {"type": "array"},
                    "voiceover_hints": {"type": "array"},
                },
            },
            client=client,
        )
    except Exception:  # noqa: BLE001
        llm_obj = {}

    themes = list(llm_obj.get("themes") or [])
    voiceover_hints = list(llm_obj.get("voiceover_hints") or [])

    groups: list[dict[str, Any]] = []
    for g_idx, g in enumerate(raw_groups):
        theme = themes[g_idx] if g_idx < len(themes) else ""
        hint = voiceover_hints[g_idx] if g_idx < len(voiceover_hints) else ""
        # 计算 group 的时间跨度(从第一个 clip 的 source_in_ms / source_out_ms)
        first_in = g[0].get("source_in_ms", 0)
        last_out = g[-1].get("source_out_ms", 0)
        groups.append(
            {
                "group_id": f"group_{g_idx + 1:04d}",
                "media_refs": [
                    {"clip_id": c.get("clip_id"), "media_id": c.get("media_id")}
                    for c in g
                ],
                "clips": [
                    {k: v for k, v in c.items() if k != "caption"}
                    for c in g
                ],
                "captions": [c.get("caption") for c in g],
                "theme": str(theme),
                "voiceover_hint": str(hint),
                "start_ms": first_in,
                "end_ms": last_out,
            }
        )

    return {
        "groups": groups,
        "method": "fixed_grouping" if not themes else "llm_themed",
        "group_size": group_size,
    }


__all__ = ["group_clips"]
