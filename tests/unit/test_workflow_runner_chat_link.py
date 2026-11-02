"""``run_with_events`` 会话关联维护的桩测试(执行设计 §10.4 任务 2.1-2.3/2.6)。

不连库:事件存储用进程内假存储,关联存储用**记录时序的假 store**,验证:

1. upsert 发生在**首个运行事件之前**(§4.4 第 3 条);
2. run.started / run.completed 分别触发 running / completed 流转(§4.4 第 4 条);
3. resume 时从 checkpoint 读取 chat_session_id(Command 输入不含该键);
4. 不传 chat_session_id → 关联 store 零调用(旧 checkpoint 兼容,任务 1.6);
5. 关联 store 抛异常 → 工作流照常完成(§10.4 任务 2.6 降级);
6. import_video 式 custom 等待(node.waiting_input)**不**把关联标成
   waiting_human —— 那是运行中的等待,工作流未挂起(映射表注释的设计决策)。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from monitoring.workflow_runner import run_with_events

_CSID = "0123456789abcdef0123456789abcdef"


class ProbeState(TypedDict, total=False):
    chat_session_id: str
    n: int
    seen: Annotated[list[str], __import__("operator").add]


def _node_inc(state: dict) -> dict:
    return {"n": int(state.get("n") or 0) + 1, "seen": ["inc"]}


def _done_graph() -> Any:
    g = StateGraph(ProbeState)
    g.add_node("inc", _node_inc)
    g.add_edge(START, "inc")
    g.add_edge("inc", END)
    return g.compile(checkpointer=InMemorySaver())


def _node_cp(state: dict) -> dict:
    answer = interrupt({"checkpoint": "probe-cp"})
    return {"seen": [f"cp:{answer}"]}


def _interrupt_graph() -> Any:
    g = StateGraph(ProbeState)
    g.add_node("cp", _node_cp)
    g.add_edge(START, "cp")
    g.add_edge("cp", END)
    return g.compile(checkpointer=InMemorySaver())


def _node_handshake(state: dict) -> dict:
    from langgraph.config import get_stream_writer

    get_stream_writer()({
        "monitor_event": "waiting_input",
        "node_id": "import_video",
        "waiting_kind": "web_file_handshake",
        "detail": {},
    })
    return {"seen": ["import_video"]}


def _handshake_graph() -> Any:
    g = StateGraph(ProbeState)
    g.add_node("import_video", _node_handshake)
    g.add_edge(START, "import_video")
    g.add_edge("import_video", END)
    return g.compile(checkpointer=InMemorySaver())


class _FakeEventStore:
    def __init__(self) -> None:
        self._next = 1

    def append_event(self, event: dict) -> int:
        eid = self._next
        self._next += 1
        return eid


class _TimelineLinkStore:
    """把关联调用按序写入共享 timeline,与事件回调交错可断言先后。"""

    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.calls: list[tuple] = []

    def upsert_running(self, csid: str, tid: str, rid: str | None = None) -> bool:
        self.calls.append(("upsert_running", csid, tid, rid))
        self.timeline.append(f"link:upsert:{tid}")
        return True

    def update_status(
        self, csid: str, status: str, *, thread_id: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        self.calls.append(("update_status", csid, status, thread_id, run_id))
        self.timeline.append(f"link:update:{status}")
        return True


class _RaisingLinkStore:
    """模拟关联存储彻底不可用(§10.4 任务 2.6 降级路径)。"""

    def upsert_running(self, *a: Any, **k: Any) -> bool:
        raise RuntimeError("db down")

    def update_status(self, *a: Any, **k: Any) -> bool:
        raise RuntimeError("db down")


async def test_upsert_precedes_first_event_and_tracks_lifecycle() -> None:
    timeline: list[str] = []
    link_store = _TimelineLinkStore(timeline)

    def _on_event(event: dict) -> None:
        timeline.append(str(event.get("event_type")))

    await run_with_events(
        _done_graph(), {"chat_session_id": _CSID, "n": 0},
        {"configurable": {"thread_id": "t-order"}},
        store=_FakeEventStore(), link_store=link_store, run_id="run-ord01",
        on_event=_on_event,
    )
    assert timeline == [
        "link:upsert:t-order",      # 首个运行事件之前(§4.4 第 3 条)
        "run.started",
        "link:update:running",      # run.started → running
        "node.started",
        "node.succeeded",
        "run.completed",
        "link:update:completed",    # run.completed → completed
    ]
    # run_id 透传给关联行
    assert link_store.calls[0] == ("upsert_running", _CSID, "t-order", "run-ord01")


async def test_resume_upserts_from_checkpoint_before_run_resumed() -> None:
    g = _interrupt_graph()  # 两场必须复用同一图(checkpointer 才连续)
    link_store = _TimelineLinkStore([])
    config = {"configurable": {"thread_id": "t-order-resume"}}
    await run_with_events(
        g, {"chat_session_id": _CSID}, config,
        store=_FakeEventStore(), link_store=link_store, run_id="run-A",
    )
    # 第一场以 interrupt 收尾:关联流转到 waiting_human
    assert link_store.calls[-1] == (
        "update_status", _CSID, "waiting_human", "t-order-resume", "run-A"
    )

    timeline: list[str] = []
    link_store2 = _TimelineLinkStore(timeline)

    def _on_event(event: dict) -> None:
        timeline.append(str(event.get("event_type")))

    await run_with_events(
        g, Command(resume="ok"), config,
        store=_FakeEventStore(), link_store=link_store2, run_id="run-B",
        on_event=_on_event,
    )
    assert timeline == [
        "link:upsert:t-order-resume",   # chat_session_id 来自 checkpoint(Command 输入为空)
        "run.resumed",
        "link:update:running",
        "node.resumed",
        "node.succeeded",
        "run.completed",
        "link:update:completed",
    ]
    assert link_store2.calls[0][3] == "run-B"


async def test_without_chat_session_id_store_never_called() -> None:
    link_store = _TimelineLinkStore([])
    final = await run_with_events(
        _done_graph(), {"n": 0}, {"configurable": {"thread_id": "t-nolink"}},
        store=_FakeEventStore(), link_store=link_store,
    )
    assert final["n"] == 1
    assert link_store.calls == [], "旧调用(无 chat_session_id)不得触碰关联存储"


async def test_link_store_failure_does_not_block_run() -> None:
    events: list[dict] = []
    final = await run_with_events(
        _done_graph(), {"chat_session_id": _CSID, "n": 0},
        {"configurable": {"thread_id": "t-degrade"}},
        store=_FakeEventStore(), link_store=_RaisingLinkStore(),
        on_event=events.append,
    )
    assert final["n"] == 1  # 运行本身不受关联存储影响
    assert [e["event_type"] for e in events][-1] == "run.completed"


async def test_handshake_waiting_input_keeps_link_running_path() -> None:
    timeline: list[str] = []
    await run_with_events(
        _handshake_graph(), {"chat_session_id": _CSID},
        {"configurable": {"thread_id": "t-handshake"}},
        store=_FakeEventStore(), link_store=_TimelineLinkStore(timeline),
    )
    assert "link:update:waiting_human" not in timeline, (
        "custom 握手等待是运行中的等待,不得把关联标成 waiting_human"
    )
    assert timeline[-1] == "link:update:completed"
