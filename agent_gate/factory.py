"""按后端名字造 LLM 客户端。给仓库的 ``register_default_clients()`` 用。

后端:
    stub        仓库原有的假客户端(默认,行为不变)
    gate        方案 A:LLM 任务闸门,Agent 通过 MCP / CLI 领任务、交答案
    claude-cli  方案 B:每次调用拉起 `claude -p`
    mcode-cli   方案 B:每次调用拉起 `mcode exec`
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from agent_gate.mailbox import Mailbox


def build_client(backend: str, mailbox: Optional[Mailbox] = None) -> Any:
    b = (backend or "stub").strip().lower()
    if b == "stub":
        from storyline_capabilities.vlm_client import StubLLMClient

        return StubLLMClient()
    if b == "gate":
        from agent_gate.gate_client import GateLLMClient

        if mailbox is None:
            raise ValueError("gate 后端需要 mailbox")
        return GateLLMClient(mailbox)
    if b == "claude-cli":
        from agent_gate.cli_client import ClaudeCLIClient

        return ClaudeCLIClient()
    if b == "mcode-cli":
        from agent_gate.cli_client import McodeCLIClient

        return McodeCLIClient()
    raise ValueError(f"未知后端:{backend!r}(可选:stub / gate / claude-cli / mcode-cli)")


def register_from_env() -> Any:
    """仓库 ``register_default_clients()`` 的实现体。

    环境变量:
        VIDEO_LLM_BACKEND   后端名,默认 stub
        AGENT_GATE_OUTPUTS  outputs 根目录(gate 后端需要)
        AGENT_GATE_THREAD   thread_id(gate 后端需要)
    """
    from storyline_capabilities.vlm_client import set_default_client

    backend = os.environ.get("VIDEO_LLM_BACKEND", "stub")
    mb = None
    if backend.lower() == "gate":
        from agent_gate.runner import gate_root

        mb = Mailbox(gate_root(os.environ.get("AGENT_GATE_OUTPUTS", "outputs"),
                               os.environ["AGENT_GATE_THREAD"]))
    client = build_client(backend, mb)
    set_default_client(client)
    return client
