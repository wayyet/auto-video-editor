"""AI Transition 桩(plan_v4 §5 阶段 5 / ADR-005)。

ADR-005 默认关闭 AI Transition。本模块**只**在 ``STORYLINE_ENABLE_AI_TRANSITION=1``
时才被节点壳子调用,默认状态返回 ``{"enabled": False}`` 让节点 noop。

真实 AI 转场(可控视频扩散模型)依赖 torch + GPU + 模型权重,主 venv 不能装,
所以本地化版本 = 接口 + 默认 stub。后续要上线时再写 ``RealAITransitionClient``
注入位。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


ENABLE_AI_TRANSITION: bool = os.environ.get(
    "STORYLINE_ENABLE_AI_TRANSITION", "0"
).lower().strip() in ("1", "true", "yes")


@runtime_checkable
class AITransitionClient(Protocol):
    """AI Transition 接口;默认实现写空 mp4 占位。"""

    def generate(
        self,
        *,
        from_clip: dict[str, Any],
        to_clip: dict[str, Any],
        output_dir: Path,
    ) -> "AITransitionResult":
        ...


@dataclass
class AITransitionResult:
    """AI Transition 结果(plan §3.2 字段 ``storyline_ai_transition_artifact``)。"""

    artifact_path: Path
    from_clip_id: str
    to_clip_id: str
    duration_ms: int
    provider: str = "stub"
    error: Optional[str] = None


class StubAITransitionClient:
    """默认 stub:写最小空 mp4 占位文件(无视频流,1 sample 帧),让节点壳子
    知道跑过了。
    """

    def generate(
        self,
        *,
        from_clip: dict[str, Any],
        to_clip: dict[str, Any],
        output_dir: Path,
    ) -> AITransitionResult:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        ts_ms = int(time.time() * 1000)
        from_id = str(from_clip.get("clip_id") or "from")
        to_id = str(to_clip.get("clip_id") or "to")
        artifact = output_dir / f"ai_transition_{from_id}_{to_id}_{ts_ms}.json"
        data = {
            "from_clip_id": from_id,
            "to_clip_id": to_id,
            "duration_ms": 500,
            "provider": "stub",
            "generated_at_ms": ts_ms,
        }
        artifact.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return AITransitionResult(
            artifact_path=artifact,
            from_clip_id=from_id,
            to_clip_id=to_id,
            duration_ms=500,
            provider="stub",
        )


_ACTIVE_CLIENT: Optional[AITransitionClient] = None


def get_default_ai_transition_client() -> AITransitionClient:
    global _ACTIVE_CLIENT
    if _ACTIVE_CLIENT is None:
        _ACTIVE_CLIENT = StubAITransitionClient()
    return _ACTIVE_CLIENT


def set_default_ai_transition_client(client: Optional[AITransitionClient]) -> None:
    global _ACTIVE_CLIENT
    _ACTIVE_CLIENT = client


def is_ai_transition_enabled() -> bool:
    return ENABLE_AI_TRANSITION


__all__ = [
    "AITransitionClient",
    "AITransitionResult",
    "ENABLE_AI_TRANSITION",
    "StubAITransitionClient",
    "get_default_ai_transition_client",
    "is_ai_transition_enabled",
]