"""``ToolResult`` 数据类(原样搬自 video-agent-kit 0.4.3 mcp/ve_tools/result.py)。

14 行,纯数据类,无外部依赖。video-agent-kit 全部 8 个工具都返回这个。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    image_paths: list[str] = field(default_factory=list)
    video_paths: list[str] = field(default_factory=list)
