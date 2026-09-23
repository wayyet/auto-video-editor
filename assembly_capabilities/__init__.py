"""video-agent-kit 工具能力集(完全解耦版)。

按 ADR-1:将 ``video-agent-kit`` 0.4.3 ``mcp/ve_tools/`` 中 5 个工具文件
(``media.py`` / ``video_observe.py`` / ``timeline.py`` / ``render.py`` / ``qc.py``)
里的纯逻辑函数搬进本目录,作为普通 Python 函数使用,不引入 MCP 协议库、不
启动 MCP Server、不连接 ``video-agent-kit`` 仓库。

按 ADR-4:``RunContext`` 精简为"每次新建一份、不做跨调用记忆",因为本仓库
场景是单次管线调用,不需要原 MCP 长会话跨调用的 active_video / transcript
记忆。
"""
from __future__ import annotations

__all__ = [
    "ToolResult",
    "RunContext",
    "inspect_media",
    "analyze_media",
    "speech_transcribe",
    "video_ingest",
    "validate_timeline",
    "timeline_diff",
    "render_preview",
    "qc_preview",
]
