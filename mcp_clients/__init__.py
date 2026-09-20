"""MCP 客户端集合(2026-09 迁移解耦版)。

迁移后变化:
- ``OpenStorylineMCPClient`` 已删除(改走本地 uvicorn + httpx 健康检查,见
  ``nodes/node_02_launch_openstoryline.py``)。其异常类 ``ContractInvalid``
  迁到 ``storyline.contract``。
- 保留 ``firered_image_edit_client`` / ``jianying_cover_client`` 两个 MCP 客户端
  (节点 15 / 节点 16a 用),与 OpenStoryline 完全解耦。
"""

from mcp_clients.firered_image_edit_client import (  # noqa: F401
    FireRedImageEditClient,
    MockFireRedImageEditClient,
    HTTPFireRedImageEditClient,
    call_firered_edit,
)
from mcp_clients.jianying_cover_client import (  # noqa: F401
    JianyingCoverClient,
    MockJianyingCoverClient,
    HTTPJianyingCoverClient,
    call_write_cover_9x16,
)

__all__ = [
    "FireRedImageEditClient",
    "MockFireRedImageEditClient",
    "HTTPFireRedImageEditClient",
    "call_firered_edit",
    "JianyingCoverClient",
    "MockJianyingCoverClient",
    "HTTPJianyingCoverClient",
    "call_write_cover_9x16",
]