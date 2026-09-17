"""OpenStoryline MCP 客户端(Phase 1 重写版)。

设计纪律(对照 plan §ADR-002 / ADR-004 / ADR-007):
- 协议:MCP-first,std io(开发期) + Streamable HTTP(生产期 ``http://127.0.0.1:8001/mcp``)。
- 启动时 ``list_tools()`` 动态发现,缓存 ``{tool_name: ToolSpec}``,
  缺失 :data:`storyline.firered_adapter.REQUIRED_TOOLS` 中的任一项即 fail-fast。
- 所有调用必带 ``X-Storyline-Session-Id: <job_id>-storyline`` 头;
  所有 ``call_tool`` 必须含 ``artifact_id`` 参数。
- 错误码归一化(:class:`StorylineErrorCode`):``PROCESS_START_FAILED`` /
  ``MCP_CONNECT_FAILED`` / ``TOOL_NOT_FOUND`` / ``TOOL_EXECUTION_FAILED`` /
  ``TOOL_EXECUTION_TIMEOUT`` / ``CONTRACT_INVALID``。

旧 ``MockOpenStorylineMCPClient`` / ``HTTPOpenStorylineMCPClient`` 保留(改名
``LegacyHTTPOpenStorylineMCPClient``),便于 Week 2 既有单测与单测场景。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Optional

from storyline.contract import StorylineErrorCode
from storyline.firered_adapter import (
    REQUIRED_TOOLS,
    TOOL_REGISTRY,
    ToolSpec,
    list_capability,
)


Transport = Literal["stdio", "streamable-http"]


# ---------------------------------------------------------------------------
# 自定义异常
# ---------------------------------------------------------------------------
class OpenStorylineError(Exception):
    """所有 FireRed-OpenStoryline 相关错误的基类。"""

    def __init__(self, message: str, *, error_code: str, **ctx: Any) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.ctx = ctx


class ProcessStartFailed(OpenStorylineError):
    def __init__(self, message: str, **ctx: Any) -> None:
        super().__init__(message, error_code=StorylineErrorCode.PROCESS_START_FAILED, **ctx)


class MCPConnectFailed(OpenStorylineError):
    def __init__(self, message: str, **ctx: Any) -> None:
        super().__init__(message, error_code=StorylineErrorCode.MCP_CONNECT_FAILED, **ctx)


class ToolNotFound(OpenStorylineError):
    def __init__(self, message: str, **ctx: Any) -> None:
        super().__init__(message, error_code=StorylineErrorCode.TOOL_NOT_FOUND, **ctx)


class ToolExecutionFailed(OpenStorylineError):
    def __init__(self, message: str, **ctx: Any) -> None:
        super().__init__(message, error_code=StorylineErrorCode.TOOL_EXECUTION_FAILED, **ctx)


class ToolExecutionTimeout(OpenStorylineError):
    def __init__(self, message: str, **ctx: Any) -> None:
        super().__init__(message, error_code=StorylineErrorCode.TOOL_EXECUTION_TIMEOUT, **ctx)


class ContractInvalid(OpenStorylineError):
    def __init__(self, message: str, **ctx: Any) -> None:
        super().__init__(message, error_code=StorylineErrorCode.CONTRACT_INVALID, **ctx)


# ---------------------------------------------------------------------------
# 真实 MCP 客户端
# ---------------------------------------------------------------------------
@dataclass
class OpenStorylineMCPClient:
    """MCP 真实客户端(stdio + Streamable HTTP 双模式)。

    Args:
        transport: ``"stdio"`` 或 ``"streamable-http"``。
        session_id: FireRed ``X-Storyline-Session-Id`` 请求头的值;默认 ``job_id + "-storyline"``。
        firered_python: stdio 模式下子进程 Python 可执行路径(指向 FireRed 自带 3.11 环境)。
        firered_root: stdio 模式下的 cwd(影响 `config.toml` 加载)。
        mcp_url: HTTP 模式下 MCP Server URL,默认 ``http://127.0.0.1:8001/mcp``。
        timeout_s: 单次 ``call_tool`` 超时(秒),默认 600。
        connect_retries: ``__aenter__`` 阶段 ``initialize + list_tools`` 重试次数。
        popen_factory: stdio 模式下的进程工厂(便于测试注入)。
        include_ai_transition: True 则把 ``generate_ai_transition`` /
            ``plan_timeline_ai_transition`` 加入 capability 检查(默认 False,ADR-005)。
    """

    transport: Transport = "stdio"
    session_id: Optional[str] = None
    firered_python: Optional[Path] = None
    firered_root: Optional[Path] = None
    mcp_url: str = "http://127.0.0.1:8001/mcp"
    timeout_s: float = 600.0
    connect_retries: int = 3
    popen_factory: Any = subprocess.Popen
    include_ai_transition: bool = False

    # 内部状态(use plain attrs,not dataclass field,避免 init 顺序问题)
    _proc: Optional[Any] = None
    _available_tools: dict[str, ToolSpec] = None  # type: ignore[assignment]
    _raw_tool_schemas: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    _is_ready: bool = False

    def __post_init__(self) -> None:
        # 默认值兜底(__post_init__ 才能改 mutable 默认)
        if self._available_tools is None:
            self._available_tools = {}
        if self._raw_tool_schemas is None:
            self._raw_tool_schemas = {}

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    @property
    def tools(self) -> dict[str, ToolSpec]:
        return dict(self._available_tools)

    # ------------------------------------------------------------------
    # async context manager
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "OpenStorylineMCPClient":
        """启动子进程(或连 HTTP)+ initialize + list_tools + capability check。"""
        if self.transport == "stdio":
            await self._start_stdio()
        else:
            await self._connect_http()
        # list_tools 必跑
        await self._discover_tools()
        self._check_capability()
        self._is_ready = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """回收子进程 / HTTP 连接。"""
        self._is_ready = False
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None

    # ------------------------------------------------------------------
    # 协议:stdio
    # ------------------------------------------------------------------
    async def _start_stdio(self) -> None:
        if self.firered_python is None or self.firered_root is None:
            raise ProcessStartFailed(
                "stdio transport requires firered_python and firered_root",
            )
        cmd = [
            str(self.firered_python),
            "-m",
            "open_storyline.mcp.server",
        ]
        env = {
            **os.environ,
            "PYTHONPATH": str(Path(self.firered_root) / "src"),
            "PYTHONIOENCODING": "utf-8",
        }
        try:
            proc = self.popen_factory(
                cmd,
                cwd=str(self.firered_root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as e:  # noqa: BLE001
            raise ProcessStartFailed(
                f"failed to spawn firered mcp server: {e!r}",
                cmd=cmd,
                cwd=str(self.firered_root),
            ) from e
        self._proc = proc
        # 等子进程 listen 上(粗粒度;MCP stdio 握手由 SDK 处理)
        await asyncio.sleep(0.5)
        if proc.poll() is not None:
            raise ProcessStartFailed(
                f"firered mcp server exited early (code={proc.returncode})",
                returncode=proc.returncode,
            )

    async def _connect_http(self) -> None:
        """HTTP 模式:只校验 endpoint 可达 + initialize。"""
        try:
            import httpx  # type: ignore

            async with httpx.AsyncClient(timeout=30) as cli:
                # FastMCP Streamable HTTP 用 POST;探测时用 initialize JSON-RPC
                payload = {
                    "jsonrpc": "2.0",
                    "id": str(uuid.uuid4()),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "auto-video-editor", "version": "0.1.0"},
                    },
                }
                r = await cli.post(self.mcp_url, json=payload)
                if r.status_code not in (200, 202):
                    raise MCPConnectFailed(
                        f"mcp initialize got HTTP {r.status_code}",
                        body=r.text[:500],
                    )
        except ImportError as e:
            raise MCPConnectFailed(
                "httpx is required for streamable-http transport",
            ) from e
        except MCPConnectFailed:
            raise
        except Exception as e:  # noqa: BLE001
            raise MCPConnectFailed(
                f"failed to connect mcp server at {self.mcp_url}: {e!r}",
            ) from e

    # ------------------------------------------------------------------
    # list_tools + capability
    # ------------------------------------------------------------------
    async def _discover_tools(self) -> None:
        """``list_tools()`` 动态发现工具,缓存到 ``_available_tools``。"""
        last_exc: Exception | None = None
        for attempt in range(self.connect_retries):
            try:
                tools = await self._do_list_tools()
                self._available_tools = {
                    name: TOOL_REGISTRY.get(name) or ToolSpec(
                        name=name, node_class="(unknown)", node_kind="unknown",
                        description=str(schema.get("description") or ""),
                    )
                    for name, schema in tools.items()
                }
                self._raw_tool_schemas = tools
                return
            except Exception as e:  # noqa: BLE001
                last_exc = e
                await asyncio.sleep(min(2 ** attempt, 5))
        raise MCPConnectFailed(
            f"list_tools failed after {self.connect_retries} attempts: {last_exc!r}",
        )

    async def _do_list_tools(self) -> dict[str, dict[str, Any]]:
        """真正调用 list_tools 的协议细节。

        - stdio: 通过 ``mcp`` SDK 的 ClientSession。
        - HTTP: 通过 JSON-RPC POST ``tools/list``。
        """
        if self.transport == "stdio":
            return await self._list_tools_stdio()
        return await self._list_tools_http()

    async def _list_tools_stdio(self) -> dict[str, dict[str, Any]]:
        try:
            from mcp import ClientSession, StdioServerParameters  # type: ignore
            from mcp.client.stdio import stdio_client  # type: ignore
        except ImportError as e:
            raise MCPConnectFailed(
                "`mcp` SDK not installed; pip install 'mcp>=1.0,<2' to use stdio transport",
            ) from e

        assert self.firered_python is not None and self.firered_root is not None
        params = StdioServerParameters(
            command=str(self.firered_python),
            args=["-m", "open_storyline.mcp.server"],
            env={
                "PYTHONPATH": str(Path(self.firered_root) / "src"),
                "PYTHONIOENCODING": "utf-8",
            },
            cwd=str(self.firered_root),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return {t.name: {"description": t.description} for t in result.tools}

    async def _list_tools_http(self) -> dict[str, dict[str, Any]]:
        import httpx  # type: ignore

        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/list",
            "params": {},
        }
        headers = {
            "Content-Type": "application/json",
            "X-Storyline-Session-Id": self._effective_session_id(),
        }
        async with httpx.AsyncClient(timeout=30) as cli:
            r = await cli.post(self.mcp_url, json=payload, headers=headers)
            if r.status_code not in (200, 202):
                raise MCPConnectFailed(
                    f"tools/list got HTTP {r.status_code}",
                    body=r.text[:500],
                )
            data = r.json()
        tools = (data.get("result") or {}).get("tools") or []
        return {t["name"]: t for t in tools}

    def _check_capability(self) -> None:
        """缺必需 tool 即抛 :class:`ToolNotFound`(全量 tool 列表放 ctx)。"""
        required = list_capability(include_ai_transition=self.include_ai_transition)
        missing = [t for t in required if t not in self._available_tools]
        if missing:
            raise ToolNotFound(
                f"required FireRed tools missing: {missing}",
                missing=missing,
                required=required,
                available=list(self._available_tools.keys()),
            )

    # ------------------------------------------------------------------
    # call_tool
    # ------------------------------------------------------------------
    async def call_tool(
        self,
        name: str,
        *,
        artifact_id: Optional[str] = None,
        arguments: dict[str, Any] | None = None,
        timeout_s: Optional[float] = None,
    ) -> dict[str, Any]:
        """调用 MCP 工具,统一带 ``X-Storyline-Session-Id`` 与 ``artifact_id``。

        Args:
            name: MCP tool name。
            artifact_id: 必需(MCP wrapper 强制要求),不传则内部生成 uuid4。
            arguments: 业务参数,会与 ``artifact_id`` 合并到 ``params.arguments``。
            timeout_s: 单次超时,None 时用 self.timeout_s。

        Returns:
            MCP 工具返回 dict(``tool_excute_result`` 在 ``tool_excute_result`` 子键)。

        Raises:
            ToolNotFound: 工具未注册。
            ToolExecutionFailed: FireRed 节点抛 ``isError: true``。
            ToolExecutionTimeout: 超时。
            ContractInvalid: 返回值缺 ``tool_excute_result``。
        """
        if not self._is_ready:
            raise MCPConnectFailed("client not entered; use `async with` first")
        if name not in self._available_tools:
            raise ToolNotFound(
                f"tool {name!r} not in list_tools() result",
                available=list(self._available_tools.keys()),
            )
        args = dict(arguments or {})
        if artifact_id is None:
            artifact_id = uuid.uuid4().hex
        args.setdefault("artifact_id", artifact_id)

        timeout = float(timeout_s if timeout_s is not None else self.timeout_s)
        try:
            raw = await asyncio.wait_for(
                self._do_call_tool(name, artifact_id, args),
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            raise ToolExecutionTimeout(
                f"tool {name!r} timed out after {timeout}s",
                tool=name,
                artifact_id=artifact_id,
            ) from e

        if not isinstance(raw, dict):
            raise ContractInvalid(
                f"tool {name!r} returned non-dict payload",
                payload_type=type(raw).__name__,
            )
        if raw.get("isError"):
            raise ToolExecutionFailed(
                f"tool {name!r} reported isError: {raw.get('summary')!r}",
                tool=name,
                artifact_id=raw.get("artifact_id", artifact_id),
                summary=raw.get("summary"),
            )
        if "tool_excute_result" not in raw:
            raise ContractInvalid(
                f"tool {name!r} returned payload without `tool_excute_result`",
                keys=list(raw.keys()),
            )
        return raw

    async def _do_call_tool(
        self,
        name: str,
        artifact_id: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if self.transport == "stdio":
            return await self._call_tool_stdio(name, artifact_id, args)
        return await self._call_tool_http(name, artifact_id, args)

    async def _call_tool_stdio(
        self,
        name: str,
        artifact_id: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            from mcp import ClientSession, StdioServerParameters  # type: ignore
            from mcp.client.stdio import stdio_client  # type: ignore
        except ImportError as e:
            raise MCPConnectFailed("`mcp` SDK not installed") from e

        assert self.firered_python is not None and self.firered_root is not None
        params = StdioServerParameters(
            command=str(self.firered_python),
            args=["-m", "open_storyline.mcp.server"],
            env={
                "PYTHONPATH": str(Path(self.firered_root) / "src"),
                "PYTHONIOENCODING": "utf-8",
            },
            cwd=str(self.firered_root),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # mcp SDK 自动透传 session header 由本客户端管理;
                # 但 FastMCP 期待 X-Storyline-Session-Id 来自 request.headers,
                # stdio 模式下 server 端拿不到 header,
                # 因此用 kwargs 把 session_id 当参数注入(由各 Node 自己 _load_user_info)
                args_with_session = dict(args)
                args_with_session["session_id"] = self._effective_session_id()
                result = await session.call_tool(name, arguments=args_with_session)
                # result.content 通常是 TextContent 列表;首项 text 字段是 JSON 字符串
                if hasattr(result, "structuredContent") and result.structuredContent:
                    return result.structuredContent
                if hasattr(result, "content") and result.content:
                    for block in result.content:
                        text = getattr(block, "text", None)
                        if text:
                            import json as _json
                            try:
                                return _json.loads(text)
                            except _json.JSONDecodeError:
                                continue
                return {"raw": str(result)}

    async def _call_tool_http(
        self,
        name: str,
        artifact_id: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        import httpx  # type: ignore

        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": {**args, "artifact_id": artifact_id},
            },
        }
        headers = {
            "Content-Type": "application/json",
            "X-Storyline-Session-Id": self._effective_session_id(),
        }
        async with httpx.AsyncClient(timeout=self.timeout_s) as cli:
            r = await cli.post(self.mcp_url, json=payload, headers=headers)
        if r.status_code not in (200, 202):
            raise MCPConnectFailed(
                f"tools/call got HTTP {r.status_code}",
                body=r.text[:500],
            )
        return r.json().get("result") or {}

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------
    async def read_artifact(self, query_artifact_id: str) -> dict[str, Any]:
        """包装 ``read_node_history``(便于 Phase 2 checkpoint resume)。"""
        return await self.call_tool(
            "read_node_history",
            artifact_id=uuid.uuid4().hex,
            arguments={"query_artifact_id": query_artifact_id},
        )

    async def write_skill(
        self,
        skill_name: str,
        skill_content: str,
        *,
        skill_dir: str = ".storyline/skills/",
    ) -> dict[str, Any]:
        """包装 ``write_skills``(Phase 3 用)。"""
        return await self.call_tool(
            "write_skills",
            artifact_id=uuid.uuid4().hex,
            arguments={
                "skill_name": skill_name,
                "skill_content": skill_content,
                "skill_dir": skill_dir,
            },
        )

    def available_tools(self) -> list[str]:
        """返回运行时发现的所有 tool name(用于 phase 0 inventory + snapshot 测试)。"""
        return sorted(self._available_tools.keys())

    def snapshot(self) -> dict[str, list[str]]:
        """返回 ``{required_tools: [...], available_tools: [...]}`` 快照。"""
        return {
            "required_tools": list(REQUIRED_TOOLS),
            "available_tools": self.available_tools(),
        }

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _effective_session_id(self) -> str:
        if self.session_id:
            return self.session_id
        # stdio 模式无显式 session_id,生成占位 uuid4
        return f"av-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# 旧实现(保留,改名便于识别)
# ---------------------------------------------------------------------------
class MockOpenStorylineMCPClient:
    """Mock 实现,返回固定 shot_plan,不发起真实 HTTP 请求。"""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8001/mcp",
        *,
        fixed_plan: dict | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._fixed_plan = fixed_plan or self._default_plan()

    @staticmethod
    def _default_plan() -> dict:
        return {
            "video_path": "",
            "shots": [
                {"id": "shot-1", "video_ref": "video-1", "start_s": 0.0, "end_s": 5.0},
                {"id": "shot-2", "video_ref": "video-1", "start_s": 5.0, "end_s": 10.0},
            ],
        }

    def import_video_and_get_shot_plan(self, *, video_path: str) -> dict:
        plan = dict(self._fixed_plan)
        plan["video_path"] = video_path
        return plan


class LegacyHTTPOpenStorylineMCPClient:
    """Week 2 遗留 HTTP 实现 — 仅供阶段 C 单元测试参考。

    新代码请用 :class:`OpenStorylineMCPClient`(MCP 协议)。
    """

    def __init__(self, endpoint: str, *, timeout_s: float = 30.0) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s

    def _request(self, method: str, params: dict) -> dict:
        import json as _json
        import urllib.request

        body = _json.dumps({"method": method, "params": params}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
            return _json.loads(resp.read().decode("utf-8"))

    def import_video_and_get_shot_plan(self, *, video_path: str) -> dict:
        return self._request(
            "import_video_and_get_shot_plan",
            {"video_path": video_path},
        )


# ---------------------------------------------------------------------------
# 兼容旧 Protocol 接口(node_04/05 import_vec 仍按 Protocol 注入)
# ---------------------------------------------------------------------------
class OpenStorylineMCPProtocol(OpenStorylineMCPClient):
    """向后兼容 Week 2 的 :class:`Protocol` 接口。

    旧代码调 ``client.import_video_and_get_shot_plan(video_path=...)``;
    本类把这条入口路由成 ``load_media`` + ``read_node_history`` 的链。
    """

    async def import_video_and_get_shot_plan(self, *, video_path: str) -> dict:
        """把 ``video_path`` 通过 ``load_media`` 注入,再返回最小可用 shot_plan。"""
        from pathlib import Path as _P
        from storyline.mapper import _path_to_file_uri

        path = _P(video_path)
        if not path.exists():
            raise FileNotFoundError(f"video_path not found: {video_path}")
        file_uri = _path_to_file_uri(str(path))
        artifact = await self.call_tool(
            "load_media",
            arguments={"inputs": [{"file_uri": file_uri, "orig_path": str(path), "orig_md5": ""}]},
        )
        # Phase 1 简化:不调 split_shots,直接返回全段为 1 shot 的最小 shot_plan。
        return {
            "video_path": video_path,
            "load_media_artifact": artifact.get("artifact_id"),
            "shots": [
                {"id": "shot-1", "video_ref": "video-1", "start_s": 0.0, "end_s": 0.0},
            ],
        }


__all__ = [
    "OpenStorylineMCPClient",
    "OpenStorylineMCPProtocol",
    "MockOpenStorylineMCPClient",
    "LegacyHTTPOpenStorylineMCPClient",
    "OpenStorylineError",
    "ProcessStartFailed",
    "MCPConnectFailed",
    "ToolNotFound",
    "ToolExecutionFailed",
    "ToolExecutionTimeout",
    "ContractInvalid",
    "Transport",
]