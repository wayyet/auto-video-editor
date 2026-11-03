"""会话—工作流关联集成测试(执行设计 §10.4 任务 2.7,六场景)。

场景清单(阶段 2 门禁):

1. **Web 启动**:initial state 含 ``chat_session_id``(= ``POST /runs`` 透传
   后经 CLI ``--initial-state-json`` 到达 runner 的形态)→ 关联 running →
   跑完 → completed;
2. **CLI 启动**:``scripts.run_workflow._load_initial_state`` 注入
   ``--chat-session-id`` → 同一条关联;
3. **interrupt 挂起**:→ waiting_human;
4. **resume**:``Command(resume=...)``(**输入不含** chat_session_id,证明
   是从 checkpoint State 读取,§4.4 第 2 条)→ running → completed;
5. **failed**:节点异常 → run.failed → failed;
6. **旧调用兼容**:不传 ``chat_session_id`` → 不产生任何关联,运行不受影响。

测试图用最小 StateGraph + InMemorySaver;事件存储用进程内假存储;关联存储
用**真** ``WorkflowChatLinkStore`` 打 ``isolated_postgres_db`` 隔离库
(每会话唯一库,不碰生产库;连不上直接 fail,方案 §7.2)。
"""

from __future__ import annotations

import argparse
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from monitoring.workflow_chat_link_store import (
    ChatLinkValidationError,
    WorkflowChatLinkStore,
)
from monitoring.workflow_runner import run_with_events

_CSID = "0123456789abcdef0123456789abcdef"


# ---------------------------------------------------------------------------
# 测试图(最小探针;state schema 必须含 chat_session_id,否则 LangGraph
# 会把该键从 state 通道里丢掉,checkpoint 就读不到了)
# ---------------------------------------------------------------------------
class LinkProbeState(TypedDict, total=False):
    chat_session_id: str
    go: bool
    seen: Annotated[list[str], __import__("operator").add]


def _node_start(state: dict) -> dict:
    return {"seen": ["start"]}


def _node_cp(state: dict) -> dict:
    answer = interrupt({"checkpoint": "probe-cp", "instructions": "probe interrupt"})
    return {"seen": [f"cp:{answer}"]}


def _interrupt_graph() -> Any:
    g = StateGraph(LinkProbeState)
    g.add_node("start", _node_start)
    g.add_node("cp", _node_cp)
    g.add_edge(START, "start")
    g.add_edge("start", "cp")
    g.add_edge("cp", END)
    return g.compile(checkpointer=InMemorySaver())


def _node_done(state: dict) -> dict:
    return {"seen": ["done"]}


def _done_graph() -> Any:
    g = StateGraph(LinkProbeState)
    g.add_node("start", _node_start)
    g.add_node("done", _node_done)
    g.add_edge(START, "start")
    g.add_edge("start", "done")
    g.add_edge("done", END)
    return g.compile(checkpointer=InMemorySaver())


def _node_boom(state: dict) -> dict:
    raise ValueError("boom")


def _boom_graph() -> Any:
    g = StateGraph(LinkProbeState)
    g.add_node("start", _node_start)
    g.add_node("boom", _node_boom)
    g.add_edge(START, "start")
    g.add_edge("start", "boom")
    g.add_edge("boom", END)
    return g.compile(checkpointer=InMemorySaver())


class _FakeStore:
    """进程内假事件存储(本文件只关注关联表;append 即分配自增 id)。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._next = 1

    def append_event(self, event: dict) -> int:
        stored = dict(event)
        stored["id"] = self._next
        self._next += 1
        self.events.append(stored)
        return stored["id"]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
@pytest.fixture
def link_db_uri(isolated_postgres_db) -> str:
    """隔离库 + 预置一个聊天会话行(FK 目标);返回 URI。"""
    import psycopg

    uri = isolated_postgres_db()
    with psycopg.connect(uri, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_sessions (session_id) VALUES (%s) "
                "ON CONFLICT (session_id) DO NOTHING",
                (_CSID,),
            )
        conn.commit()
    return uri


def _types(events: list[dict]) -> list[str]:
    return [e["event_type"] for e in events]


# ---------------------------------------------------------------------------
# 场景 1:Web 启动(initial state = POST /runs 透传产物)→ running → completed
# ---------------------------------------------------------------------------
async def test_web_entry_links_and_completes(link_db_uri: str) -> None:
    link_store = WorkflowChatLinkStore(uri=link_db_uri)
    # POST /runs 会把 chat_session_id 写进 initial_state JSON,CLI 原样传入 runner
    state = {"chat_session_id": _CSID, "go": False}
    config = {"configurable": {"thread_id": "t-web-001"}}

    final = await run_with_events(
        _done_graph(), state, config,
        store=_FakeStore(), link_store=link_store, run_id="run-web01",
    )
    assert final["seen"] == ["start", "done"]

    link = link_store.get_by_chat_session(_CSID)
    assert link is not None
    assert link["chat_session_id"] == _CSID
    assert link["thread_id"] == "t-web-001"
    assert link["run_id"] == "run-web01"
    assert link["status"] == "completed"  # 终态保留,刷新页面仍可显示
    assert link["updated_at"].endswith("Z")


# ---------------------------------------------------------------------------
# 场景 2:CLI 启动(--chat-session-id 经 _load_initial_state 注入)
# ---------------------------------------------------------------------------
def test_cli_initial_state_injects_chat_session_id() -> None:
    from scripts.run_workflow import _chat_session_id_arg, _load_initial_state

    args = argparse.Namespace(
        thread_id="video-cli-001",
        video_input_paths="",
        initial_state_json=None,
        chat_session_id=_CSID,
    )
    state = _load_initial_state(args)
    assert state["chat_session_id"] == _CSID
    # 既有字段语义不动(执行设计 §10.3 红线 2:session_id 仍 = thread_id)
    assert state["session_id"] == "video-cli-001"

    # 不传参数时行为不变(旧调用兼容)
    args_old = argparse.Namespace(
        thread_id="video-cli-001",
        video_input_paths="",
        initial_state_json=None,
        chat_session_id=None,
    )
    assert "chat_session_id" not in _load_initial_state(args_old)

    # argparse 层拒绝非法 ID
    with pytest.raises(argparse.ArgumentTypeError):
        _chat_session_id_arg("not-hex")


async def test_cli_entry_links_and_completes(link_db_uri: str) -> None:
    from scripts.run_workflow import _load_initial_state

    args = argparse.Namespace(
        thread_id="t-cli-001",
        video_input_paths="",
        initial_state_json=None,
        chat_session_id=_CSID,
    )
    state = _load_initial_state(args)
    link_store = WorkflowChatLinkStore(uri=link_db_uri)
    await run_with_events(
        _done_graph(), state, {"configurable": {"thread_id": "t-cli-001"}},
        store=_FakeStore(), link_store=link_store, run_id="run-cli01",
    )
    link = link_store.get_by_chat_session(_CSID)
    assert link["thread_id"] == "t-cli-001"
    assert link["status"] == "completed"


# ---------------------------------------------------------------------------
# 场景 3:interrupt 挂起 → waiting_human
# ---------------------------------------------------------------------------
async def test_interrupt_marks_waiting_human(link_db_uri: str) -> None:
    link_store = WorkflowChatLinkStore(uri=link_db_uri)
    config = {"configurable": {"thread_id": "t-int-001"}}
    events: list[dict] = []
    await run_with_events(
        _interrupt_graph(), {"chat_session_id": _CSID, "go": True}, config,
        store=_FakeStore(), link_store=link_store, run_id="run-int01",
        on_event=events.append,
    )
    assert "run.interrupted" in _types(events)
    assert "run.completed" not in _types(events)

    link = link_store.get_by_chat_session(_CSID)
    assert link["status"] == "waiting_human"
    assert link["thread_id"] == "t-int-001"
    assert link["run_id"] == "run-int01"


# ---------------------------------------------------------------------------
# 场景 4:resume(Command 输入不含 chat_session_id → 必须来自 checkpoint)
# ---------------------------------------------------------------------------
async def test_resume_reads_chat_session_id_from_checkpoint(link_db_uri: str) -> None:
    g = _interrupt_graph()  # 两场必须复用同一图(checkpointer 才连续)
    link_store = WorkflowChatLinkStore(uri=link_db_uri)
    config = {"configurable": {"thread_id": "t-res-001"}}

    # 第一场:跑到 cp 挂起(waiting_human)
    await run_with_events(
        g, {"chat_session_id": _CSID}, config,
        store=_FakeStore(), link_store=link_store, run_id="run-resA",
    )
    assert link_store.get_by_chat_session(_CSID)["status"] == "waiting_human"

    # 第二场:Command(resume=...) —— 输入没有任何 state;关联仍能重建为
    # running 并随完成转 completed ⇒ 证明 chat_session_id 来自 checkpoint
    events: list[dict] = []
    final = await run_with_events(
        g, Command(resume="ok"), config,
        store=_FakeStore(), link_store=link_store, run_id="run-resB",
        on_event=events.append,
    )
    assert _types(events)[0] == "run.resumed"
    assert final["seen"] == ["start", "cp:ok"]

    link = link_store.get_by_chat_session(_CSID)
    assert link["status"] == "completed"
    assert link["thread_id"] == "t-res-001"
    assert link["run_id"] == "run-resB"


# ---------------------------------------------------------------------------
# 场景 5:failed → failed(异常照常冒出,关联仍更新)
# ---------------------------------------------------------------------------
async def test_failed_marks_failed_and_reraises(link_db_uri: str) -> None:
    link_store = WorkflowChatLinkStore(uri=link_db_uri)
    events: list[dict] = []
    with pytest.raises(ValueError, match="boom"):
        await run_with_events(
            _boom_graph(), {"chat_session_id": _CSID},
            {"configurable": {"thread_id": "t-fail-001"}},
            store=_FakeStore(), link_store=link_store, run_id="run-fail01",
            on_event=events.append,
        )
    assert "run.failed" in _types(events)
    assert "run.completed" not in _types(events)
    link = link_store.get_by_chat_session(_CSID)
    assert link["status"] == "failed"


# ---------------------------------------------------------------------------
# 场景 6:旧调用兼容 —— 不传 chat_session_id 不产生关联,运行不受影响
# ---------------------------------------------------------------------------
async def test_without_chat_session_id_no_link_created(link_db_uri: str) -> None:
    link_store = WorkflowChatLinkStore(uri=link_db_uri)
    final = await run_with_events(
        _done_graph(), {"go": False}, {"configurable": {"thread_id": "t-legacy-001"}},
        store=_FakeStore(), link_store=link_store,
    )
    assert final["seen"] == ["start", "done"]
    assert link_store.get_by_chat_session(_CSID) is None, (
        "旧调用(无 chat_session_id)不得创建任何关联"
    )


# ---------------------------------------------------------------------------
# 补充:同一会话先后两次运行 → 关联覆盖为最近一次(每会话单行语义)
# ---------------------------------------------------------------------------
async def test_second_run_overwrites_link(link_db_uri: str) -> None:
    link_store = WorkflowChatLinkStore(uri=link_db_uri)
    await run_with_events(
        _done_graph(), {"chat_session_id": _CSID},
        {"configurable": {"thread_id": "t-run-1st"}},
        store=_FakeStore(), link_store=link_store,
    )
    await run_with_events(
        _done_graph(), {"chat_session_id": _CSID},
        {"configurable": {"thread_id": "t-run-2nd"}},
        store=_FakeStore(), link_store=link_store,
    )
    link = link_store.get_by_chat_session(_CSID)
    assert link["thread_id"] == "t-run-2nd"
    assert link["status"] == "completed"


def test_invalid_chat_session_id_at_store_level() -> None:
    """非法 ID 在 Store 层拒绝(入口层已各自校验;此处兜底防呆)。"""
    store = WorkflowChatLinkStore(
        uri="postgresql://postgres:devpass@127.0.0.1:5991/no_such_db"
    )
    with pytest.raises(ChatLinkValidationError):
        store.upsert_running("bad-id", "t-x")
