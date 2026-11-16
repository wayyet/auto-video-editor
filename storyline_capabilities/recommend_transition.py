"""本地化 recommend_transition(plan_v4 §5 阶段 3 / B 类第二批)。

按 group 推荐每段之间的转场(默认 / 段落)。

2026-11-17 核验报告 P0 重写(转场池方案 §6.3):
- 修复存量 bug:此前错误渲染 ``generate_ai_transition`` 提示词(MiniMax 图生
  视频 prompt 模板,要求输出英文散文),宽松解析拿不到 JSON → 每组永远降级
  ``fade_in/fade_out`` → 草稿每个剪辑边界都是"叠化"。
- 现使用专用 ``recommend_transition`` 提示词任务 + 受控词表
  ``cut/fade/zoom/slide/whip/creative``(与 templates/fx_template.json
  ``transition_semantic_map`` 的池 key 对齐)。
- 按 ``group_id`` 建映射对齐(不按数组下标):空/重复/未知 ID 拒绝并记录;
  缺失 group 用校验过的 default 补齐;非法枚举回退 default → 再回退 fade。
- ``method`` ∈ no_input/llm/stub/fallback:只有真实 client 成功返回且解析出
  JSON 才标 ``llm``;stub 一律标 ``stub``(修复 truthy dict 伪装 llm 的坑)。
- 诊断信息(调用异常/解析失败/ID 异常/枚举回退)写入返回值 ``warnings``,
  节点包装层负责落 error_log。
"""
from __future__ import annotations

from typing import Any, Optional

from storyline_capabilities.vlm_client import (
    LLMClient,
    StubLLMClient,
    chat_json_with_trace,
    get_default_client,
)
from storyline_capabilities.prompts import render_prompt
from storyline_capabilities._common import default_user_request

# 受控词表 —— 与 templates/fx_template.json transition_semantic_map 的池 key 对齐
TRANSITION_VOCAB: frozenset[str] = frozenset(
    {"cut", "fade", "zoom", "slide", "whip", "creative"}
)
_DEFAULT_FALLBACK_SEMANTIC = "fade"

# chat_json_with_trace 的 schema(stub 据此返回假结构;真实模型按提示词输出)
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array"},
        "default": {"type": "string"},
    },
}


def _validate_semantic(
    raw: Any, default: str, warnings: list[str], label: str
) -> str:
    """词表校验:合法原样返回;非法回退 ``default`` 并记录。"""
    value = str(raw or "").strip()
    if value in TRANSITION_VOCAB:
        return value
    if value:
        warnings.append(f"{label}={value!r} 不在受控词表,回退 {default!r}")
    return default


def recommend_transition(
    *,
    groups: list[dict[str, Any]],
    bgm_selection: dict[str, Any] | None = None,
    lang: str = "zh",
    user_request: str = "",
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """每 group 推荐转场语义(受控词表),输出规范化 items。

    Output::
        {
            "items": [{"group_id": "...", "transition_in": "fade", "transition_out": "cut"}],
            "default": "fade",
            "method": "llm" | "stub" | "fallback" | "no_input",
            "warnings": [...],
        }

    失败降级:LLM 调用异常/解析失败/输出全不可用时,全部组按校验过的
    default(fade)补齐 —— 图不会因推荐失败而中断,原因全部可诊断。
    """
    if not groups:
        return {
            "items": [],
            "default": _DEFAULT_FALLBACK_SEMANTIC,
            "method": "no_input",
            "warnings": [],
        }

    warnings: list[str] = []

    # ---- 输入 groups 规范化:ID 非空唯一;重复槽位复用首条 ----
    prompt_rows: list[tuple[str, str]] = []
    slot_ids: list[str] = []  # 与输入 groups 等长;空/重复 ID 的槽位记 ""
    seen: set[str] = set()
    for g in groups:
        gid = str((g or {}).get("group_id") or "").strip()
        theme = str((g or {}).get("theme") or "")
        if not gid:
            warnings.append("输入 groups 存在空 group_id,该组按 default 补齐")
            slot_ids.append("")
            continue
        if gid in seen:
            warnings.append(f"输入 groups 重复 group_id={gid!r},该槽位复用首条推荐")
            slot_ids.append(gid)
            continue
        seen.add(gid)
        slot_ids.append(gid)
        prompt_rows.append((gid, theme))

    # ---- 提示词渲染(专用任务;groups/bgm 真正进入 user prompt)----
    sys_p = render_prompt("recommend_transition", "system", lang=lang)
    user_p = render_prompt(
        "recommend_transition",
        "user",
        lang=lang,
        user_request=str(user_request or "").strip() or default_user_request(lang),
        groups="\n".join(f"- group_id={gid}: {theme}" for gid, theme in prompt_rows),
        bgm=str((bgm_selection or {}).get("bgm_ref") or "no_bgm"),
    )

    # ---- LLM 调用(带 trace:异常/解析失败可诊断,不外抛)----
    obj, trace = chat_json_with_trace(
        system_prompt=sys_p,
        user_prompt=user_p,
        schema=_OUTPUT_SCHEMA,
        client=client,
    )
    if not trace.get("ok"):
        warnings.append(f"LLM 调用异常: {trace.get('error', '')!r}")
    elif not trace.get("parsed"):
        warnings.append("LLM 返回未解析出非空 JSON,按降级处理")

    # ---- default 校验 ----
    raw_default = str(obj.get("default") or "").strip()
    default = _validate_semantic(raw_default, _DEFAULT_FALLBACK_SEMANTIC, warnings, "default")
    if not raw_default:
        default = _DEFAULT_FALLBACK_SEMANTIC

    # ---- items 按 group_id 对齐(不按下标)----
    by_id: dict[str, dict[str, Any]] = {}
    raw_items = obj.get("items")
    if isinstance(raw_items, list):
        for raw in raw_items:
            if not isinstance(raw, dict):
                warnings.append("LLM 输出存在非对象 items 条目,已忽略")
                continue
            gid = str(raw.get("group_id") or "").strip()
            if not gid:
                warnings.append("LLM 输出存在空 group_id 条目,已忽略")
                continue
            if gid in by_id:
                warnings.append(f"LLM 输出重复 group_id={gid!r},保留首条")
                continue
            by_id[gid] = raw
    unknown = sorted(set(by_id) - seen)
    for gid in unknown:
        warnings.append(f"LLM 输出未知 group_id={gid!r},已忽略")

    items: list[dict[str, Any]] = []
    for g, gid in zip(groups, slot_ids):
        raw = by_id.get(gid)
        if raw is None:
            warnings.append(f"group_id={gid or '<空>'!r} 缺少推荐,按 default={default!r} 补齐")
            transition_in = default
            transition_out = default
        else:
            transition_in = _validate_semantic(
                raw.get("transition_in"), default, warnings, f"group {gid!r}.transition_in"
            )
            transition_out = _validate_semantic(
                raw.get("transition_out"), default, warnings, f"group {gid!r}.transition_out"
            )
        items.append(
            {
                "group_id": (g or {}).get("group_id"),
                "transition_in": transition_in,
                "transition_out": transition_out,
            }
        )

    # ---- method 判别:stub 一律如实标注,不再伪装 llm ----
    effective_client = client or get_default_client()
    if isinstance(effective_client, StubLLMClient):
        method = "stub"
    elif trace.get("ok") and trace.get("parsed"):
        method = "llm"
    else:
        method = "fallback"

    return {
        "items": items,
        "default": default,
        "method": method,
        "warnings": warnings,
    }


__all__ = ["recommend_transition", "TRANSITION_VOCAB"]
