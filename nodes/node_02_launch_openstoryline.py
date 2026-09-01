"""节点 2:launch_openstoryline_service — 对应 /openstoryline-launcher(附件 1.3 节)。

拉起 MCP Server + Web 前端(FastAPI/uvicorn)。健康检查失败时
openstoryline_ready=False,但节点**不抛异常**——由下游节点 3/4 决定是否
早退(附件 1.3 单测要点"节点只管产出状态")。
"""

from __future__ import annotations

import subprocess
import time

import httpx

from config import (
    OPENSTORYLINE_CMD,
    OPENSTORYLINE_HEALTH_TIMEOUT_S,
    openstoryline_mcp_url,
    openstoryline_web_url,
)
from state import WorkflowState


def _wait_for_ready(url: str, timeout_s: int) -> bool:
    """轮询健康检查端点,直到 200 或超时。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def launch_openstoryline_service(
    state: WorkflowState,
    *,
    popen_factory=subprocess.Popen,
    health_checker: callable | None = None,
) -> dict:
    """启动 OpenStoryline 服务并等待就绪。

    Args:
        state: 当前工作流状态。
        popen_factory: 可注入的 subprocess.Popen 工厂,默认 subprocess.Popen。
        health_checker: 可选健康检查函数(接受 url+timeout_s 返回 bool),
            None 时使用内置 _wait_for_ready。

    Returns:
        更新后的 state,含 openstoryline_pid / mcp_endpoint / web_url /
        openstoryline_ready;若进程未启动成功,ready=False 且 pid=None。
    """
    errors = list(state.get("error_log", []) or [])
    try:
        proc = popen_factory(
            OPENSTORYLINE_CMD,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as e:  # noqa: BLE001
        errors.append(f"[node_02] 启动 OpenStoryline 失败: {e}")
        return {
            **state,
            "openstoryline_pid": None,
            "openstoryline_mcp_endpoint": None,
            "openstoryline_web_url": openstoryline_web_url(),
            "openstoryline_ready": False,
            "error_log": errors,
        }

    mcp_endpoint = openstoryline_mcp_url()
    web_url = openstoryline_web_url()
    checker = health_checker or _wait_for_ready
    ready = checker(web_url, OPENSTORYLINE_HEALTH_TIMEOUT_S)
    if not ready:
        errors.append(
            f"[node_02] OpenStoryline 健康检查超时 "
            f"({OPENSTORYLINE_HEALTH_TIMEOUT_S}s),url={web_url}"
        )

    return {
        **state,
        "openstoryline_pid": proc.pid,
        "openstoryline_mcp_endpoint": mcp_endpoint,
        "openstoryline_web_url": web_url,
        "openstoryline_ready": ready,
        "error_log": errors,
    }
