"""测试用的慢 MCP 服务:一个工具 nap(seconds),睡够时间再返回。用来测「MCP 工具调用超时」。"""
import time

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("slow")


@mcp.tool()
def nap(seconds: float) -> str:
    """睡 seconds 秒后返回。"""
    time.sleep(seconds)
    return f"slept {seconds}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
