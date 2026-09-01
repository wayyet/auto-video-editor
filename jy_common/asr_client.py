"""ASR 客户端 — Week 3 步骤 8 使用(对应原文档 4.3 节)。

Week 3 用 ``MockASRClient``,按 video_input_path 长度返回固定条数的伪字幕段;
Week 4 替换为真实 ``FireRedASR2S`` 客户端(参考 E:\\Documents\\kuaishou\\FireRed-OpenStoryline)。

接口约定: 返回值是 ``list[dict]``,每项含
- ``text: str`` — 字幕文本
- ``start_s: float`` / ``end_s: float`` — 起止秒数
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ASRClient(Protocol):
    """ASR 客户端协议 — Week 4 真实实现应满足此接口。"""

    def transcribe(self, video_path: str) -> list[dict]:
        ...


class MockASRClient:
    """Week 3 Mock — 根据视频文件大小生成固定条数的伪字幕段。

    Args:
        segments: 预设字幕段数(默认 5),便于单测断言。
    """

    def __init__(self, segments: int = 5) -> None:
        self._segments = segments

    def transcribe(self, video_path: str) -> list[dict]:
        path = Path(video_path)
        # 用文件大小当伪 hash,让 mock 输出有变化但不依赖真实 ASR
        size_hint = path.stat().st_size if path.exists() else 0
        seed = (size_hint or 1) & 0xFFFF

        out: list[dict] = []
        for i in range(self._segments):
            # 每段 2 秒,从 0 开始
            start_s = float(i * 2)
            end_s = float((i + 1) * 2)
            text = f"Mock 字幕 {i + 1} (seed={seed})"
            out.append({"text": text, "start_s": start_s, "end_s": end_s})
        return out


_default_client: ASRClient = MockASRClient()


def get_default_client() -> ASRClient:
    return _default_client


def set_default_client(client: ASRClient) -> None:
    """允许单测/Week 4 替换默认 ASR 客户端。"""
    global _default_client
    _default_client = client


def call_asr2s(video_path: str) -> list[dict]:
    """便捷调用 — Week 3 节点 8 的统一入口。"""
    return get_default_client().transcribe(video_path)