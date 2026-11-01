"""统一 Runner 流事件集成测试(设计《工作流流程图与实时节点追踪》§9.6 阶段3)。

覆盖(§9.6 任务 5 清单):

1. 普通起止序列:run.started → node.started/succeeded ×N → run.completed;
2. 并行超步:两个节点同时 started(都不早于对方 succeeded 结束);
3. fan-in 汇合:join 触发器 → branch.selected(免费信号,基线记录 §4.3);
4. interrupt → node.waiting_input + run.interrupted(**不发** succeeded)→
   resume → run.resumed + node.resumed,PG 游标跨运行单调;
5. 重试回路:同一节点再次启动 → attempt 递增 + node.retrying +
   branch.selected(时序推断,§7.3 规则 8);
6. import_video 式 custom 等待事件 → node.waiting_input(web_file_handshake);
7. 节点异常:node.failed(脱敏)+ run.failed,异常照常冒出;
8. 脱敏:state 增量白名单外键值不外泄(提示词/绝对路径);
9. 事件全部落 PG 且 id 单调(isolated 测试库);存储不可用时显式降级
   (事件照发、id 为空、degraded_count 增加),运行不受影响。

契约样例来源:``docs/修正/auto-video-editor_工作流流程图_阶段0基线记录.md`` §4。
测试图用最小 StateGraph + InMemorySaver(checkpointer 必须存在,tasks 模式
需要它);PG 只用于**事件存储**侧,用 ``isolated_postgres_db`` 隔离库。
"""

from __future__ import annotations

import json
import operator
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from monitoring.workflow_events import to_sse_frame
from monitoring.workflow_runner import run_with_events


# ---------------------------------------------------------------------------
# 测试图(探针同款拓扑,基线记录 §4)
# ---------------------------------------------------------------------------
class ProbeState(TypedDict, total=False):
    seen: Annotated[list[str], operator.add]
    go: bool
    n: int


def _writer():
    from langgraph.config import get_stream_writer

    return get_stream_writer()


def _node_a(state: dict) -> dict:
    w = _writer()
    w({"monitor_event": "progress", "node_id": "a", "detail": {"x": 1}})
    return {"seen": ["a"]}


def _node_b1(state: dict) -> dict:
    return {"seen": ["b1"]}


def _node_b2(state: dict) -> dict:
    return {"seen": ["b2"]}


def _node_gate(state: dict) -> dict:
    return {}


def _route_gate(state: dict) -> str:
    return "cp" if state.get("go") else "end"


def _node_cp(state: dict) -> dict:
    answer = interrupt({"checkpoint": "probe-cp", "instructions": "probe interrupt"})
    return {"seen": [f"cp:{answer}"]}


def _probe_graph() -> Any:
    g = StateGraph(ProbeState)
    g.add_node("a", _node_a)
    g.add_node("b1", _node_b1)
    g.add_node("b2", _node_b2)
    g.add_node("gate", _node_gate)
    g.add_node("cp", _node_cp)
    g.add_edge(START, "a")
    g.add_edge("a", "b1")
    g.add_edge("a", "b2")
    g.add_edge(["b1", "b2"], "gate")
    g.add_conditional_edges("gate", _route_gate, {"cp": "cp", "end": END})
    g.add_edge("cp", END)
    return g.compile(checkpointer=InMemorySaver())


def _node_r(state: dict) -> dict:
    return {"n": int(state.get("n") or 0) + 1}


def _route_r(state: dict) -> str:
    return "r" if int(state.get("n") or 0) < 3 else "end"


def _retry_graph() -> Any:
    """自环重试图:r 跑 3 次(attempt 1→3)后走 END。"""
    g = StateGraph(ProbeState)
    g.add_node("r", _node_r)
    g.add_edge(START, "r")
    g.add_conditional_edges("r", _route_r, {"r": "r", "end": END})
    return g.compile(checkpointer=InMemorySaver())


def _node_boom(state: dict) -> dict:
    raise ValueError("api_key=sk-abcdef123456 E:\\私密\\dir\\clip.mp4 炸了")


def _boom_graph() -> Any:
    g = StateGraph(ProbeState)
    g.add_node("boom", _node_boom)
    g.add_edge(START, "boom")
    g.add_edge("boom", END)
    return g.compile(checkpointer=InMemorySaver())


def _node_handshake(state: dict) -> dict:
    w = _writer()
    w({
        "monitor_event": "waiting_input",
        "node_id": "import_video",
        "waiting_kind": "web_file_handshake",
        "detail": {"job_id": "job-probe", "timeout_s": 600},
    })
    return {"seen": ["import_video"]}


def _handshake_graph() -> Any:
    g = StateGraph(ProbeState)
    g.add_node("import_video", _node_handshake)
    g.add_edge(START, "import_video")
    g.add_edge("import_video", END)
    return g.compile(checkpointer=InMemorySaver())


class LeakState(TypedDict, total=False):
    # 泄漏测试专用 schema:白名单外键必须真实存在于 state 才能进节点返回增量
    video_input_paths: list[str]
    internal_prompt: str


class _LeakyNode:
    """返回带敏感内容的 state 增量(白名单/脱敏测试用)。"""

    def __call__(self, state: dict) -> dict:
        return {
            "video_input_paths": ["E:\\媒体目录\\clip.mp4"],
            "internal_prompt": "超级机密提示词内容",
        }


def _leaky_graph() -> Any:
    g = StateGraph(LeakState)
    g.add_node("leak", _LeakyNode())
    g.add_edge(START, "leak")
    g.add_edge("leak", END)
    return g.compile(checkpointer=InMemorySaver())


# ---------------------------------------------------------------------------
# 存储侧工具
# ---------------------------------------------------------------------------
class _FakeStore:
    """进程内假事件存储(不需要 Postgres 的用例);append 即分配自增 id。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._next = 1

    def append_event(self, event: dict) -> int:
        stored = dict(event)
        stored["id"] = self._next
        self._next += 1
        self.events.append(stored)
        return stored["id"]


def _isolate_event_store(monkeypatch: pytest.MonkeyPatch, uri: str) -> None:
    """把默认事件存储指到隔离测试库(resolve_postgres_target 每次动态读 env)。"""
    monkeypatch.setenv("POSTGRES_URI", uri)
    monkeypatch.delenv("AGENT_GATE_POSTGRES_URI", raising=False)


def _types(events: list[dict]) -> list[str]:
    return [e["event_type"] for e in events]


def _by_type(events: list[dict], event_type: str) -> list[dict]:
    return [e for e in events if e["event_type"] == event_type]


# ---------------------------------------------------------------------------
# 1. 普通起止序列 + 并行超步 + fan-in(单次运行覆盖)
# ---------------------------------------------------------------------------
async def test_normal_run_sequence_parallel_and_fanin(isolated_postgres_db, monkeypatch) -> None:
    _isolate_event_store(monkeypatch, isolated_postgres_db())
    events: list[dict] = []
    g = _probe_graph()
    final = await run_with_events(
        g, {"go": False}, {"configurable": {"thread_id": "t-seq"}},
        on_event=events.append,
    )

    # 返回值 = 最终 state values(与 ainvoke 语义一致)
    assert final["seen"] == ["a", "b1", "b2"]

    types = _types(events)
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"

    # 起止序列:a → b1/b2 → gate
    started = [(e["node_id"], e["attempt"]) for e in _by_type(events, "node.started")]
    assert started == [("a", 1), ("b1", 1), ("b2", 1), ("gate", 1)]
    succeeded_nodes = [e["node_id"] for e in _by_type(events, "node.succeeded")]
    assert succeeded_nodes == ["a", "b1", "b2", "gate"]

    # 并行超步:b1/b2 都在任一 succeeded 之前 started(§9.6:同时 started)
    idx = {f"{e['event_type']}:{e['node_id']}": i for i, e in enumerate(events)}
    assert idx["node.started:b1"] < idx["node.succeeded:b1"]
    assert idx["node.started:b2"] < idx["node.succeeded:b1"]
    assert idx["node.started:b1"] < idx["node.succeeded:b2"]

    # 耗时字段存在(succeeded 的 payload 带 duration_ms;SSE 帧平铺到 data 顶层)
    assert all("duration_ms" in e["payload"] for e in _by_type(events, "node.succeeded"))

    # id 单调(emit 序 = 落库序)且全部已分配
    ids = [e["id"] for e in events]
    assert all(i is not None for i in ids)
    assert ids == sorted(ids)

    # PG 落库核对:事件表里的序列与回调收到的完全一致
    from monitoring.workflow_event_store import fetch_events_after, latest_snapshot

    stored = fetch_events_after("t-seq")
    assert [e["id"] for e in stored] == ids
    assert [e["event_type"] for e in stored] == types

    snap = latest_snapshot("t-seq")
    assert snap["available"] is True
    assert snap["event_count"] == len(events)
    assert snap["last_id"] == ids[-1]
    assert snap["node_attempts"] == {"a": 1, "b1": 1, "b2": 1, "gate": 1}


async def test_fanin_join_branch_selected(isolated_postgres_db, monkeypatch) -> None:
    _isolate_event_store(monkeypatch, isolated_postgres_db())
    events: list[dict] = []
    await run_with_events(
        _probe_graph(), {"go": False}, {"configurable": {"thread_id": "t-join"}},
        on_event=events.append,
    )
    joins = _by_type(events, "branch.selected")
    # gate 经列表 fan-in 触发:join:b1+b2:gate(§4.3 免费信号)
    assert any(
        str(e["payload"].get("label", "")).startswith("join:") for e in joins
    ), f"应有 join 触发的 branch.selected,实际 {joins}"
    join_gate = next(e for e in joins if e["node_id"] == "gate")
    assert join_gate["payload"]["inferred"] is True
    assert sorted(join_gate["payload"]["detail"]["from"]) == ["b1", "b2"]


# ---------------------------------------------------------------------------
# 2. interrupt → run.interrupted → resume → run.resumed
# ---------------------------------------------------------------------------
async def test_interrupt_resume_flow(isolated_postgres_db, monkeypatch) -> None:
    _isolate_event_store(monkeypatch, isolated_postgres_db())
    g = _probe_graph()
    config = {"configurable": {"thread_id": "t-int"}}

    first: list[dict] = []
    await run_with_events(g, {"go": True}, config, on_event=first.append)

    # 挂起:waiting_input(interrupt)+ run.interrupted;无 succeeded(cp)、无 completed
    waiting = _by_type(first, "node.waiting_input")
    assert [e["node_id"] for e in waiting] == ["cp"]
    assert waiting[0]["payload"]["waiting_kind"] == "interrupt"
    assert waiting[0]["payload"]["detail"]["checkpoints"] == ["probe-cp"]
    assert _by_type(first, "run.interrupted")
    assert "node.succeeded:cp" not in {
        f"{e['event_type']}:{e['node_id']}" for e in first
    }
    assert not _by_type(first, "run.completed")
    assert first[-1]["event_type"] == "run.interrupted"

    # resume:同一 thread + Command(resume=...)
    second: list[dict] = []
    final = await run_with_events(g, Command(resume="ok"), config, on_event=second.append)

    assert second[0]["event_type"] == "run.resumed"
    resumed = _by_type(second, "node.resumed")
    assert [e["node_id"] for e in resumed] == ["cp"]
    # resumed 不递增 attempt(同一 attempt 续跑,不是重试)
    assert resumed[0]["attempt"] == 1
    assert _by_type(second, "node.succeeded")[0]["node_id"] == "cp"
    assert second[-1]["event_type"] == "run.completed"
    assert final["seen"] == ["a", "b1", "b2", "cp:ok"]

    # PG 游标跨运行单调(thread 中心,恢复续写不回退,§6.2-D)
    from monitoring.workflow_event_store import fetch_events_after

    all_events = fetch_events_after("t-int", last_id=0, limit=1000)
    all_ids = [e["id"] for e in all_events]
    assert all_ids == sorted(all_ids) and len(all_ids) == len(set(all_ids))
    assert len(all_events) == len(first) + len(second)
    # 第一场的最后一条就是 run.interrupted,第二场第一条是 run.resumed
    assert all_events[len(first) - 1]["event_type"] == "run.interrupted"
    assert all_events[len(first)]["event_type"] == "run.resumed"


# ---------------------------------------------------------------------------
# 3. 重试回路:attempt 递增 + retrying + branch.selected 推断
# ---------------------------------------------------------------------------
async def test_retry_attempt_increments(isolated_postgres_db, monkeypatch) -> None:
    _isolate_event_store(monkeypatch, isolated_postgres_db())
    events: list[dict] = []
    await run_with_events(
        _retry_graph(), {"n": 0}, {"configurable": {"thread_id": "t-retry"}},
        on_event=events.append,
    )

    attempts = [(e["attempt"], e["payload"].get("duration_ms")) for e in _by_type(events, "node.started")]
    assert [a for a, _ in attempts] == [1, 2, 3]
    retrying = _by_type(events, "node.retrying")
    assert [e["attempt"] for e in retrying] == [2, 3]
    # 历史保留:3 条 started 各自独立,不是覆盖式
    succeeded = _by_type(events, "node.succeeded")
    assert [e["attempt"] for e in succeeded] == [1, 2, 3]

    # 条件自环的时序推断(§7.3 规则 8):r 第 2/3 次启动时推 (r → r)
    branch = _by_type(events, "branch.selected")
    assert branch, "自环重试应有 branch.selected"
    for e in branch:
        assert e["payload"]["inferred"] is True
        assert e["payload"]["detail"] == {"from": ["r"], "to": "r"}

    from monitoring.workflow_event_store import latest_snapshot

    snap = latest_snapshot("t-retry")
    assert snap["node_attempts"] == {"r": 3}


# ---------------------------------------------------------------------------
# 4. custom 等待事件(import_video 握手语义,§7.3 规则 7)
# ---------------------------------------------------------------------------
async def test_custom_waiting_event(isolated_postgres_db, monkeypatch) -> None:
    _isolate_event_store(monkeypatch, isolated_postgres_db())
    events: list[dict] = []
    await run_with_events(
        _handshake_graph(), {}, {"configurable": {"thread_id": "t-wait"}},
        on_event=events.append,
    )
    waiting = _by_type(events, "node.waiting_input")
    assert len(waiting) == 1
    assert waiting[0]["node_id"] == "import_video"
    assert waiting[0]["payload"]["waiting_kind"] == "web_file_handshake"
    assert waiting[0]["payload"]["detail"]["job_id"] == "job-probe"
    # 等待事件在 succeeded 之前;任务照常收尾
    idx = {f"{e['event_type']}:{e['node_id']}": i for i, e in enumerate(events)}
    assert idx["node.waiting_input:import_video"] < idx["node.succeeded:import_video"]
    assert _types(events)[-1] == "run.completed"


# ---------------------------------------------------------------------------
# 5. 节点异常:node.failed(脱敏)+ run.failed,异常冒出
# ---------------------------------------------------------------------------
async def test_node_failure_sanitized_and_reraised(isolated_postgres_db, monkeypatch) -> None:
    _isolate_event_store(monkeypatch, isolated_postgres_db())
    events: list[dict] = []
    with pytest.raises(ValueError, match="炸了"):
        await run_with_events(
            _boom_graph(), {}, {"configurable": {"thread_id": "t-boom"}},
            on_event=events.append,
        )

    failed = _by_type(events, "node.failed")
    assert len(failed) == 1, "task end error 与外层异常双现,node.failed 只发一次"
    assert failed[0]["node_id"] == "boom"
    err = failed[0]["payload"]["error"]
    assert err["type"] == "ValueError"
    # 脱敏:密钥打码、绝对路径降级文件名
    assert "sk-abcdef123456" not in err["message"]
    assert "私密" not in err["message"]
    assert "clip.mp4" in err["message"]

    run_failed = _by_type(events, "run.failed")
    assert len(run_failed) == 1
    assert run_failed[0]["payload"]["error"]["type"] == "ValueError"
    assert not _by_type(events, "run.completed")


# ---------------------------------------------------------------------------
# 6. state 增量白名单:白名单外键只出现在 keys 名单,值不外泄
# ---------------------------------------------------------------------------
async def test_summary_whitelist_and_sanitization(isolated_postgres_db, monkeypatch) -> None:
    _isolate_event_store(monkeypatch, isolated_postgres_db())
    events: list[dict] = []
    await run_with_events(
        _leaky_graph(), {}, {"configurable": {"thread_id": "t-leak"}},
        on_event=events.append,
    )
    ok = _by_type(events, "node.succeeded")
    assert len(ok) == 1
    summary = ok[0]["payload"]["summary"]
    assert summary["keys"] == ["internal_prompt", "video_input_paths"]
    # 白名单键带值(绝对路径已降级文件名)
    assert summary["video_input_paths"] == ["clip.mp4"]
    # 白名单外键的值绝不出现(整条事件序列化后核对)
    raw = json.dumps(events, ensure_ascii=False)
    assert "超级机密提示词内容" not in raw
    assert "媒体目录" not in raw


# ---------------------------------------------------------------------------
# 7. 存储不可用:显式降级(事件照发、id 空、degraded 计数),运行不受影响
# ---------------------------------------------------------------------------
async def test_store_unavailable_degrades_explicitly(monkeypatch) -> None:
    # 本机必然无监听的端口:连不上 = 降级路径(不能静默假装成功)
    monkeypatch.setenv(
        "POSTGRES_URI", "postgresql://postgres:devpass@127.0.0.1:5991/no_such_db"
    )
    monkeypatch.delenv("AGENT_GATE_POSTGRES_URI", raising=False)
    from monitoring import workflow_event_store

    before = workflow_event_store.degraded_count()
    events: list[dict] = []
    final = await run_with_events(
        _retry_graph(), {"n": 0}, {"configurable": {"thread_id": "t-degraded"}},
        on_event=events.append,
    )
    assert final["n"] == 3  # 运行本身不受事件存储影响
    assert events, "降级时事件仍要发给进程内订阅者"
    assert all(e.get("id") is None for e in events), "未落库的事件不得伪造 id"
    assert workflow_event_store.degraded_count() > before
    # SSE 帧此时省略 id 行(to_sse_frame 契约)
    frame = to_sse_frame(events[0])
    assert not frame.startswith("id:")


# ---------------------------------------------------------------------------
# 8. run.started / run.resumed 的输入判定 + thread_id 校验
# ---------------------------------------------------------------------------
async def test_resume_input_emits_run_resumed_first() -> None:
    """``None`` 输入(崩溃/挂起后从检查点继续)→ 首事件 run.resumed。

    None 输入要求线程已有挂起检查点 —— 先用 go=True 跑到 cp 挂起,再以
    None 输入续跑(与 agent_gate worker 的 continue 路径同语义)。
    """
    store = _FakeStore()
    g = _probe_graph()
    config = {"configurable": {"thread_id": "t-resume-in"}}
    await run_with_events(g, {"go": True}, config, on_event=lambda e: None, store=store)
    events: list[dict] = []
    await run_with_events(g, None, config, on_event=events.append, store=store)
    assert events[0]["event_type"] == "run.resumed"
    assert events[0]["payload"]["reason"] == "resume"
    # None 输入续跑同样按"运行前快照带 interrupt"归因 node.resumed
    assert [e["node_id"] for e in _by_type(events, "node.resumed")] == ["cp"]


async def test_missing_thread_id_rejected() -> None:
    with pytest.raises(ValueError, match="thread_id"):
        await run_with_events(_retry_graph(), {"n": 0}, {"configurable": {}})
