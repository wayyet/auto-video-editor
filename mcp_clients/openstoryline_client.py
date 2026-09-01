"""OpenStoryline MCP 客户端接口与 Mock 实现。

[TODO: Stage C 开工核实] 真实协议方法名/字段结构。本文件目前只提供接口
骨架与 Mock 实现,接入真实服务时只需替换 _request 方法。
"""

from __future__ import annotations

import json
import urllib.request
from typing import Protocol


class OpenStorylineMCPClient(Protocol):
    """OpenStoryline MCP 客户端协议。

    阶段 C 调用方法:
        client = OpenStorylineMCPClient(endpoint=state["openstoryline_mcp_endpoint"])
        shot_plan = client.import_video_and_get_shot_plan(video_path=...)
    """

    endpoint: str

    def import_video_and_get_shot_plan(self, *, video_path: str) -> dict:
        """导入视频并返回分镜规划(shot_plan)。

        Args:
            video_path: 待剪辑视频的绝对路径。

        Returns:
            shot_plan dict,字段未最终确定,Week 2 用最小可用结构:
            {
                "video_path": <str>,
                "shots": [{"id": <str>, "video_ref": <str>,
                           "start_s": <float>, "end_s": <float>}],
            }
        """
        ...


class MockOpenStorylineMCPClient:
    """Mock 实现,返回固定 shot_plan,不发起真实 HTTP 请求。

    用于单测、集成测试、Week 2 演示。
    """

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8006/mcp",
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
                {
                    "id": "shot-1",
                    "video_ref": "video-1",
                    "start_s": 0.0,
                    "end_s": 5.0,
                },
                {
                    "id": "shot-2",
                    "video_ref": "video-1",
                    "start_s": 5.0,
                    "end_s": 10.0,
                },
            ],
        }

    def import_video_and_get_shot_plan(self, *, video_path: str) -> dict:
        plan = dict(self._fixed_plan)
        plan["video_path"] = video_path
        return plan


class HTTPOpenStorylineMCPClient:
    """HTTP 实现骨架 — 待真实协议确定后填充 _request。

    Week 2 不接入真实服务;保留此类仅为阶段 C 替换时参考签名。
    """

    def __init__(self, endpoint: str, *, timeout_s: float = 30.0) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s

    def _request(self, method: str, params: dict) -> dict:
        body = json.dumps({"method": method, "params": params}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    def import_video_and_get_shot_plan(self, *, video_path: str) -> dict:
        return self._request(
            "import_video_and_get_shot_plan",
            {"video_path": video_path},
        )
