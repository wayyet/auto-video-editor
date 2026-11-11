"""AI 转场功能开关(2026-11-12 noop 治理阶段2 重构)。

本模块现在只承载**建图开关** ``ENABLE_AI_TRANSITION``(env
``STORYLINE_ENABLE_AI_TRANSITION``,与 ``config.STORYLINE_ENABLE_AI_TRANSITION``
双拷贝对齐,由 ``tests/unit/test_flag_defaults_aligned.py`` 锁定)。

历史:2026-10~2026-11 这里还住着 ``StubAITransitionClient``(每个衔接点写占位
JSON、恒为默认客户端)。阶段2 起 Stub **整体删除** —— 真实生成走
``storyline_capabilities.ai_transition_provider``(Provider 接口 + MiniMax
Hailuo-02 实现 + ``resolve_default_provider`` 注入位),未配置 Provider /
未获批准时节点层显式降级,**不再产生任何占位 JSON 伪装成功**。
"""
from __future__ import annotations

import os

# 2026-10:默认值由 "0" 改为 "true"(与 config.STORYLINE_ENABLE_AI_TRANSITION 保持一致,
# 见 tests/unit/test_flag_defaults_aligned.py)。设为 0 即关闭。
ENABLE_AI_TRANSITION: bool = os.environ.get(
    "STORYLINE_ENABLE_AI_TRANSITION", "true"
).lower().strip() in ("1", "true", "yes")


def is_ai_transition_enabled() -> bool:
    return ENABLE_AI_TRANSITION


__all__ = [
    "ENABLE_AI_TRANSITION",
    "is_ai_transition_enabled",
]