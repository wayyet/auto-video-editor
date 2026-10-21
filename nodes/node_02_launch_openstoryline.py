"""节点 2:launch_openstoryline_service — 只探测,不再启动任何进程。

行为(2026-10 启动入口统一后的最终形态):
- 不再有 MCP/stdio 链路(``OpenStorylineMCPClient`` 已删除)。
- **不再启动任何进程**:OpenStoryline Web 的生命周期由
  ``start_production.ps1``(Phase 0.6 / 0.7)统一管理 —— 那里负责
  复用已有服务、启动唯一的一个 uvicorn、做 HTTP 健康检查。
  本节点在 **human 模式**下只**探测** Web 是否就绪,并如实记录。
- **human-mode(默认)**:``GET <web_url>/`` 返回 200 → ``openstoryline_ready=True``,
  ``openstoryline_pid=None``(PID 未知 —— 进程不是本节点起的),
  ``status_log`` 追加 ``node_02_openstoryline_reused``;
  探测失败 → ``openstoryline_ready=False``,``status_log`` 追加
  ``node_02_launch_openstoryline_failed``,``error_log`` 追加可操作的错误说明。
- **auto-mode**:行为**完全不变** —— 不需要 Web UI,直接返回
  ``openstoryline_ready=True`` + append ``node_02_launch_openstoryline_auto_skipped``。
- 保留 ``_wait_for_ready(url, timeout_s)`` 模块顶层函数,作为**探测注入点**,
  便于测试用 ``monkeypatch.setattr(m2, "_wait_for_ready", ...)`` 桩掉。
- 本节点**不抛异常**,与原设计一致 —— 由图路由与 Python 入口处理。

设计要点
--------
* 方案 A(生产脚本管 Web 生命周期):避免"生产脚本起一次 + node_02 再起一次"
  的双重启动,后者会让 PID 记录指向那个失败的第二进程。
* ``openstoryline_pid`` 恒为 ``None``:本节点不拥有任何进程,不该记 PID。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from config import (
    OPENSTORYLINE_HEALTH_TIMEOUT_S,
    OPENSTORYLINE_MCP_PORT,
    STORYLINE_MODE,
    openstoryline_web_url,
)
from state import WorkflowState


# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
# auto-video-editor/ → openstoryline/ → openstoryline/agent_fastapi.py
OPENSTORYLINE_DIR: Path = (
    Path(__file__).resolve().parent.parent / "openstoryline"
)
OPENSTORYLINE_AGENT: Path = OPENSTORYLINE_DIR / "agent_fastapi.py"
# Windows: openstoryline/.venv/Scripts/python.exe
# (迁移后是本仓独立 venv,不再指向 FireRed 外部)
OPENSTORYLINE_VENV_PY: Path = (
    OPENSTORYLINE_DIR / ".venv" / "Scripts" / "python.exe"
)


# ---------------------------------------------------------------------------
# 健康检查(可注入的探测点)
# ---------------------------------------------------------------------------
def _wait_for_ready(url: str, timeout_s: int) -> bool:
    """轮询 ``GET <url>``,200 即 ready。

    暴露为模块顶层符号,测试按
    ``monkeypatch.setattr(m2, "_wait_for_ready", lambda url, t: True)`` 模式接入。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


# ---------------------------------------------------------------------------
# 节点主体
# ---------------------------------------------------------------------------
def launch_openstoryline_service(
    state: WorkflowState,
    *,
    health_checker: Any = None,
) -> dict:
    """探测 OpenStoryline Web 是否就绪(human 模式),或直接放行(auto 模式)。

    Args:
        state: LangGraph 工作流状态。
        health_checker: 注入位(测试用),签名 ``(url: str, timeout_s: int) -> bool``。
            为 None 时走 ``_wait_for_ready`` 默认实现。

    Returns:
        更新后的 state 子集,字段:
        - ``openstoryline_pid``: 恒为 ``None``(本节点不拥有进程)
        - ``openstoryline_web_url``: str, ``http://127.0.0.1:7860``
        - ``openstoryline_mcp_endpoint``: str, 向后兼容字段(8001 已不再使用)
        - ``openstoryline_ready``: bool, 探测通过与否(auto-mode 永远 True)
        - ``status_log`` / ``error_log``: 增量追加
    """
    # auto-mode:不需要 vendored Web UI(plan §5 阶段 7),直接放行
    if STORYLINE_MODE == "auto":
        web_url = openstoryline_web_url()
        mcp_endpoint = f"http://127.0.0.1:{OPENSTORYLINE_MCP_PORT}/mcp"
        return {
            **state,
            "openstoryline_pid": None,
            "openstoryline_web_url": web_url,
            "openstoryline_mcp_endpoint": mcp_endpoint,
            "openstoryline_ready": True,
            "storyline_session_id": state.get("session_id"),
            "storyline_artifacts": [],
            "status_log": list(state.get("status_log") or [])
            + ["node_02_launch_openstoryline_auto_skipped"],
            "error_log": list(state.get("error_log") or []),
        }

    errors = list(state.get("error_log", []) or [])
    checker = health_checker or _wait_for_ready

    web_url = openstoryline_web_url()  # http://127.0.0.1:7860
    mcp_endpoint = f"http://127.0.0.1:{OPENSTORYLINE_MCP_PORT}/mcp"  # 向后兼容

    # 只探测,不启动:Web 生命周期归 start_production.ps1 管
    ready = bool(checker(f"{web_url}/", OPENSTORYLINE_HEALTH_TIMEOUT_S))
    if not ready:
        errors.append(
            f"[node_02] OpenStoryline Web 未就绪 "
            f"({OPENSTORYLINE_HEALTH_TIMEOUT_S}s),url={web_url}/;"
            "Web 生命周期由 start_production.ps1 管理,请检查 :7860 "
            "是否被非本项目进程占用,或查看 logs\\start_*.log"
        )

    return {
        **state,
        # 本节点不拥有任何进程 → PID 未知
        "openstoryline_pid": None,
        "openstoryline_web_url": web_url,
        "openstoryline_mcp_endpoint": mcp_endpoint,  # 向后兼容
        "openstoryline_ready": ready,
        "storyline_session_id": state.get("session_id"),  # 记录供下游使用
        "storyline_artifacts": [],  # 占位(后续不再用)
        "status_log": (state.get("status_log") or [])
        + [
            "node_02_openstoryline_reused"
            if ready
            else "node_02_launch_openstoryline_failed"
        ],
        "error_log": errors,
    }


__all__ = ["launch_openstoryline_service", "_wait_for_ready"]
