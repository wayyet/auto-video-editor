"""VLM/LLM 客户端抽象(plan_v4 §5 阶段 2 第 2~3 行)。

设计纪律(plan §5 阶段 2 决策 2):
- 用 ``Protocol`` 抽象调用 AI Gateway,主项目内所有 capability 函数只依赖此协议
  不绑死具体厂商。
- 默认实现:``StubLLMClient`` —— 阶段 2 不接真实 LLM 时返回固定假文本,
  让 graph 跑通 + 单元测试可写。
- 阶段 5 起接入 ``SemanticKernelLLMClient`` 或 SK + Polly 熔断,本文档
  ``register_default_clients()`` 提供 hook 位。

签名:
    chat(system_prompt, user_prompt, *, media=None, json_schema=None,
         temperature=0.3, top_p=0.9, max_tokens=4096) -> str

``media``:list[dict] 形式,与 vendored ``llm.complete(media=...)`` 兼容::
    - ``{"path": "..."}``  → 图片
    - ``{"path": ..., "in_sec": 0.0, "out_sec": 5.0}`` → 视频段
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------
@runtime_checkable
class LLMClient(Protocol):
    """统一 LLM/VLM 调用入口。"""

    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        media: Optional[list[dict[str, Any]]] = None,
        json_schema: Optional[dict[str, Any]] = None,
        temperature: float = 0.3,
        top_p: float = 0.9,
        max_tokens: int = 4096,
    ) -> str:
        """返回 LLM 输出(纯文本或 JSON 字符串,看 capability 怎么解析)。"""
        ...


# ---------------------------------------------------------------------------
# Stub 默认实现(plan §5 阶段 2 第 3 行决策)
# ---------------------------------------------------------------------------
_DEFAULT_TEXT_FALLBACK = "no caption"
_DEFAULT_JSON_FALLBACK = "{}"


class StubLLMClient:
    """永远返回固定 fake 文本,用于 plan §5 阶段 2 「不接真实 LLM 时跑通」。

    行为优先级:
    1. **若显式给了 ``default_text``**(非 None),总是返回它(json_schema 也无效化)。
    2. 否则若有 ``default_json``(非 None),总是返回它。
    3. 否则按调用形态:
       - ``json_schema`` 非空 → 返回按 schema 生成的空 stub(dict)
       - 否则 → 返回 ``_DEFAULT_TEXT_FALLBACK`` (``"no caption"``)
    """

    def __init__(
        self,
        *,
        default_text: str | None = None,
        default_json: dict | None = None,
        # 历史兼容:test 用 default_caption="..." 时当文本用
        default_caption: str | None = _DEFAULT_TEXT_FALLBACK,
    ) -> None:
        self.default_text = default_text
        self.default_json = default_json
        self.default_caption = default_caption  # 兼容旧代码
        self.call_count = 0

    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        media: Optional[list[dict[str, Any]]] = None,
        json_schema: Optional[dict[str, Any]] = None,
        temperature: float = 0.3,
        top_p: float = 0.9,
        max_tokens: int = 4096,
    ) -> str:
        self.call_count += 1
        # 优先级最高的:explicit default_text
        if self.default_text is not None:
            return self.default_text
        # 其次:explicit default_json
        if self.default_json is not None:
            return json.dumps(self.default_json, ensure_ascii=False)
        # 兼容历史字段 default_caption:仅在 default_caption 非默认空值时用它
        if self.default_caption and self.default_caption != _DEFAULT_TEXT_FALLBACK:
            return self.default_caption
        # 否则按 schema / text 分流
        if json_schema is not None:
            return json.dumps(
                _fake_obj_from_schema(json_schema), ensure_ascii=False
            )
        return _DEFAULT_TEXT_FALLBACK


def _fake_obj_from_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """按 json_schema 形状生成最简 stub dict(单元测试用)。"""
    out: dict[str, Any] = {}
    if not isinstance(schema, dict):
        return out
    props = schema.get("properties") or {}
    if isinstance(props, dict):
        for k, v in props.items():
            t = (
                v.get("type", "string")
                if isinstance(v, dict)
                else "string"
            )
            out[k] = _fake_value_for_type(t)
    return out


def _fake_value_for_type(t: str) -> Any:
    if t == "string":
        return ""
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    if t == "array":
        return []
    if t == "object":
        return {}
    return ""


# ---------------------------------------------------------------------------
# 全局默认 client(协议注入,阶段 5 替换)
# ---------------------------------------------------------------------------
_ACTIVE_CLIENT: Optional[LLMClient] = None


def get_default_client() -> LLMClient:
    """主流程不显式传 client 时,返回当前默认 client。"""
    global _ACTIVE_CLIENT
    if _ACTIVE_CLIENT is None:
        _ACTIVE_CLIENT = StubLLMClient()
    return _ACTIVE_CLIENT


def set_default_client(client: Optional[LLMClient]) -> None:
    """阶段 5 接 SK 时由 ``register_default_clients()`` 调用。"""
    global _ACTIVE_CLIENT
    _ACTIVE_CLIENT = client


def register_default_clients() -> None:
    """阶段 5 hook — 接 Semantic Kernel + Polly 后由 main 入口调用,这里留空。"""
    # Phase 5: 接 SK
    # from storyline_capabilities.vlm_client_semantickernel import (
    #     SemanticKernelLLMClient,
    # )
    # set_default_client(SemanticKernelLLMClient(...))
    return None


# ---------------------------------------------------------------------------
# 调用便捷函数 — 让 capability 实现更短
# ---------------------------------------------------------------------------
def chat_json(
    *,
    system_prompt: str,
    user_prompt: str,
    media: Optional[list[dict[str, Any]]] = None,
    schema: Optional[dict[str, Any]] = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """调 ``client.chat``,把 JSON 字符串解析成 dict(失败返回 ``{}``)。

    与 vendored ``open_storyline.utils.parse_json.parse_json_dict`` 对齐的容错:
    容错提取首段 ``{...}`` JSON,容忍 ``<think>...</think>`` 包裹。
    """
    raw = (client or get_default_client()).chat(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        media=media,
        json_schema=schema,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return parse_json_loose(raw)


def chat_text(
    *,
    system_prompt: str,
    user_prompt: str,
    media: Optional[list[dict[str, Any]]] = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    client: Optional[LLMClient] = None,
) -> str:
    """纯文本调用(脚本生成等不需要 JSON)。"""
    return (client or get_default_client()).chat(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        media=media,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def parse_json_loose(raw: str) -> dict[str, Any]:
    """对 vendored ``parse_json_dict`` 的轻量复刻(去括号剥离 + 容忍 <think>)。

    - 把 ``<think>...</think>``(FireRed 模型常见)截断
    - 找首个 ``{`` 与最后一个 ``}``,截取中间作为 JSON 解析
    """
    if not raw:
        return {}
    s = raw
    # 容忍常见推理前缀
    if "<think>" in s:
        end = s.find("</think>")
        if end != -1:
            s = s[end + len("</think>"):]
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    snippet = s[start : end + 1]
    try:
        data = json.loads(snippet)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return data
    return {}


__all__ = [
    "LLMClient",
    "StubLLMClient",
    "get_default_client",
    "set_default_client",
    "register_default_clients",
    "chat_json",
    "chat_text",
    "parse_json_loose",
]
