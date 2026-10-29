"""AI Transition 桩(plan_v4 §5 阶段 5 / ADR-005)。

2026-10 起开关 ``STORYLINE_ENABLE_AI_TRANSITION`` **默认开启**(ADR-005 原为默认关闭,
已被"默认全部开启"的决定取代)。这个开关在**建图时**读取:设为 ``0`` 时图里根本不接
``storyline_generate_ai_transition`` / ``storyline_plan_timeline_ai_transition``,拓扑与接入前完全一致
(两个节点自己也检查开关:旧 checkpoint / 单测直接调用时是 noop)。改开关后要重启进程。
当前只有 stub 实现(每个衔接点写一份占位 JSON,不改时间线 clips)。

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


# 2026-10:默认值由 "0" 改为 "true"(与 config.STORYLINE_ENABLE_AI_TRANSITION 保持一致,
# 见 tests/unit/test_flag_defaults_aligned.py)。设为 0 即关闭。
ENABLE_AI_TRANSITION: bool = os.environ.get(
    "STORYLINE_ENABLE_AI_TRANSITION", "true"
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