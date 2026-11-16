"""LLM 后端公共装配 — 一个 client 实例同时服务 storyline VLM 与英文翻译。

2026-11-17 P1(英文字幕真实翻译,修订计划 §5.3):原 run / resume / Agent Gate
三个入口各自只装配 ``storyline_capabilities.vlm_client`` 的默认 client,翻译侧
默认客户端长期停留在 Week4 Mock(输出 ``[EN] 中文``)。本模块把"注册 VLM client
+ 注册翻译 client"收敛到一处:

- 真实后端(claude-cli / mcode-cli / gate)→ 同一个 client 实例既注入 VLM
  默认,又包装成 :class:`~jy_common.translate_client.LLMTranslateClient` 注入
  翻译默认 —— 翻译与脚本生成共用同一模型/配置(修订计划 §5.2 第 1 条);
- stub 后端 → 翻译侧保持 :class:`~jy_common.translate_client.MockTranslateClient`
  (translator_id="mock"),与 P2-1"stub 显式标注不冒充真实成功"口径一致;
  node_08b/node_17 的 ``subtitle_translation_status`` 门禁会拦下 stub 译文,
  交付 QA 判 degraded 阻断。
"""

from __future__ import annotations

from typing import Any

#: 显式按真实翻译装配的后端选择集(与 run/resume CLI --llm-backend 白名单一致)
_REAL_BACKENDS: tuple[str, ...] = ("claude-cli", "mcode-cli", "gate")


def build_translator(backend: str | None, client: Any) -> Any:
    """按后端类型构造翻译客户端:stub → Mock;真实 → LLMTranslateClient。"""
    from jy_common.translate_client import LLMTranslateClient, MockTranslateClient
    from storyline_capabilities.vlm_client import StubLLMClient

    if backend == "stub" or isinstance(client, StubLLMClient):
        return MockTranslateClient()
    return LLMTranslateClient(
        client,
        translator_id=backend or type(client).__name__,
    )


def install_llm_backend(backend: str | None = None, *, client: Any = None) -> Any:
    """装配默认 LLM client(VLM + 翻译两处),返回生效的 client 实例。

    两种用法:
    - 入口侧按后端名装配(创建台 run/resume):``install_llm_backend("mcode-cli")``;
    - Agent Gate worker 已持有 client 实例:``install_llm_backend(client=client)``
      (``backend=None`` 时 translator_id 取 client 类型名)。
    ``backend`` 与 ``client`` 都为空 → 什么都不装(保持既有懒加载 Stub 行为)。
    """
    if client is None:
        if not backend:
            return None
        from agent_gate.factory import build_client

        client = build_client(backend)

    from storyline_capabilities.vlm_client import set_default_client as _set_vlm

    _set_vlm(client)

    from jy_common.translate_client import set_default_client as _set_translate

    effective_backend = (
        str(backend).strip().lower() if backend else None
    )
    _set_translate(build_translator(effective_backend, client))
    return client
