"""ASR 调用骨架(plan_v4 §5 阶段 5;单流水线架构 2026-11-04 阶段2 清理)。

本地化版本仅留**接口 + 默认 stub**:``STORYLINE_ASR_MODE=stub`` 返回空
segments 数组(默认;CI 烟测与 auto-mode 端到端联调用)。plan_timeline_pro
在无 ASR 数据时退化为无 speech_rough_cut 路径。

历史说明:vendored 模式(subprocess 调 ``.venv-openstoryline`` 的
``open_storyline.runway``)随旧聊天运行时删除 —— 该目标模块在删除前就已
不存在(死链),``VENDORED_OPENSTORYLINE_*`` 配置常量一并移除。
本模块不在主 venv import funasr / torchaudio;不引入新依赖。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模式开关
# ---------------------------------------------------------------------------
ASR_MODE: str = os.environ.get("STORYLINE_ASR_MODE", "stub").lower().strip()
if ASR_MODE != "stub":
    raise ValueError(
        f"STORYLINE_ASR_MODE 只支持 'stub'(vendored 模式已随旧运行时删除,"
        f"当前 {ASR_MODE!r})"
    )


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------
@runtime_checkable
class ASRClient(Protocol):
    """统一 ASR 入口(plan §5 阶段 5 Protocol 抽象)。"""

    def transcribe(
        self,
        *,
        media_path: Path,
        output_dir: Path,
    ) -> "ASRResult":
        """对 ``media_path`` 做转写,产出 ASR JSON 写到 ``output_dir``。"""
        ...


@dataclass
class ASRResult:
    """ASR 转写结果。"""

    artifact_path: Path
    segments: list[dict[str, Any]]
    provider: str          # "stub"
    error: Optional[str] = None
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Stub ASRClient
# ---------------------------------------------------------------------------
class StubASRClient:
    """默认 stub:返回 0 个 segments,plan_timeline_pro 走无 ASR 路径。"""

    def transcribe(
        self,
        *,
        media_path: Path,
        output_dir: Path,
    ) -> ASRResult:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact = output_dir / "asr.json"
        data: dict[str, Any] = {
            "segments": [],
            "text": "",
            "lang": "zh",
            "provider": "stub",
        }
        artifact.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return ASRResult(
            artifact_path=artifact,
            segments=[],
            provider="stub",
        )


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
_ACTIVE_CLIENT: Optional[ASRClient] = None


def get_default_asr_client() -> ASRClient:
    global _ACTIVE_CLIENT
    if _ACTIVE_CLIENT is None:
        _ACTIVE_CLIENT = StubASRClient()
    return _ACTIVE_CLIENT


def set_default_asr_client(client: Optional[ASRClient]) -> None:
    global _ACTIVE_CLIENT
    _ACTIVE_CLIENT = client


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------
def transcribe_media(
    *,
    media_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """转写 + 写 JSON 产物,返回 ``{"artifact": str, "segments": [...], "provider": str,
    "error": str | None}``。
    """
    try:
        client = get_default_asr_client()
        result = client.transcribe(media_path=media_path, output_dir=output_dir)
    except Exception as e:  # noqa: BLE001
        return {
            "artifact": "",
            "segments": [],
            "provider": "error",
            "error": f"asr exception: {e!r}",
        }
    return {
        "artifact": str(result.artifact_path),
        "segments": result.segments,
        "provider": result.provider,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


__all__ = [
    "ASRClient",
    "ASR_MODE",
    "ASRResult",
    "StubASRClient",
    "get_default_asr_client",
    "set_default_asr_client",
    "transcribe_media",
]