"""OpenStoryline MCP 客户端(占位)。

Week 2 提供接口骨架与 Mock 实现;真实协议方法名待 OpenStoryline
`tools/list` 核实后替换 mcp_clients/openstoryline_client.py 一文件即可。
"""

from mcp_clients.openstoryline_client import (
    MockOpenStorylineMCPClient,
    OpenStorylineMCPClient,
)

__all__ = ["MockOpenStorylineMCPClient", "OpenStorylineMCPClient"]
