"""resume_all_pending 单测 — 0/1/≥2 个挂起场景(Week 5 新增)。

对齐第 5 周计划 §5.1。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from resume_utils import resume_all_pending


def _make_graph_with_interrupts(interrupts: list[Any]) -> MagicMock:
    """构造一个 mock CompiledStateGraph,aget_state 返回指定挂起列表。"""
    snap = MagicMock()
    snap.interrupts = interrupts

    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=snap)

    # ainvoke 返回 final state
    final = MagicMock()
    graph.ainvoke = AsyncMock(return_value=final)
    return graph, final


def _interrupt(value: dict, iid: str = "iid-x") -> Any:
    """构造 i 对象的最小形态(只有 .id 和 .value 两个属性)。"""
    obj = MagicMock()
    obj.id = iid
    obj.value = value
    return obj


# ---------------------------------------------------------------------------
# 0 个挂起 → raise
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resume_no_pending_raises() -> None:
    graph, _ = _make_graph_with_interrupts([])
    with pytest.raises(ValueError, match="无挂起可恢复"):
        await resume_all_pending(graph, {"configurable": {"thread_id": "t"}}, {"①": "ok"})


# ---------------------------------------------------------------------------
# 1 个挂起 + checkpoint 字段命中 → 走 id 模式
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resume_single_pending_with_checkpoint_match() -> None:
    graph, final = _make_graph_with_interrupts(
        [_interrupt({"checkpoint": "①", "ask": "reorder"})]
    )
    result = await resume_all_pending(
        graph, {"configurable": {"thread_id": "t"}}, {"①": "ok_1"}
    )
    assert result is final
    graph.ainvoke.assert_awaited_once()
    cmd = graph.ainvoke.await_args.args[0]
    # Command(resume=...) 形式
    assert hasattr(cmd, "resume")
    assert cmd.resume == {"iid-x": "ok_1"}


# ---------------------------------------------------------------------------
# 1 个挂起 + payload 缺 checkpoint + fallback_key 命中
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resume_single_pending_fallback_key() -> None:
    graph, final = _make_graph_with_interrupts(
        [_interrupt({"ask": "no checkpoint field"})]  # 缺 checkpoint
    )
    result = await resume_all_pending(
        graph,
        {"configurable": {"thread_id": "t"}},
        {"default": True},
    )
    assert result is final
    cmd = graph.ainvoke.await_args.args[0]
    assert cmd.resume == {"iid-x": True}


# ---------------------------------------------------------------------------
# 2 个挂起 + 都匹配 → 走 dict 模式,按 i.id 分配
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resume_two_pending_dict_mode() -> None:
    graph, final = _make_graph_with_interrupts(
        [
            _interrupt({"checkpoint": "①"}, iid="id-a"),
            _interrupt({"checkpoint": "②"}, iid="id-b"),
        ]
    )
    result = await resume_all_pending(
        graph,
        {"configurable": {"thread_id": "t"}},
        {"①": "ok_a", "②": "ok_b"},
    )
    assert result is final
    cmd = graph.ainvoke.await_args.args[0]
    assert cmd.resume == {"id-a": "ok_a", "id-b": "ok_b"}


# ---------------------------------------------------------------------------
# 2 个挂起 + 1 个 payload 缺 checkpoint → 仍可 resume,但打 warning
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resume_two_pending_one_unmatched(caplog) -> None:
    graph, _ = _make_graph_with_interrupts(
        [
            _interrupt({"checkpoint": "①"}, iid="id-a"),
            _interrupt({"ask": "no checkpoint"}, iid="id-b"),  # 缺
        ]
    )
    with caplog.at_level("WARNING"):
        result = await resume_all_pending(
            graph,
            {"configurable": {"thread_id": "t"}},
            {"①": "ok_a"},
        )
    # 只 resume 已匹配的,unmatched 会打 warning
    assert result is not None
    cmd = graph.ainvoke.await_args.args[0]
    assert cmd.resume == {"id-a": "ok_a"}


# ---------------------------------------------------------------------------
# 1 个挂起 + 没有匹配的 key 且无 fallback → raise
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resume_no_match_raises() -> None:
    graph, _ = _make_graph_with_interrupts(
        [_interrupt({"checkpoint": "①"}, iid="id-a")]
    )
    with pytest.raises(ValueError, match="未匹配到任何 interrupt"):
        await resume_all_pending(
            graph,
            {"configurable": {"thread_id": "t"}},
            {"②": "ok_b"},  # 不匹配
        )
