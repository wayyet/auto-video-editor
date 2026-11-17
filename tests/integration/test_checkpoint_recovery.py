"""LangGraph checkpoint 恢复语义集成测试(P1-01/P1-02,审查计划 §3.2/§6.2)。

覆盖审查计划 §3.2 要求的两个必须由集成测试证明的场景:

1. 串行失败恢复:节点 A 成功 → 节点 B 抛异常 → 同 thread_id 从 checkpoint
   续跑(None 输入,即 ``resume_workflow.py --continue`` 的底层语义)
   → A 不重复执行,B 重新执行成功;
2. 并行 superstep 恢复:a 分叉 b_ok/b_fail 汇合 join;b_ok 已提交的
   pending write 不重放执行,b_fail 重新执行,join 收齐两路结果。

checkpointer 用 ``AsyncPostgresSaver``(与生产 ``graph.get_checkpointer``
同类型)指向 ``isolated_postgres_db`` 隔离库 —— 内存 saver 只能证明图语义,
不能证明 PostgreSQL 持久化语义(审查计划 §7.1 注意事项)。Windows 上
async psycopg 必须 SelectorEventLoop(与 ``web/gate_state.py`` 同款纪律),
因此用同步测试 + ``asyncio.run(loop_factory=SelectorEventLoop)``,不走
pytest-asyncio 的默认事件循环。

前置:PG Docker 容器(edb-postgres16)已启动;连不上由夹具直接 fail
(不静默 skip,避免"假绿")。
"""

from __future__ import annotations

import asyncio
import operator
import selectors
import sys
from typing import Annotated, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph


class RecoveryState(TypedDict, total=False):
    seen: Annotated[list[str], operator.add]


def _run_async(coro):
    """Windows 上 async psycopg 需要 Selector 事件循环(Proactor 不兼容)。"""
    if sys.platform == "win32":
        return asyncio.run(
            coro,
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(coro)


def test_serial_failure_recovery(isolated_postgres_db):
    """P1-01 串行:A 成功、B 失败;同 thread 续跑后 A 不重复、B 成功。

    对应 ``scripts/resume_workflow.py --continue`` 的底层语义:
    ``run_with_events(g, None, config)`` → LangGraph 从最后 checkpoint 恢复,
    失败 superstep 的写入未提交,失败节点被重新执行。
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    uri = isolated_postgres_db()

    async def _run():
        calls = {"a": 0, "b": 0}

        async def node_a(state):
            calls["a"] += 1
            return {"seen": ["a"]}

        async def node_b(state):
            calls["b"] += 1
            if calls["b"] == 1:
                raise RuntimeError("controlled failure")
            return {"seen": ["b"]}

        builder = StateGraph(RecoveryState)
        builder.add_node("a", node_a)
        builder.add_node("b", node_b)
        builder.add_edge(START, "a")
        builder.add_edge("a", "b")
        builder.add_edge("b", END)
        async with AsyncPostgresSaver.from_conn_string(uri) as saver:
            await saver.setup()
            graph = builder.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": "recovery-serial-1"}}
            # 第一次运行:A 成功,B 抛异常,工作流失败但 checkpoint 保留
            with pytest.raises(RuntimeError):
                await graph.ainvoke({"seen": []}, config)
            assert calls == {"a": 1, "b": 1}, "首跑:A 成功、B 失败"
            # 第二次运行:同一 thread_id,None 输入续跑(与 --continue 一致)
            result = await graph.ainvoke(None, config)
        assert calls["a"] == 1, "已成功节点 A 绝不重复执行"
        assert calls["b"] == 2, "失败节点 B 重新执行"
        assert result["seen"] == ["a", "b"]

    _run_async(_run())


def test_parallel_superstep_pending_writes_recovery(isolated_postgres_db):
    """P1-02 并行 superstep:b_ok 已提交的 pending write 不重放执行。

    同一 superstep 内 b_ok 成功、b_fail 失败:恢复后 b_ok 不再重新执行
    (其写入已作为 pending write 提交),b_fail 重新执行,汇合节点只在
    两路结果齐备后执行。此场景必须由真实持久化 checkpointer 证明,
    不能用串行图的结果代替(审查计划 §3.2)。
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    uri = isolated_postgres_db()

    async def _run():
        calls = {"b_ok": 0, "b_fail": 0, "join": 0}

        async def b_ok(state):
            calls["b_ok"] += 1
            return {"seen": ["b_ok"]}

        async def b_fail(state):
            calls["b_fail"] += 1
            if calls["b_fail"] == 1:
                raise RuntimeError("controlled failure in parallel branch")
            return {"seen": ["b_fail"]}

        async def join(state):
            calls["join"] += 1
            return {}

        builder = StateGraph(RecoveryState)
        builder.add_node("b_ok", b_ok)
        builder.add_node("b_fail", b_fail)
        builder.add_node("join", join)
        builder.add_edge(START, "b_ok")
        builder.add_edge(START, "b_fail")
        builder.add_edge(["b_ok", "b_fail"], "join")
        builder.add_edge("join", END)
        async with AsyncPostgresSaver.from_conn_string(uri) as saver:
            await saver.setup()
            graph = builder.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": "recovery-parallel-1"}}
            with pytest.raises(RuntimeError):
                await graph.ainvoke({"seen": []}, config)
            assert calls == {"b_ok": 1, "b_fail": 1, "join": 0}, \
                "首跑:两并行节点同 superstep,失败阻止 join"
            result = await graph.ainvoke(None, config)
        assert calls["b_ok"] == 1, "并行 superstep:已成功节点不重复执行(pending write 重放)"
        assert calls["b_fail"] == 2, "失败分支重新执行"
        assert calls["join"] == 1, "汇合节点只在两路结果齐备后执行一次"
        assert sorted(result["seen"]) == ["b_fail", "b_ok"]

    _run_async(_run())
