"""阶段 D:来源隔离与 WebSocket 接线测试(SRC-01…SRC-07)。

对应设计文档 §10.2:

======  ==========================================  ==========================
编号    场景                                        验收标准
======  ==========================================  ==========================
SRC-01  选择 ``mcode-operator``                     过程事件全部 ``source=mcode``
SRC-02  选择 ``mcode-chat``                         过程事件全部 ``source=mcode``
SRC-03  选择 DeepSeek                              不出现 MCode 执行面板事件
SRC-04  MCode turn 内 LangGraph 发出 model chunk   WebSocket 不重复转发
SRC-05  MCode CLI 失败                             不用 DeepSeek 内容冒充
SRC-06  两个会话并发                                三类 id 严格隔离
SRC-07  CLI 事件 id 不匹配                         丢弃事件 + 记诊断
======  ==========================================  ==========================

被测的是**真实生产代码路径**:``agent_fastapi`` 的来源判定与转发规则、
``MCodeEventAdapter`` 的事件映射、以及 ``MCodeChatModel`` 与 LangGraph
之间真实的双通道行为(真子进程)。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

import agent_fastapi as web
from open_storyline.config import MCodeConfig, ProjectConfig, Settings, load_settings
from open_storyline.mcode_chat_model import (
    TRANSPORT_FINAL_JSON,
    TRANSPORT_STREAM_JSON,
    MCodeChatModel,
)
from open_storyline.mcode_event_adapter import (
    KIND_FINAL,
    KIND_STATUS,
    KIND_TEXT,
    KIND_TOOL_END,
    KIND_TOOL_PROGRESS,
    KIND_TOOL_START,
    MCodeEventAdapter,
)
from open_storyline.mcode_events import MCodeEvent, MCodeItem, MCodeToolCall, MCodeToolStatus

OPENSTORYLINE_DIR = Path(__file__).resolve().parent.parent

MCODE_OPERATOR = "mcode-operator"
MCODE_CHAT = "mcode-chat"
DEEPSEEK = "deepseek-v4-pro"


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _in_openstoryline_dir(monkeypatch):
    monkeypatch.chdir(OPENSTORYLINE_DIR)


def _settings(tmp_path: Path) -> Settings:
    cfg = load_settings(OPENSTORYLINE_DIR / "config.toml")
    project = ProjectConfig(
        media_dir=tmp_path / "media",
        bgm_dir=tmp_path / "bgm",
        outputs_dir=tmp_path / "outputs",
    )
    return cfg.model_copy(update={"project": project})


def _event(
    etype: str,
    *,
    run_id: str = "run-1",
    session_id: str = "sess-1",
    turn_id: str = "turn-1",
    sequence: int = 1,
    item: MCodeItem | None = None,
    result: Dict[str, Any] | None = None,
    error: Any = None,
) -> MCodeEvent:
    return MCodeEvent(
        type=etype,
        sequence=sequence,
        timestamp_ms=1700000000000 + sequence,
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
        item=item,
        result=result,
        error=error,
    )


def _text_item(item_id: str, delta: str) -> MCodeItem:
    return MCodeItem(id=item_id, type="agent_message", content_delta=delta)


def _tool_item(tool_id: str, name: str, status: MCodeToolStatus, **extra: Any) -> MCodeItem:
    return MCodeItem(
        id=tool_id,
        type="tool_call",
        tool_call=MCodeToolCall(id=tool_id, name=name, status=status, **extra),
    )


# --------------------------------------------------------------------------
# SRC-01 / SRC-02:mcode 档位 -> 过程事件全部 source=mcode
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tier", [MCODE_OPERATOR, MCODE_CHAT])
def test_mcode_tiers_resolve_to_mcode_source(tier):
    """SRC-01 / SRC-02:两个 mcode 档位都判成 mcode 来源。"""
    assert web._resolve_turn_source(tier) == web.TURN_SOURCE_MCODE
    assert web._resolve_turn_source(tier, DEEPSEEK) == web.TURN_SOURCE_MCODE
    assert web._resolve_turn_source(DEEPSEEK, tier) == web.TURN_SOURCE_MCODE


@pytest.mark.parametrize("tier", [MCODE_OPERATOR, MCODE_CHAT])
def test_mcode_events_all_carry_mcode_source(tier, tmp_path):
    """SRC-01 / SRC-02:适配器产出的每条事件都带 ``source=mcode``。"""
    adapter = web._make_mcode_adapter(
        web.TURN_SOURCE_MCODE, _settings(tmp_path), session_id="sess-1", turn_id="turn-1"
    )
    assert adapter is not None

    events: List[MCodeEvent] = [
        _event("exec.started"),
        _event("turn.started", sequence=2),
        _event("item.started", sequence=3, item=_text_item("u1:message", "甲")),
        _event("item.updated", sequence=4, item=_text_item("u1:message", "乙")),
        _event(
            "item.updated", sequence=5,
            item=_tool_item("call_1", "read_notes", MCodeToolStatus.PREPARING),
        ),
        _event(
            "item.updated", sequence=6,
            item=_tool_item("call_1", "read_notes", MCodeToolStatus.FINISHED,
                            output={"content": [{"type": "text", "text": "笔记内容"}]}),
        ),
        _event(
            "exec.completed", sequence=7,
            result={"status": "succeeded", "output": "甲乙"},
        ),
    ]

    produced = []
    for ev in events:
        produced.extend(adapter.adapt(ev))

    assert produced, "一条事件都没产出"
    for internal in produced:
        assert internal.data["source"] == "mcode", internal
        assert internal.data["session_id"] == "sess-1"
        assert internal.data["turn_id"] == "turn-1"
        assert internal.data["run_id"] == "run-1"
        assert isinstance(internal.data["seq"], int)


def test_mcode_tier_resolves_from_session_state_not_browser_param():
    """SRC-01:来源按**会话已生效的模型**判定,浏览器传参不参与。"""
    # 浏览器可以随便传,但服务端只看会话状态。
    assert web._resolve_turn_source(MCODE_OPERATOR) == web.TURN_SOURCE_MCODE
    assert web._resolve_turn_source("") == web.TURN_SOURCE_LLM
    assert web._resolve_turn_source(None) == web.TURN_SOURCE_LLM


# --------------------------------------------------------------------------
# SRC-03:普通模型链路不出现 MCode 事件
# --------------------------------------------------------------------------

def test_llm_source_has_no_mcode_adapter(tmp_path):
    """SRC-03:普通模型链路**不注册** MCode 事件通道。"""
    adapter = web._make_mcode_adapter(
        web.TURN_SOURCE_LLM, _settings(tmp_path), session_id="sess-1", turn_id="turn-1"
    )
    assert adapter is None


def test_llm_source_still_forwards_langgraph_text():
    """SRC-03:普通模型链路的 LangGraph 文本照旧转发(零回归)。"""
    assert web._should_forward_langgraph_text(web.TURN_SOURCE_LLM) is True


def test_llm_source_never_writes_mcode_event(tmp_path):
    """SRC-03:普通模型链路连 ``mcode.event`` 队列都不碰。

    适配器为 None ⇒ WebSocket 主循环里的 ``mcode.event`` 分支直接跳过,
    也就没有任何 ``agent.status`` / ``tool.*`` 之类的「执行面板」事件。
    """
    adapter = web._make_mcode_adapter(
        web.TURN_SOURCE_LLM, _settings(tmp_path), session_id="sess-1", turn_id="turn-1"
    )
    assert adapter is None
    # 规则本身:只有 mcode 来源才建适配器
    assert web._resolve_turn_source(DEEPSEEK) == web.TURN_SOURCE_LLM


# --------------------------------------------------------------------------
# SRC-04:MCode turn 内 LangGraph chunk 不重复转发
# --------------------------------------------------------------------------

def test_mcode_source_does_not_forward_langgraph_text():
    """SRC-04:MCode turn 一律不转发 LangGraph 的 model chunk。"""
    assert web._should_forward_langgraph_text(web.TURN_SOURCE_MCODE) is False


async def test_langgraph_chunk_and_mcode_event_carry_the_same_text(
    tmp_path, fake_mcode_stream_cli
):
    """SRC-04:两条通道拿到的是**同一份文本** —— 所以只能发一次。

    这里跑真实的 LangGraph agent(真子进程 CLI):
    * ``messages`` 通道 = LangGraph 侧看到的 model chunk;
    * sink 通道 = MCode CLI 侧的真实事件。

    两者文本一致 ⇒ WebSocket 层必须二选一,否则用户会看到两遍。
    """
    cli = fake_mcode_stream_cli(deltas=["甲", "乙"], final_output="甲乙")
    seen: List[Any] = []

    async def sink(event: Any) -> None:
        seen.append(event)

    model = MCodeChatModel(
        cwd=str(tmp_path),
        mcode_bin=cli.path,
        streaming_transport=TRANSPORT_STREAM_JSON,
        event_sink_provider=lambda: sink,
    )
    agent = create_agent(model=model, tools=[])

    langgraph_texts: List[str] = []
    async for mode, chunk in agent.astream(
        {"messages": [HumanMessage(content="干活")]},
        stream_mode=["messages", "updates"],
    ):
        if mode != "messages":
            continue
        msg_chunk, meta = chunk
        if meta.get("langgraph_node") != "model":
            continue
        content = getattr(msg_chunk, "content", "")
        if isinstance(content, str) and content:
            langgraph_texts.append(content)

    cli_texts = [e.text_delta for e in seen if e.text_delta]

    assert cli_texts == ["甲", "乙"]
    assert langgraph_texts == cli_texts, "两条通道的文本必须一致"
    # 唯一权威来源:CLI 事件通道。LangGraph 那份被抑制。
    assert web._should_forward_langgraph_text(web.TURN_SOURCE_MCODE) is False


# --------------------------------------------------------------------------
# SRC-05:MCode 失败不冒充成别的模型
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "transport", [TRANSPORT_FINAL_JSON, TRANSPORT_STREAM_JSON]
)
async def test_mcode_failure_does_not_fall_back_to_other_model(
    tmp_path, fake_mcode_stream_cli, transport
):
    """SRC-05:CLI 失败就是失败 —— 不会偷偷换成别的模型出内容。

    两种传输方式都要失败得**明确**,不能变成一段空文本或别家模型的内容。
    """
    cli = fake_mcode_stream_cli(deltas=["甲"], exit_code=7, fail_hard=True)
    model = MCodeChatModel(
        cwd=str(tmp_path),
        mcode_bin=cli.path,
        streaming_transport=transport,
    )

    with pytest.raises(Exception) as exc:
        async for _ in model.astream([HumanMessage(content="干活")]):
            pass

    message = str(exc.value).lower()
    assert "mcode" in message
    # 绝不能是「别的模型的内容」—— 没有第二个模型参与过。
    assert "deepseek" not in message


def test_mcode_failure_produces_no_final_output():
    """SRC-05:没有 ``exec.completed`` 就没有最终正文,不会拿别的内容顶上。"""
    adapter = MCodeEventAdapter(session_id="sess-1", turn_id="turn-1")
    adapter.adapt(_event("exec.started"))
    adapter.adapt(_event("turn.failed", sequence=2, error="CLI 挂了"))

    assert adapter.final_output == ""


# --------------------------------------------------------------------------
# SRC-06:两个会话并发,三类 id 严格隔离
# --------------------------------------------------------------------------

def test_two_sessions_keep_their_ids_separate(tmp_path):
    """SRC-06:两个会话的事件各自带自己的三类 id,不串台。"""
    a = web._make_mcode_adapter(
        web.TURN_SOURCE_MCODE, _settings(tmp_path), session_id="sess-a", turn_id="turn-a"
    )
    b = web._make_mcode_adapter(
        web.TURN_SOURCE_MCODE, _settings(tmp_path), session_id="sess-b", turn_id="turn-b"
    )
    assert a is not None and b is not None

    a_out = a.adapt(_event("item.started", run_id="run-a", session_id="sess-a",
                            turn_id="turn-a", item=_text_item("u1:message", "甲")))
    b_out = b.adapt(_event("item.started", run_id="run-b", session_id="sess-b",
                            turn_id="turn-b", item=_text_item("u1:message", "乙")))

    assert a_out[0].data["session_id"] == "sess-a"
    assert a_out[0].data["turn_id"] == "turn-a"
    assert a_out[0].data["run_id"] == "run-a"
    assert a_out[0].data["delta"] == "甲"

    assert b_out[0].data["session_id"] == "sess-b"
    assert b_out[0].data["turn_id"] == "turn-b"
    assert b_out[0].data["run_id"] == "run-b"
    assert b_out[0].data["delta"] == "乙"


async def test_two_concurrent_turns_do_not_share_sink(tmp_path, fake_mcode_stream_cli):
    """SRC-06:两个 turn 各挂各的 sink,事件不会串到对方队列。"""
    cli_a = fake_mcode_stream_cli(deltas=["甲"], run_id="run-a")
    cli_b = fake_mcode_stream_cli(deltas=["乙"], run_id="run-b")

    got_a: List[str] = []
    got_b: List[str] = []

    async def sink_a(event: Any) -> None:
        if event.text_delta:
            got_a.append(event.text_delta)

    async def sink_b(event: Any) -> None:
        if event.text_delta:
            got_b.append(event.text_delta)

    model_a = MCodeChatModel(
        cwd=str(tmp_path), mcode_bin=cli_a.path,
        streaming_transport=TRANSPORT_STREAM_JSON,
        event_sink_provider=lambda: sink_a,
    )
    model_b = MCodeChatModel(
        cwd=str(tmp_path), mcode_bin=cli_b.path,
        streaming_transport=TRANSPORT_STREAM_JSON,
        event_sink_provider=lambda: sink_b,
    )

    import asyncio

    async def _drain(m: MCodeChatModel, text: str) -> None:
        async for _ in m.astream([HumanMessage(content=text)]):
            pass

    await asyncio.gather(
        _drain(model_a, "a"),
        _drain(model_b, "b"),
    )

    assert got_a == ["甲"]
    assert got_b == ["乙"]


# --------------------------------------------------------------------------
# SRC-07:事件 id 不匹配 -> 丢弃 + 记诊断
# --------------------------------------------------------------------------

def test_event_with_mismatched_ids_is_discarded():
    """SRC-07:标识对不上的事件整条丢弃,后面的正常事件继续处理。"""
    adapter = MCodeEventAdapter(session_id="sess-1", turn_id="turn-1")

    first = adapter.adapt(_event("item.started", item=_text_item("u1:message", "甲")))
    assert len(first) == 1

    # run_id 变了 —— 不是本轮的事件
    bad = adapter.adapt(_event(
        "item.updated", sequence=2, run_id="run-其它",
        item=_text_item("u1:message", "脏数据"),
    ))
    assert bad == [], "标识不匹配的事件必须被丢弃"

    # 后续正常事件不受影响
    good = adapter.adapt(_event(
        "item.updated", sequence=3, item=_text_item("u1:message", "乙")
    ))
    assert len(good) == 1
    assert good[0].data["delta"] == "乙"


def test_event_with_mismatched_turn_id_is_discarded():
    """SRC-07:turn_id 对不上同样丢弃。"""
    adapter = MCodeEventAdapter(session_id="sess-1", turn_id="turn-1")
    adapter.adapt(_event("exec.started"))

    bad = adapter.adapt(_event(
        "item.started", sequence=2, turn_id="turn-其它",
        item=_text_item("u1:message", "脏数据"),
    ))
    assert bad == []


# --------------------------------------------------------------------------
# 适配器细节:工具生命周期由 status 枚举驱动
# --------------------------------------------------------------------------

def test_tool_lifecycle_is_driven_by_status_enum():
    """``tool.start`` 只发一次,``tool.end`` 只发一次,中间都是 progress。"""
    adapter = MCodeEventAdapter(session_id="sess-1", turn_id="turn-1")
    kinds = []
    for status in (
        MCodeToolStatus.PREPARING,
        MCodeToolStatus.PREPARING,
        MCodeToolStatus.PREPARED,
        MCodeToolStatus.START,
        MCodeToolStatus.FINISHED,
        MCodeToolStatus.FINISHED,
    ):
        for internal in adapter.adapt(_event(
            "item.updated", sequence=status.value,
            item=_tool_item("call_1", "read_notes", status,
                            output={"content": [{"type": "text", "text": "ok"}]}),
        )):
            kinds.append(internal.kind)

    assert kinds == [
        KIND_TOOL_START,
        KIND_TOOL_PROGRESS,
        KIND_TOOL_PROGRESS,
        KIND_TOOL_PROGRESS,
        KIND_TOOL_END,
    ]


def test_failed_tool_maps_to_error_end():
    """status=Failed → ``tool.end`` 且 ``is_error=True``。"""
    adapter = MCodeEventAdapter(session_id="sess-1", turn_id="turn-1")
    out = adapter.adapt(_event(
        "item.updated", item=_tool_item(
            "call_9", "boom", MCodeToolStatus.FAILED, error="炸了",
        )
    ))
    assert len(out) == 1
    assert out[0].kind == KIND_TOOL_END
    assert out[0].data["is_error"] is True
    assert "炸了" in out[0].data["summary"]


def test_tool_output_is_summarized_and_truncated():
    """工具输出必须摘要化 + 截断 —— 原文不进浏览器。"""
    adapter = MCodeEventAdapter(
        session_id="sess-1", turn_id="turn-1", max_text_chars=50
    )
    long_text = "很长" * 500
    adapter.adapt(_event(
        "item.updated", item=_tool_item(
            "call_1", "read_notes", MCodeToolStatus.FINISHED,
            output={"content": [{"type": "text", "text": long_text}]},
        )
    ))
    summary = adapter.adapt(_event(
        "item.updated", sequence=2, item=_tool_item(
            "call_2", "read_notes", MCodeToolStatus.FINISHED,
            output={"content": [{"type": "text", "text": long_text}]},
        )
    ))[0].data["summary"]

    assert len(summary) < len(long_text)
    assert "截断" in summary


def test_text_delta_comes_from_both_started_and_updated():
    """首段(item.started)与后续(item.updated)都要出文本。"""
    adapter = MCodeEventAdapter(session_id="sess-1", turn_id="turn-1")
    started = adapter.adapt(_event(
        "item.started", item=_text_item("u1:message", "首段")
    ))
    updated = adapter.adapt(_event(
        "item.updated", sequence=2, item=_text_item("u1:message", "后续")
    ))

    assert started[0].kind == KIND_TEXT
    assert started[0].data["delta"] == "首段"
    assert updated[0].kind == KIND_TEXT
    assert updated[0].data["delta"] == "后续"


def test_final_output_comes_from_exec_completed():
    """最终正文取 ``exec.completed.result.output``。"""
    adapter = MCodeEventAdapter(session_id="sess-1", turn_id="turn-1")
    adapter.adapt(_event("item.started", item=_text_item("u1:message", "过程说明")))
    out = adapter.adapt(_event(
        "exec.completed", sequence=2,
        result={"status": "succeeded", "output": "最终答复"},
    ))

    assert adapter.final_output == "最终答复"
    assert out[-1].kind == KIND_FINAL
    assert out[-1].data["text"] == "最终答复"


def test_turn_failed_maps_to_failed_status():
    """``turn.failed`` → 状态事件(status=failed)。"""
    adapter = MCodeEventAdapter(session_id="sess-1", turn_id="turn-1")
    out = adapter.adapt(_event("turn.failed", error="CLI 挂了"))

    assert out[0].kind == KIND_STATUS
    assert out[0].data["status"] == "failed"
    assert "CLI 挂了" in out[0].data["message"]


def test_trace_event_limit_is_enforced():
    """单轮事件数有上限 —— 高频输出不能打爆内存。"""
    adapter = MCodeEventAdapter(
        session_id="sess-1", turn_id="turn-1", max_trace_events=3
    )
    produced = 0
    for i in range(10):
        produced += len(adapter.adapt(_event(
            "item.updated", sequence=i + 1, item=_text_item("u1:message", "x")
        )))
    assert produced == 3
    assert adapter.truncated is True


# --------------------------------------------------------------------------
# 接线:session 的 sink provider 每轮可换
# --------------------------------------------------------------------------

def test_chat_session_sink_provider_returns_current_sink(tmp_path):
    """session 上的 provider 返回的是**当前** turn 的 sink。"""
    sess = web.ChatSession("sess-x", _settings(tmp_path))
    provider = sess.mcode_sink_provider()

    assert provider() is None

    async def sink(event: Any) -> None:
        return None

    sess._mcode_event_sink = sink
    assert provider() is sink

    sess._mcode_event_sink = None
    assert provider() is None
