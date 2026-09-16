"""ASR 客户端 — Week 3 步骤 8 使用(对应原文档 4.3 节 + Week 3 补全 §11/P1-2)。

Week 3 默认 ``MockASRClient``,Week 3 补全后新增 ``FireRedASR2SClient``;
通过环境变量 ``ASR_BACKEND=firered`` 切换。

接口约定:返回值是 ``list[dict]``,每项含
- ``text: str`` — 字幕文本
- ``start_s: float`` / ``end_s: float`` — 起止秒数

Week 3 补全要点:
- ``FireRedASR2SClient`` 是真实 ASR 服务接入骨架
- 默认 endpoint: ``http://127.0.0.1:8009/transcribe``
- 服务不可达 → 抛 ``ASRUnavailable``,调用方(节点 8) 降级为 ``MockASRClient``
- 环境变量 ``FIRERED_ASR_ENDPOINT`` 覆盖默认 endpoint
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class ASRClient(Protocol):
    """ASR 客户端协议 — Week 4 真实实现应满足此接口。"""

    def transcribe(self, video_path: str) -> list[dict]:
        ...


class ASRUnavailable(RuntimeError):
    """FireRedASR2S 服务不可达时抛出。调用方应降级为 MockASRClient。"""


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


class FireRedASR2SClient:
    """FireRedASR2S 客户端骨架(Week 3 补全接入)。

    真实模型由 ``E:\\Documents\\kuaishou\\FireRed-OpenStoryline`` 启服务;
    本类只负责 HTTP 调用 + 异常归一化,模型加载由服务侧负责。

    Args:
        endpoint: ASR 服务 URL;None 时读 ``FIRERED_ASR_ENDPOINT`` 环境变量,
            再退到默认 ``http://127.0.0.1:8009/transcribe``。
        timeout_s: HTTP 请求超时(秒)。
        http_client: 注入 httpx.Client(便于单测 mock);None 时按需构造。
    """

    DEFAULT_ENDPOINT = "http://127.0.0.1:8009/transcribe"

    def __init__(
        self,
        endpoint: str | None = None,
        timeout_s: int = 300,
        http_client=None,
    ) -> None:
        self._endpoint = endpoint or os.environ.get(
            "FIRERED_ASR_ENDPOINT", self.DEFAULT_ENDPOINT
        )
        self._timeout = timeout_s
        self._http_client = http_client  # 注入点;None 时 transcribe 内部 lazy 构造

    def transcribe(self, video_path: str) -> list[dict]:
        """POST ``{video_path}`` 到 endpoint,返回 ``[{text, start_s, end_s}, ...]``。

        Raises:
            ASRUnavailable: 服务不可达、超时、返回非 2xx 或响应不是合法 JSON。
        """
        try:
            import httpx  # 局部 import 避免模块加载期硬依赖
        except ImportError as e:
            raise ASRUnavailable(
                "FireRedASR2SClient 依赖 httpx,请先 pip install httpx"
            ) from e

        client = self._http_client or httpx
        try:
            response = client.post(
                self._endpoint,
                json={"video_path": video_path},
                timeout=self._timeout,
            )
        except (httpx.RequestError, httpx.TimeoutException) as e:
            raise ASRUnavailable(
                f"FireRedASR2S 服务不可达: endpoint={self._endpoint}, "
                f"video_path={video_path}, error={type(e).__name__}: {e}"
            ) from e

        if response.status_code >= 400:
            raise ASRUnavailable(
                f"FireRedASR2S 服务返回 HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            data = response.json()
        except Exception as e:
            raise ASRUnavailable(
                f"FireRedASR2S 服务响应不是合法 JSON: {e}"
            ) from e

        if not isinstance(data, list):
            raise ASRUnavailable(
                f"FireRedASR2S 响应应为 list,实际 {type(data).__name__}"
            )

        # 字段完整性校验 — 缺字段或字段不可解析时跳过该条
        normalized: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            text = item.get("text", "")
            # 要求 start_s 和 end_s 都显式存在且可解析为浮点数
            raw_start = item.get("start_s")
            raw_end = item.get("end_s")
            if raw_start is None or raw_end is None:
                continue
            try:
                start_s = float(raw_start)
                end_s = float(raw_end)
            except (TypeError, ValueError):
                continue
            normalized.append({"text": text, "start_s": start_s, "end_s": end_s})
        return normalized


# ---------------------------------------------------------------------------
# 默认 client 解析 — Week 3 补全:支持 ASR_BACKEND=firered 环境变量切换
# ---------------------------------------------------------------------------
def _resolve_default_client() -> ASRClient:
    backend = os.environ.get("ASR_BACKEND", "mock").lower().strip()
    if backend == "firered":
        return FireRedASR2SClient()
    if backend != "mock":
        # 未知 backend:降级为 Mock + warning(通过 error_log 由节点 8 写入)
        # 简化:直接返回 Mock
        pass
    return MockASRClient()


_default_client: ASRClient = _resolve_default_client()


def get_default_client() -> ASRClient:
    return _default_client


def set_default_client(client: ASRClient) -> None:
    """允许单测/Week 4 替换默认 ASR 客户端。"""
    global _default_client
    _default_client = client


def call_asr2s(video_path: str) -> list[dict]:
    """便捷调用 — Week 3 节点 8 的统一入口。

    若默认 client 是 ``FireRedASR2SClient`` 且服务不可达,此处直接抛
    ``ASRUnavailable``,由调用方(节点 8)决定是否降级为 Mock。
    """
    return get_default_client().transcribe(video_path)


def get_active_backend() -> str:
    """返回当前默认 client 对应的 backend 名称(便于状态记录)。

    - ``FireRedASR2SClient`` 实例 → ``"firered"``
    - ``MockASRClient`` 实例 → ``"mock"``
    - 其他 → ``"custom"``
    """
    client = get_default_client()
    if isinstance(client, FireRedASR2SClient):
        return "firered"
    if isinstance(client, MockASRClient):
        return "mock"
    return "custom"