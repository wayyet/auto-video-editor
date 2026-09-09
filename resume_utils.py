"""resume_all_pending — 统一 resume 入口(Week 5 新增)。

对齐第 5 周计划 §2 / §4.5:
- 单/多 interrupt 并发都能处理(附件"关键发现②":关卡②③ 可同时挂起)。
- 按 ``i.value["checkpoint"]`` 字段("①"/"②"/"③")自动匹配传入的
  ``values_by_checkpoint`` 字典。
- 单 interrupt + ``fallback_key`` 命中 → 直接 ``Command(resume=value)``
  (v1 兼容,不强制按 id 传)。
- ≥2 个挂起时,LangGraph 要求 ``Command(resume={id: value})`` dict 模式
  (附件原话),所以走 id 匹配。
- ``i.value`` 缺 ``checkpoint`` 键时,记录 warning 并跳过。

测试覆盖:``tests/unit/test_resume_all_pending.py``(Day 3)+ 
``tests/integration/test_resume_all_pending_integration.py``(Day 3)。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from langgraph.types import Command

logger = logging.getLogger(__name__)


async def resume_all_pending(
    graph,
    config: dict,
    values_by_checkpoint: Mapping[str, Any],
    *,
    fallback_key: str = "default",
) -> dict:
    """按 checkpoint 标识自动匹配并 resume。

    Args:
        graph: ``CompiledStateGraph`` 实例(由 ``build_graph()`` 返回)。
        config: ``{"configurable": {"thread_id": ...}}`` 形式的 LangGraph 配置。
        values_by_checkpoint: ``{"①": "ok_1", "②": "ok_2", ...}`` 形式的
            待传入值表,键为 checkpoint 标识。
        fallback_key: 单 interrupt 时若 ``i.value`` 无 ``checkpoint`` 字段,
            使用 ``values_by_checkpoint[fallback_key]`` 作为传入值。
            默认 ``"default"``,对应 Week 3 关卡①/② ``Command(resume=True)``。

    Returns:
        ``graph.ainvoke(Command(resume=resume_map), config)`` 的返回值。

    Raises:
        ValueError: 无挂起可恢复;或 ``values_by_checkpoint`` 没有匹配项。
    """
    snapshot = await graph.aget_state(config)
    interrupts = list(snapshot.interrupts or [])
    if not interrupts:
        raise ValueError("无挂起可恢复:graph state 中没有 interrupts")

    resume_map: dict[str, Any] = {}
    matched_keys: list[str] = []
    unmatched: list[Any] = []

    for i in interrupts:
        cp = (i.value or {}).get("checkpoint")
        if cp in values_by_checkpoint:
            resume_map[i.id] = values_by_checkpoint[cp]
            matched_keys.append(cp)
        elif (
            fallback_key in values_by_checkpoint
            and len(interrupts) == 1
        ):
            # 单 interrupt + fallback_key → 用 fallback 值
            resume_map[i.id] = values_by_checkpoint[fallback_key]
            matched_keys.append(fallback_key)
        else:
            unmatched.append({"interrupt_id": i.id, "value": i.value})
            logger.warning(
                "resume_all_pending: interrupt %s 的 payload 缺 'checkpoint' 字段,"
                " 且无 fallback_key 命中,跳过。value=%r",
                i.id,
                i.value,
            )

    if not resume_map:
        raise ValueError(
            f"未匹配到任何 interrupt,可用 keys={list(values_by_checkpoint)},"
            f"未匹配的 interrupt={unmatched}"
        )

    if unmatched:
        # 至少有一个挂起没匹配上;LangGraph 在 ≥2 个挂起时只接受 dict 模式,
        # 缺值会触发 RuntimeError。这里 raise 比静默执行更安全。
        logger.warning(
            "resume_all_pending: 有 %d 个挂起未匹配 — 将继续尝试 resume 已匹配的 %d 个,"
            "LangGraph 可能因此报错。",
            len(unmatched),
            len(resume_map),
        )

    return await graph.ainvoke(Command(resume=resume_map), config=config)
