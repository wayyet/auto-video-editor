"""本地化 generate_script(plan_v4 §5 阶段 3 / B 类第二批)。

按 groups 数组逐组生成文案,产出 group_scripts / captions。

2026-11-10(交付 QA 阻断分析):chat 异常 / 解析失败此前都被静默折成空
narration(配音/字幕源头断裂且零痕迹)。现改走 :func:`chat_json_with_trace`
留痕,坏返回(异常或拿不到 ``scripts``)自动重试一次,并在输出携带
``llm_trace``;空 narration 是否算失败由**节点层**按 required_features 裁决
(本能力保持纯函数,不做策略)。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from storyline_capabilities.vlm_client import (
    LLMClient,
    StubLLMClient,
    chat_json_with_trace,
    parse_json_loose,
)
from storyline_capabilities.prompts import render_prompt


def _extract_scripts(obj: dict[str, Any]) -> list[Any]:
    """从 LLM 返回的 dict 提取脚本数组,双键兼容。

    prompts 模板(prompt/tasks/generate_script)要求 ``group_scripts``;
    代码历史键名是 ``scripts``。两键语义等价,取先命中者。
    """
    for key in ("group_scripts", "scripts"):
        value = obj.get(key)
        if isinstance(value, list):
            return value
    return []


def generate_script(
    *,
    groups: list[dict[str, Any]],
    voiceover_style: str = "storyteller",
    lang: str = "zh",
    user_request: str = "",
    overall: str = "",
    web_research: str = "",
    script_template: str = "",
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """为每个 group 生成一段文案 + 配音脚本。

    Args:
        groups: ``group_clips`` 的输出。
        voiceover_style: 配音风格。
        lang: prompt 语言。
        user_request: 用户对文案的额外要求;空时用中性默认文案。
        overall: 素材整体概述(来自 ``understand_clips`` 的 ``overall``,
            纯视频画面语义;2026-11-10 前置化阶段4 起不再混入网页资料)。
        web_research: 外部网页检索摘要与来源 URL(来自 ``search_web_topic``
            产物,经节点层清洗;前置化设计方案 §4.4)。检索缺失/失败时为空串,
            文案照常生成。外部资料是不可信参考,模板已写明使用纪律。
        script_template: 文案模板正文(来自 ``script_template_recommendation``
            产物,经节点层清洗;2026-11-12 noop 治理阶段1)。空串 = 未注入模板,
            文案照常生成。模板是风格/结构参考、不是事实来源,使用纪律写在
            prompt(system ``[script_template]`` 段 + user 模板块)。

    输出 ``{"group_scripts": [{group_id, narration, summary, ...}, ...],
    "lang": ..., "llm_trace": {...}}``;``llm_trace`` 记录 LLM 调用与解析
    留痕(节点层落盘 + 依此裁决空 narration 是否 fail-fast),
    并带 ``script_template_injected`` 证明模板正文是否真实进入 LLM 请求。
    """
    if not groups:
        return {"group_scripts": [], "lang": lang, "llm_trace": {"note": "no groups"}}

    injected = bool(str(script_template or "").strip())
    template_body = str(script_template or "").strip()
    if not template_body:
        # 空模板也必须渲染出明确的占位语,避免 user 模板的模板块悬空
        template_body = (
            "（本次运行没有注入模板，忽略本段）"
            if lang == "zh"
            else "(no template injected this run; ignore this section)"
        )
    # 模板需要 user_request / overall / groups / web_research / script_template
    # 变量(D6:此前 user_request 与 overall 没传,占位符原样残留;阶段4 新增
    # web_research;阶段1 新增 script_template)。
    sys_p = render_prompt("generate_script", "system", lang=lang)
    user_p = render_prompt(
        "generate_script",
        "user",
        lang=lang,
        user_request=str(user_request or "").strip() or "No requirements",
        overall=str(overall or ""),
        web_research=str(web_research or "").strip() or "（无外部检索资料）",
        script_template=template_body,
        groups="\n".join(
            f"- {g.get('group_id', '?')} theme={g.get('theme', '')} "
            f"clips={len(g.get('clips') or [])}"
            for g in groups
        ),
    )

    schema = {
        "type": "object",
        "properties": {
            "group_scripts": {
                "type": "array",
            },
        },
    }

    # 坏返回(调用异常 / 解析不出 / 拿不到脚本数组)重试一次;每次尝试都留痕。
    obj: dict[str, Any] = {}
    trace: dict[str, Any] = {}
    for _attempt in range(2):
        obj, trace = chat_json_with_trace(
            system_prompt=sys_p,
            user_prompt=user_p,
            schema=schema,
            client=client,
        )
        trace["obj_keys"] = sorted(obj.keys())
        # 请求侧留痕(2026-11-10 前置化阶段4):raw_head 只留响应,请求 prompt
        # 也留头部证据 —— script_llm_trace.json 可核对 web_research 是否真的
        # 进入 LLM 请求(前置化设计方案 §5.7 DoD)。
        trace["user_prompt_head"] = user_p[:4000]
        # 2026-11-12 阶段1:模板注入证据(验收标准:trace 能证明模板正文进入 LLM 请求)
        trace["script_template_injected"] = injected
        if trace.get("ok") and _extract_scripts(obj):
            break

    raw_scripts = _extract_scripts(obj)

    out: list[dict[str, Any]] = []
    for g_idx, g in enumerate(groups):
        raw = raw_scripts[g_idx] if g_idx < len(raw_scripts) else {}
        narration = ""
        summary = ""
        if isinstance(raw, dict):
            # prompts/tasks/generate_script 模板要求模型输出 ``raw_text``;
            # ``narration`` 是历史键名,双键兼容防未来模板/模型漂移。
            narration = str(raw.get("narration") or raw.get("raw_text") or "")
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
        "llm_trace": trace,
    }


__all__ = ["generate_script"]
