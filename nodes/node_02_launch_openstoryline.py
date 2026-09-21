"""节点 2:launch_openstoryline_service — 2026-09 迁移解耦版 + 阶段 7 auto-mode 旁路。

行为(对照 plan §4.1 / §5 阶段 7):
- 不再有 MCP/stdio 链路(``OpenStorylineMCPClient`` 已删除)。
- **human-mode(默认)**:本地 ``subprocess.Popen`` 启动
  ``openstoryline/agent_fastapi.py``(uvicorn,监听 127.0.0.1:7860);
  ``GET /`` 返回 200 即 ready。保留 ``popen_factory`` / ``health_checker``
  注入位,便于 ``tests/unit/test_node_02_launch_openstoryline.py`` 三条历史
  用例继续生效。
- **auto-mode**(plan §5 阶段 7):不再启动 vendored Web UI — auto 走
  19 节点确定性图,不依赖 OpenStoryline 网页交互。直接返回
  ``openstoryline_ready=True`` + append ``node_02_auto_skipped`` status_log。
- 保留 ``_wait_for_ready(url, timeout_s)`` 模块顶层函数,便于
  ``tests/integration/test_interrupt_resume.py`` 的 monkeypatch。
- 本节点**不抛异常**,与原设计一致 — 由下游节点 3/4 决定是否早退。

阶段 7 解耦要点(plan §7.3 验收):
- auto-mode 跑 19 节点完全不需要 vendored Web 服务,可以停掉
  ``openstoryline/.venv`` 整个子进程。本节点保留 vendored 是为了
  ``asr_runner.py``(ASR funasr 推理);**Web UI 启动在 auto-mode 下
  禁用**,避免无谓开销 + 阶段 7 验收"完全解耦"语义。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from config import (
    OPENSTORYLINE_HEALTH_TIMEOUT_S,
    OPENSTORYLINE_MCP_PORT,
    OPENSTORYLINE_WEB_PORT,
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
# 进程工厂 + 健康检查(可注入)
# ---------------------------------------------------------------------------
def _default_popen(argv: list[str], **kwargs: Any):
    """默认进程工厂:subprocess.Popen。"""
    return subprocess.Popen(argv, **kwargs)


def _wait_for_ready(url: str, timeout_s: int) -> bool:
    """轮询 ``GET <url>``,200 即 ready。

    暴露为模块顶层符号,``tests/integration/test_interrupt_resume.py`` 仍按
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
    popen_factory: Any = None,
    health_checker: Any = None,
) -> dict:
    """启动 OpenStoryline Web 服务并等待 ``GET /`` 健康检查通过。

    阶段 7(plan §5):``STORYLINE_MODE=auto`` 时直接 noop 返回,跳过 uvicorn
    子进程;``openstoryline_ready=True`` 供下游 ``open_preview`` /
    ``_route_mode`` 继续。``human-mode``(默认)行为**完全不动**,
    仍走 vendored Web UI + 关卡⓪ 人工交互路径。

    Args:
        state: LangGraph 工作流状态。
        popen_factory: 注入位(测试用),签名 ``(argv, **kwargs) -> Popen-like``。
            仅 human-mode 使用。
        health_checker: 注入位(测试用),签名 ``(url: str, timeout_s: int) -> bool``。
            为 None 时走 ``_wait_for_ready`` 默认实现。仅 human-mode 使用。

    Returns:
        更新后的 state 子集,字段:
        - ``openstoryline_pid``: int | None,子进程 PID(失败时 None)
        - ``openstoryline_web_url``: str, ``http://127.0.0.1:7860``
        - ``openstoryline_mcp_endpoint``: str, 向后兼容字段(8001 已不再使用)
        - ``openstoryline_ready``: bool, 健康检查通过与否(auto-mode 永远 True)
        - ``status_log`` / ``error_log``: 增量追加
    """
    # 阶段 7:auto-mode 不启动 vendored Web UI(plan §5 阶段 7 第 3 条)
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
    factory = popen_factory or _default_popen
    checker = health_checker or _wait_for_ready

    web_url = openstoryline_web_url()  # http://127.0.0.1:7860
    mcp_endpoint = f"http://127.0.0.1:{OPENSTORYLINE_MCP_PORT}/mcp"  # 向后兼容

    # 1. 启动子进程
    argv = [
        str(OPENSTORYLINE_VENV_PY),
        "-m",
        "uvicorn",
        "agent_fastapi:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(OPENSTORYLINE_WEB_PORT),
    ]
    pid: int | None = None
    try:
        proc = factory(argv, cwd=str(OPENSTORYLINE_DIR))
        pid = getattr(proc, "pid", None)
    except FileNotFoundError as e:
        errors.append(f"[node_02] 启动 OpenStoryline 失败: {e}")
        return {
            **state,
            "openstoryline_pid": None,
            "openstoryline_web_url": web_url,
            "openstoryline_mcp_endpoint": mcp_endpoint,
            "openstoryline_ready": False,
            "status_log": (state.get("status_log") or [])
            + ["node_02_launch_openstoryline_failed"],
            "error_log": errors,
        }

    # 2. 等待健康检查
    ready = bool(checker(f"{web_url}/", OPENSTORYLINE_HEALTH_TIMEOUT_S))
    if not ready:
        errors.append(
            f"[node_02] OpenStoryline 健康检查超时 "
            f"({OPENSTORYLINE_HEALTH_TIMEOUT_S}s),url={web_url}/"
        )

    return {
        **state,
        "openstoryline_pid": pid,
        "openstoryline_web_url": web_url,
        "openstoryline_mcp_endpoint": mcp_endpoint,  # 向后兼容
        "openstoryline_ready": ready,
        "storyline_session_id": state.get("session_id"),  # 记录供下游使用
        "storyline_artifacts": [],  # 占位(后续不再用)
        "status_log": (state.get("status_log") or [])
        + ["node_02_launch_openstoryline_done"],
        "error_log": errors,
    }


__all__ = ["launch_openstoryline_service", "_wait_for_ready", "_default_popen"]