"""storyline_capabilities 内部公共小函数(原先在多个能力模块里各抄一份)。

只放纯函数,不 import ``open_storyline``,保持能力层"无 vendored 依赖"的约束。
"""
from __future__ import annotations

from typing import Any


def clip_duration_sec(clip: dict[str, Any]) -> float:
    """从 clip / caption 的 ``source_ref``(毫秒)算该片段时长(秒);拿不到返回 0。"""
    ref = clip.get("source_ref") or {}
    start = ref.get("start")
    end = ref.get("end")
    if start is None or end is None:
        return 0.0
    try:
        return max(0.0, (float(end) - float(start)) / 1000.0)
    except (TypeError, ValueError):
        return 0.0


def default_user_request(lang: str) -> str:
    """用户没给额外要求时,提示词里填的中性默认文案。"""
    return "无其他要求" if str(lang).lower().startswith("zh") else "No additional requirements"
