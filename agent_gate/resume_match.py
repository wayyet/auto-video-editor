"""resume 配对:把挂起的 interrupt 和调用方传入的值对上(纯函数,无第三方依赖)。

``resume_utils.resume_all_pending``(异步)和 ``agent_gate.runner.resume_pending_sync``
(同步)原先各写了一份同样的配对逻辑,统一放在这里。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


def match_resume_values(
    interrupts: Sequence[Any],
    values_by_checkpoint: Mapping[str, Any],
    *,
    fallback_key: str = "default",
) -> tuple[dict[str, Any], list[Any]]:
    """把 interrupt 和传入值配对。

    规则:
    1. ``interrupt.value["checkpoint"]`` 命中 ``values_by_checkpoint`` → 用该值;
    2. 否则,只有 1 个挂起且存在 ``fallback_key`` → 用 fallback 值;
    3. 其余记为未匹配。

    ``interrupt.value`` 不是 dict(例如 None / 字符串)时,按"没有 checkpoint 字段"处理。

    Returns:
        ``(resume_map, unmatched)``:``resume_map = {interrupt.id: 值}``;
        ``unmatched`` 是没配上的 interrupt 列表。
    """
    resume_map: dict[str, Any] = {}
    unmatched: list[Any] = []
    for i in interrupts:
        payload = i.value if isinstance(i.value, dict) else {}
        cp = payload.get("checkpoint")
        if cp in values_by_checkpoint:
            resume_map[i.id] = values_by_checkpoint[cp]
        elif fallback_key in values_by_checkpoint and len(interrupts) == 1:
            resume_map[i.id] = values_by_checkpoint[fallback_key]
        else:
            unmatched.append(i)
    return resume_map, unmatched
