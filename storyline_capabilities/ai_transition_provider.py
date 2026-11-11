"""AI 转场视频生成 Provider 适配层(2026-11-12 noop 治理阶段2,设计文档 §2.1)。

参考 FireRed-OpenStoryline ``utils/ai_transition_client.py``(b45011d)的
MiniMax 调用链,按本项目约束重写:

- 只定义 Provider 接口 + 具体 HTTP 实现,**不耦合** FireRed 的 BaseNode /
  NodeState / MoviePy;HTTP 用项目已有依赖 httpx(FireRed 用 requests)。
- API Key 只从服务器端环境变量(``MINIMAX_API_KEY``)读取,不进 state /
  workflow event / 日志。
- **fail-closed**:未配置 Provider(``STORYLINE_AI_TRANSITION_PROVIDER`` 为空)
  时 :func:`resolve_default_provider` 返回 ``None``,节点层据此显式降级,
  0 次外呼、不伪造产物(旧 Stub 写假 JSON 充成功的路径已整体删除)。
- 测试注入位:``set_default_provider``(镜像 ``vlm_client.set_default_client``
  模式);HTTP 层支持注入 ``httpx.Client``(测试用 ``httpx.MockTransport``)。

MiniMax 海螺 Hailuo-02 调用链(基线 b45011d 实测样例,见
docs/第四期开发/noop治理阶段0_双仓基线与资源盘点_20261112.md §2.3)::

    POST {BASE}/video_generation            → task_id
    GET  {BASE}/query/video_generation      → 轮询(默认 15s 间隔)status==Success → file_id
    GET  {BASE}/files/retrieve?file_id=...  → file.download_url
    GET  download_url(stream)              → 本地 mp4
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)

#: 结果状态(provider 侧):生成成功
PROVIDER_STATUS_GENERATED = "generated"
#: 结果状态:Provider 返回失败/超时/网络错误(节点层据此把该 pair 降级为硬切)
PROVIDER_STATUS_FAILED = "failed"


@dataclass
class AITransitionProviderResult:
    """单次转场生成结果(设计文档 §2.1 字段契约)。"""

    path: Optional[Path] = None
    provider: str = ""
    model: str = ""
    duration_ms: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    sha256: str = ""
    request_id: str = ""
    status: str = PROVIDER_STATUS_FAILED
    error: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AITransitionProvider(Protocol):
    """转场视频生成 Provider 接口(设计文档 §2.1)。

    ``first_frame`` / ``last_frame`` 是本地图片文件;实现负责编码传输
    (MiniMax 走 base64 dataURL)。产物校验(ffprobe)由实现负责:返回的
    ``duration_ms/width/height/fps/sha256`` 必须来自**实际产物**,不是提交参数。
    """

    name: str
    model: str

    def generate_transition(
        self,
        *,
        first_frame: Path,
        last_frame: Path,
        prompt: str,
        duration_ms: int,
        resolution: tuple[int, int],
        output_path: Path,
    ) -> AITransitionProviderResult:
        ...


class ProviderNotConfigured(RuntimeError):
    """Provider 名已配置但 Key 缺失 / 名字不认识 —— 节点层据此显式降级。"""


def _image_to_data_url(path: Path) -> str:
    """本地图片 → base64 dataURL(MiniMax first/last_frame_image 接受)。"""
    data = Path(path).read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


class MiniMaxHailuoProvider:
    """MiniMax 海螺视频生成(模型 ``MiniMax-Hailuo-02``,768P)。

    调用链见模块 docstring;轮询间隔/超时可调,贯穿取消检查位(预留)。
    """

    name = "minimax"
    model = "MiniMax-Hailuo-02"
    BASE_URL = "https://api.minimaxi.com/v1"
    DEFAULT_RESOLUTION = "768P"
    DEFAULT_DURATION_S = 6
    POLL_INTERVAL_S = 15

    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: int = 600,
        http_client: Optional[httpx.Client] = None,
        poll_interval_s: int = POLL_INTERVAL_S,
    ) -> None:
        if not api_key:
            raise ProviderNotConfigured(
                "MiniMax Provider 缺少 API Key(环境变量 MINIMAX_API_KEY 为空);"
                "请配置后重跑,或把 STORYLINE_AI_TRANSITION_PROVIDER 留空以显式关闭"
            )
        self._api_key = api_key
        self._timeout_s = int(timeout_s)
        self._poll_interval_s = int(poll_interval_s)
        self._client = http_client  # None = 惰性建真实 client(测试可注入 MockTransport)

    # -- http ---------------------------------------------------------------
    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _closest_resolution(self, resolution: tuple[int, int]) -> str:
        """MiniMax Hailuo-02 目前只有 768P 档;按画幅高度就近映射(预留扩展)。"""
        del resolution
        return self.DEFAULT_RESOLUTION

    # -- 链路 ----------------------------------------------------------------
    def _submit(
        self,
        *,
        prompt: str,
        first_frame: Path,
        last_frame: Path,
        duration_ms: int,
        resolution: tuple[int, int],
    ) -> str:
        duration_s = max(1, int(round(duration_ms / 1000)))
        payload = {
            "model": self.model,
            "prompt": prompt,
            "first_frame_image": _image_to_data_url(first_frame),
            "last_frame_image": _image_to_data_url(last_frame),
            "duration": duration_s,
            "resolution": self._closest_resolution(resolution),
            "prompt_optimizer": False,  # 我们的 prompt 已按模板纪律生成,不再服务端改写
        }
        resp = self._get_client().post(
            f"{self.BASE_URL}/video_generation", json=payload, headers=self._headers()
        )
        resp.raise_for_status()
        task_id = str((resp.json() or {}).get("task_id") or "")
        if not task_id:
            raise RuntimeError(f"MiniMax 提交响应缺少 task_id: {resp.text[:200]}")
        return task_id

    def _poll_once(self, task_id: str) -> tuple[Optional[str], dict[str, Any]]:
        resp = self._get_client().get(
            f"{self.BASE_URL}/query/video_generation",
            params={"task_id": task_id},
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json() or {}
        if data.get("status") == "Success":
            file_id = str(data.get("file_id") or "")
            if not file_id:
                raise RuntimeError(f"MiniMax Success 响应缺少 file_id: {data}")
            r2 = self._get_client().get(
                f"{self.BASE_URL}/files/retrieve",
                params={"file_id": file_id},
                headers=self._headers(),
            )
            r2.raise_for_status()
            url = str((((r2.json() or {}).get("file") or {}).get("download_url")) or "")
            return (url or None), data
        if data.get("status") == "Fail":
            raise RuntimeError(f"MiniMax 任务失败: {data}")
        return None, data

    def _download(self, url: str, output_path: Path) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_client().stream("GET", url) as resp:
            resp.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=8192):
                    f.write(chunk)
        return output_path

    # -- 协议实现 --------------------------------------------------------------
    def generate_transition(
        self,
        *,
        first_frame: Path,
        last_frame: Path,
        prompt: str,
        duration_ms: int,
        resolution: tuple[int, int],
        output_path: Path,
    ) -> AITransitionProviderResult:
        result = AITransitionProviderResult(
            provider=self.name, model=self.model, status=PROVIDER_STATUS_FAILED
        )
        try:
            task_id = self._submit(
                prompt=prompt,
                first_frame=first_frame,
                last_frame=last_frame,
                duration_ms=duration_ms,
                resolution=resolution,
            )
            result.request_id = task_id
            deadline = time.monotonic() + self._timeout_s
            url: Optional[str] = None
            while url is None:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"MiniMax 任务 {task_id} 超时(>{self._timeout_s}s)"
                    )
                url, data = self._poll_once(task_id)
                if url is None:
                    logger.info(
                        "ai_transition provider: task %s processing...", task_id
                    )
                    time.sleep(self._poll_interval_s)
            result.raw = {"task_id": task_id}
            path = self._download(url, output_path)
            # 产物必须经 ffprobe 复检;时长/宽高/fps/sha256 一律取实测值
            from storyline_capabilities.ai_transition_media import describe_video_file

            info = describe_video_file(path)
            result.path = path
            result.duration_ms = info["duration_ms"]
            result.width = info["width"]
            result.height = info["height"]
            result.fps = info["fps"]
            result.sha256 = info["sha256"]
            result.status = PROVIDER_STATUS_GENERATED
        except Exception as e:  # noqa: BLE001 — 失败语义由节点层降级
            result.status = PROVIDER_STATUS_FAILED
            result.error = repr(e)
            logger.warning("ai_transition provider 失败: %r", e)
        return result


# ---------------------------------------------------------------------------
# 默认 Provider 解析(环境配置 → 实例;测试可注入)
# ---------------------------------------------------------------------------
_ACTIVE_PROVIDER: Optional[AITransitionProvider] = None


def set_default_provider(provider: Optional[AITransitionProvider]) -> None:
    """测试/代码注入位(镜像 ``vlm_client.set_default_client`` 模式)。"""
    global _ACTIVE_PROVIDER
    _ACTIVE_PROVIDER = provider


def resolve_default_provider() -> Optional[AITransitionProvider]:
    """按配置解析当前 Provider;未配置返回 ``None``(节点层显式降级,0 次外呼)。

    - ``STORYLINE_AI_TRANSITION_PROVIDER`` 空 / "none" → ``None``;
    - "minimax" → MiniMax 实例(Key 缺失抛 :class:`ProviderNotConfigured`);
    - 其他名字 → :class:`ProviderNotConfigured`(名字打错要响亮报错)。
    """
    global _ACTIVE_PROVIDER
    if _ACTIVE_PROVIDER is not None:
        return _ACTIVE_PROVIDER
    import config as _config

    name = str(getattr(_config, "STORYLINE_AI_TRANSITION_PROVIDER", "") or "").strip().lower()
    if not name or name == "none":
        return None
    if name == MiniMaxHailuoProvider.name:
        return MiniMaxHailuoProvider(
            os.environ.get("MINIMAX_API_KEY", ""),
            timeout_s=int(getattr(_config, "STORYLINE_AI_TRANSITION_TIMEOUT_S", 600)),
        )
    raise ProviderNotConfigured(
        f"未知的 AI 转场 Provider: {name!r}(当前支持: minimax;留空 = 显式关闭)"
    )


__all__ = [
    "AITransitionProvider",
    "AITransitionProviderResult",
    "ProviderNotConfigured",
    "MiniMaxHailuoProvider",
    "PROVIDER_STATUS_GENERATED",
    "PROVIDER_STATUS_FAILED",
    "resolve_default_provider",
    "set_default_provider",
]
