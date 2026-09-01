"""FireRed-Image-Edit MCP 客户端(Week 4 新增,对照计划 §4.3)。

对齐用户决策 Q3:常驻本地推理服务(仿 OpenStoryline MCP Server 模式)。

Week 4 提供:
- ``FireRedImageEditClient`` Protocol — 协议层
- ``MockFireRedImageEditClient`` — 直接复制 zh → en(供联调跑通)
- ``HTTPFireRedImageEditClient`` — HTTP 调用骨架,真实协议留 Week 5 接入
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol


class FireRedImageEditClient(Protocol):
    """FireRed-Image-Edit 客户端协议。

    ``edit`` 把 ``input_image`` 中的中文替换为 ``prompt`` 指定的英文,保持
    字体风格不变,输出到 ``output_image``。
    """

    def edit(
        self,
        *,
        input_image: Path,
        prompt: str,
        output_image: Path,
        seed: int = 43,
    ) -> None:
        ...


class MockFireRedImageEditClient:
    """Week 4 Mock — 直接复制 zh → en(供联调跑通)。

    实现要点:用 ``shutil.copy2`` 保留元数据,确保下游可识别文件已被处理。
    """

    def __init__(self, *, modify_marker: bytes = b"") -> None:
        """``modify_marker``: 写入到 en_path 末尾的标记字节,用于单测断言
        "Mock 路径:文件大小/像素数与源不同(确认被调用过)"。
        """
        self._marker = modify_marker

    def edit(
        self,
        *,
        input_image: Path,
        prompt: str,
        output_image: Path,
        seed: int = 43,
    ) -> None:
        # 防御:输入图必须存在
        if not Path(input_image).exists():
            raise FileNotFoundError(f"input_image not found: {input_image}")

        output_image = Path(output_image)
        output_image.parent.mkdir(parents=True, exist_ok=True)

        # Mock:复制源图,附加 marker 让单测可识别
        shutil.copy2(str(input_image), str(output_image))
        if self._marker:
            with open(output_image, "ab") as f:
                f.write(self._marker)


class HTTPFireRedImageEditClient:
    """HTTP 骨架 — 留 Week 5 接入真实协议(对照 OpenStoryline MCP 模式)。"""

    def __init__(self, endpoint: str, *, timeout_s: float = 60.0) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s

    def edit(
        self,
        *,
        input_image: Path,
        prompt: str,
        output_image: Path,
        seed: int = 43,
    ) -> None:
        raise NotImplementedError("HTTPFireRedImageEditClient 留 Week 5 接入")


# ---------------------------------------------------------------------------
# 默认 client 注册(Week 4 用 Mock)
# ---------------------------------------------------------------------------
_default_client: FireRedImageEditClient = MockFireRedImageEditClient()


def get_default_client() -> FireRedImageEditClient:
    return _default_client


def set_default_client(client: FireRedImageEditClient) -> None:
    """允许单测/Week 5 替换默认客户端。"""
    global _default_client
    _default_client = client


def call_firered_edit(
    *,
    input_image: Path,
    prompt: str,
    output_image: Path,
    seed: int = 43,
) -> None:
    """便捷调用入口 — 节点 15 内的子任务。"""
    return get_default_client().edit(
        input_image=input_image,
        prompt=prompt,
        output_image=output_image,
        seed=seed,
    )