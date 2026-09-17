"""节点 2:launch_openstoryline_service — Phase 1 MCP 真实探活版。

行为升级(对照 plan §ADR-002 / O2):
- 用 :class:`OpenStorylineMCPClient` 替换旧 ``httpx.get(web_url)`` 假探活。
- stdio 模式下启动 FireRed 子进程 → ``initialize`` → ``list_tools`` →
  capability check,全部成功才 ``openstoryline_ready=True``。
- HTTP 模式下(``WORKFLOW_ENV=production``)跳过子进程启动,直接连 ``STORYLINE_MCP_URL``。
- 启动失败 / 连不上 / 缺必需 tool 时,记 ``error_log`` 并把
  ``storyline_error_code`` 写进 state(供 node_04 决策早退)。

本节点**不抛异常**,与原设计一致 — 由下游节点 3/4 决定是否早退。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from config import (
    OPENSTORYLINE_HEALTH_TIMEOUT_S,
    STORYLINE_CONNECT_RETRIES,
    STORYLINE_ENABLE_AI_TRANSITION,
    STORYLINE_FIRERED_PYTHON,
    STORYLINE_FIRERED_ROOT,
    STORYLINE_MCP_TRANSPORT,
    STORYLINE_MCP_URL,
    WORKFLOW_ENV,
    openstoryline_mcp_url,
    openstoryline_web_url,
    storyline_session_id,
)
from mcp_clients.openstoryline_client import (
    MCPConnectFailed,
    OpenStorylineMCPClient,
    ProcessStartFailed,
    ToolNotFound,
)
from state import WorkflowState
from storyline.contract import StorylineErrorCode
from storyline.firered_adapter import (
    REQUIRED_TOOLS,
    list_capability,
)


def _build_client(job_id: Optional[str]) -> OpenStorylineMCPClient:
    """按 STORYLINE_MCP_TRANSPORT + WORKFLOW_ENV 构造客户端。"""
    if WORKFLOW_ENV == "production" or STORYLINE_MCP_TRANSPORT == "streamable-http":
        return OpenStorylineMCPClient(
            transport="streamable-http",
            session_id=storyline_session_id(job_id),
            mcp_url=STORYLINE_MCP_URL,
            connect_retries=STORYLINE_CONNECT_RETRIES,
            include_ai_transition=STORYLINE_ENABLE_AI_TRANSITION,
        )
    # 默认 stdio:启动子进程
    return OpenStorylineMCPClient(
        transport="stdio",
        session_id=storyline_session_id(job_id),
        firered_python=STORYLINE_FIRERED_PYTHON,
        firered_root=STORYLINE_FIRERED_ROOT,
        connect_retries=STORYLINE_CONNECT_RETRIES,
        include_ai_transition=STORYLINE_ENABLE_AI_TRANSITION,
    )


async def _probe_mcp(
    *,
    job_id: Optional[str],
    popen_factory: Any,
    timeout_s: int = OPENSTORYLINE_HEALTH_TIMEOUT_S,
) -> tuple[bool, dict, Optional[str]]:
    """跑 ``initialize + list_tools + capability check``。

    Returns:
        ``(ready, snapshot_dict, error_code_or_None)``。
    """
    client = _build_client(job_id)
    client.popen_factory = popen_factory
    try:
        async with asyncio.timeout(timeout_s):
            async with client:
                snap = client.snapshot()
                return True, snap, None
    except ProcessStartFailed as e:
        return False, {"error": str(e)}, StorylineErrorCode.PROCESS_START_FAILED
    except MCPConnectFailed as e:
        return False, {"error": str(e)}, StorylineErrorCode.MCP_CONNECT_FAILED
    except ToolNotFound as e:
        return False, {
            "error": str(e),
            "missing": e.ctx.get("missing"),
            "available": e.ctx.get("available"),
        }, StorylineErrorCode.TOOL_NOT_FOUND


def launch_openstoryline_service(
    state: WorkflowState,
    *,
    popen_factory: Any = None,
    health_checker: callable | None = None,
) -> dict:
    """启动 OpenStoryline 服务并等待 MCP 就绪。

    Phase 1 行为:
    - 入参 ``state`` 含 ``session_id``(= job_id);构造
      :class:`OpenStorylineMCPClient` 后 ``__aenter__`` 启动 + list_tools。
    - ``openstoryline_ready=True`` 才算成功;否则记 ``error_log`` 并写
      ``storyline_error_code``。
    - ``health_checker`` 与 ``popen_factory`` 保留注入位,便于测试 mock。
    """
    errors = list(state.get("error_log", []) or [])
    job_id = state.get("session_id")
    popen_factory = popen_factory or __import__("subprocess").Popen

    # 如果用户显式注入 health_checker,走老路径(向后兼容 Week 2 测试)
    pid: Optional[int] = None
    if health_checker is not None:
        # 老路径下也用 popen_factory 拿 PID,保证 ``out["openstoryline_pid"]`` 可用。
        if popen_factory is not None:
            try:
                proc = popen_factory(["python", "-m", "open_storyline.mcp.server"])
                pid = getattr(proc, "pid", None)
            except FileNotFoundError as e:
                errors.append(f"[node_02] 启动 OpenStoryline 失败: {e}")
                return {
                    **state,
                    "openstoryline_pid": None,
                    "openstoryline_mcp_endpoint": openstoryline_mcp_url(),
                    "openstoryline_web_url": openstoryline_web_url(),
                    "openstoryline_ready": False,
                    "error_log": errors,
                }
        ready = bool(health_checker(openstoryline_web_url(), OPENSTORYLINE_HEALTH_TIMEOUT_S))
        if not ready:
            errors.append(
                f"[node_02] OpenStoryline 健康检查超时 "
                f"({OPENSTORYLINE_HEALTH_TIMEOUT_S}s),url={openstoryline_web_url()}"
            )
        return {
            **state,
            "openstoryline_pid": pid,
            "openstoryline_mcp_endpoint": openstoryline_mcp_url(),
            "openstoryline_web_url": openstoryline_web_url(),
            "openstoryline_ready": ready,
            "error_log": errors,
        }

    # 异步跑 MCP probe
    started = time.time()
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None

    if loop is None or loop.is_running():
        # 已被外层 event loop 包住(同步入口),新开 run_until_complete
        ready, snapshot, err_code = asyncio.run(
            _probe_mcp(job_id=job_id, popen_factory=popen_factory)
        )
    else:
        ready, snapshot, err_code = loop.run_until_complete(
            _probe_mcp(job_id=job_id, popen_factory=popen_factory)
        )

    if not ready:
        # ADR-007 错误分类;ProcessStartFailed 单独含"启动 OpenStoryline 失败"子串
        # 便于 Week 2 老测试``test_popen_failure_records_error_and_ready_false``继续生效。
        if err_code == StorylineErrorCode.PROCESS_START_FAILED:
            errors.append(
                f"[node_02] 启动 OpenStoryline 失败: {snapshot.get('error','')} "
                f"err_code={err_code} elapsed_ms={int((time.time() - started) * 1000)}"
            )
        else:
            errors.append(
                f"[node_02] OpenStoryline MCP probe failed: err_code={err_code} "
                f"snapshot={snapshot} elapsed_ms={int((time.time() - started) * 1000)}"
            )
        return {
            **state,
            "openstoryline_pid": None,
            "openstoryline_mcp_endpoint": openstoryline_mcp_url(),
            "openstoryline_web_url": openstoryline_web_url(),
            "openstoryline_ready": False,
            "storyline_session_id": storyline_session_id(job_id),
            "storyline_transport": STORYLINE_MCP_TRANSPORT,
            "storyline_tools_snapshot": list(snapshot.get("available_tools") or []),
            "storyline_error_code": err_code,
            "error_log": errors,
        }

    return {
        **state,
        "openstoryline_pid": None,  # 子进程由 OpenStorylineMCPClient 托管;这里不直接拿 PID
        "openstoryline_mcp_endpoint": (
            STORYLINE_MCP_URL
            if STORYLINE_MCP_TRANSPORT == "streamable-http"
            else openstoryline_mcp_url()
        ),
        "openstoryline_web_url": openstoryline_web_url(),
        "openstoryline_ready": True,
        "storyline_session_id": storyline_session_id(job_id),
        "storyline_transport": STORYLINE_MCP_TRANSPORT,
        "storyline_tools_snapshot": snapshot.get("available_tools", []),
        "storyline_artifacts": [],
        "storyline_error_code": None,
        "error_log": errors,
    }


__all__ = ["launch_openstoryline_service"]


# Mark the required tools at module level for tests to introspect.
# (Not used by the runtime path — see :data:`REQUIRED_TOOLS`.)
_NODE_REQUIRED_TOOLS = REQUIRED_TOOLS
_NODE_OPTIONAL_TOOLS = list_capability(include_ai_transition=False)