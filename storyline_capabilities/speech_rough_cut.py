"""本地化 speech_rough_cut(plan_v4 §5 阶段 5 / C 类,条件旁支)。

vendored ``SpeechRoughCutNode`` 接收 ASR 单句 + 上下文 + 历史 + 用户诉求,
让 LLM 决定:删口水词、拆分中间删除、调整起止时间。LLM 输出 JSON::

    {"reason": str, "res": [{"text", "start", "end"}, ...]}

本地化版本去 ``NodeState`` 依赖、纯函数化,Prompt 文件从
``openstoryline/prompts/tasks/speech_rough_cut/zh/{system,user}.md`` 拷到
``storyline_capabilities/prompts/speech_rough_cut/zh/``(阶段 4 已拷)。

设计纪律(plan §5 阶段 5):
1. **不依赖 torch**:LLM 调用走 ``vlm_client.chat_json``,与 B 类同。
2. **永不抛**(plan §4.4):LLM 失败 / 解析失败时返回 ``{"segments": [], ...}``,
   不阻断 plan_timeline_pro;它看到 ``rough_cut_segments`` 空时跳过
   speech-driven 路径(对照 vendored:失败时退化为原始 ASR)。
3. **批量 vs 单条**:节点壳子每条 ASR 句调一次,capability 每次只处理一条,
   简化 reasoning 链。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from storyline_capabilities.prompts import render_prompt
from storyline_capabilities.vlm_client import LLMClient, StubLLMClient, chat_json

logger = logging.getLogger(__name__)


def speech_rough_cut(
    *,
    asr_sentence: dict[str, Any],
    ctx_text: str = "",
    history: Optional[list[dict[str, Any]]] = None,
    preceding: str = "",
    following: str = "",
    user_request: str = "",
    lang: str = "zh",
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """对单条 ASR 句子做粗剪(plan §5 阶段 5)。

    Args:
        asr_sentence: ``{"text", "start", "end", "timestamp": [[s, e], ...]}``。
        ctx_text: 全文 ASR 文本(用于判断冗余)。
        history: 历史 ``speech_rough_cut`` 结果(每条同 res 字段)。
        preceding / following: 前后句文本(让 LLM 看上下文)。
        user_request: 用户诉求(可空)。
        lang: ``"zh"``(vendored 无 en 版本,默认 zh)。
        client: LLM client 注入位;None 时走默认。

    Returns:
        ``{"segments": [{"text", "start", "end"}, ...], "reason": str,
           "method": str}``。
        失败时 ``segments=[]``、``method="no_input"/"llm_fail"``,不抛。
    """
    if not asr_sentence or not (asr_sentence.get("text") or "").strip():
        return {"segments": [], "reason": "empty input", "method": "no_input"}

    sys_p = render_prompt("speech_rough_cut", "system", lang=lang)
    user_p = render_prompt(
        "speech_rough_cut",
        "user",
        lang=lang,
        curr_asr_sentence_info=json.dumps(asr_sentence, ensure_ascii=False),
        pre_ctx=preceding,
        nxt_ctx=following,
        asr_text=ctx_text,
        history_rough_cut_jsons=json.dumps(history or [], ensure_ascii=False),
        user_request=user_request,
    )

    try:
        obj = chat_json(
            system_prompt=sys_p,
            user_prompt=user_p,
            schema={
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "res": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "start": {"type": "integer"},
                                "end": {"type": "integer"},
                            },
                        },
                    },
                },
            },
            client=client,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"speech_rough_cut LLM exception: {e!r}")
        return {"segments": [], "reason": f"llm_exception: {e!r}", "method": "llm_fail"}

    if not isinstance(obj, dict):
        return {"segments": [], "reason": "llm_returned_non_dict", "method": "llm_fail"}

    raw_res = obj.get("res") or []
    segments: list[dict[str, Any]] = []
    for item in raw_res:
        if not isinstance(item, dict):
            continue
        try:
            text = str(item.get("text") or "").strip()
            start = int(item.get("start") or 0)
            end = int(item.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if not text or end <= start:
            continue
        segments.append({"text": text, "start": start, "end": end})

    return {
        "segments": segments,
        "reason": str(obj.get("reason") or ""),
        "method": "llm",
    }


__all__ = ["speech_rough_cut"]