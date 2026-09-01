"""翻译客户端 — Week 4 节点 16 使用(对照计划 §4.4)。

接口约定:同 asr_client.py 模式,返回 ``list[dict]``(每段一条):
- ``index: int`` — 段索引(与 asr_segments_zh 对齐)
- ``start_ms: int`` / ``end_ms: int`` — 起止毫秒
- ``text_en: str`` — 翻译结果

Week 4 用 ``MockTranslateClient``:把中文 text 简单附加 ``"[EN] "`` 前缀,
便于联调跑通;Week 5 替换为真实翻译服务(如 GPT-4 / DeepL)。
"""

from __future__ import annotations

from typing import Protocol


class TranslateClient(Protocol):
    """翻译客户端协议 — Week 5 真实实现应满足此接口。"""

    def translate(self, segments: list[dict]) -> list[dict]:
        ...


class MockTranslateClient:
    """Week 4 Mock — 中文加 ``"[EN] "`` 前缀。

    Args:
        prefix_en: 翻译前缀,便于单测断言。
    """

    def __init__(self, *, prefix_en: str = "[EN] ") -> None:
        self._prefix = prefix_en

    def translate(self, segments: list[dict]) -> list[dict]:
        out: list[dict] = []
        for i, seg in enumerate(segments):
            text_zh = str(seg.get("text_zh", ""))
            out.append({
                "index": i,
                "start_ms": int(seg.get("start_ms", 0)),
                "end_ms": int(seg.get("end_ms", 0)),
                "text_en": f"{self._prefix}{text_zh}",
            })
        return out


_default_client: TranslateClient = MockTranslateClient()


def get_default_client() -> TranslateClient:
    return _default_client


def set_default_client(client: TranslateClient) -> None:
    """允许单测/Week 5 替换默认翻译客户端。"""
    global _default_client
    _default_client = client


def translate_segments(segments: list[dict]) -> list[dict]:
    """便捷调用 — 节点 16 的统一入口。"""
    return get_default_client().translate(segments)