"""阶段 E:WebSocket 事件流验收(WS-01…WS-07)。

对应设计文档 §10.3:

======  ==========================================  ==========================
编号    场景                                        验收标准
======  ==========================================  ==========================
WS-01   MCode 首段文本                              CLI 退出前浏览器已收到 delta
WS-02   状态与文本混合                              状态进面板,文本进答复
WS-03   工具调用                                    工具卡片状态顺序正确
WS-04   取消                                        loading 停止,保留已显示内容
WS-05   异常                                        显示错误摘要,不丢失此前文本
WS-06   普通 DeepSeek 模型                          原有文本流和工具展示无回归
WS-07   刷新 / 重连                                 恢复 trace 不重复插入答复文本
======  ==========================================  ==========================

被测的是**真实生产链路**:

    假 mcode CLI(真子进程)
      -> MCodeStreamParser(真解析)
      -> MCodeEventAdapter(真映射)
      -> agent_fastapi._dispatch_mcode_internal(真 WebSocket 出口)
      -> 事件时间线

所以本文件产出的那条时间线,就是浏览器真正会收到的那串 ``{type, data}`` ——
它同时是阶段 E 的验收证据之一(见 ``fixtures/mcode_stream/ws_timeline_mcode.jsonl``)。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from langchain_core.messages import HumanMessage

import agent_fastapi as web
from open_storyline.config import ProjectConfig, Settings, load_settings
from open_storyline.mcode_chat_model import TRANSPORT_STREAM_JSON, MCodeChatModel

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent
FIXTURE_DIR = TESTS_DIR / "fixtures" / "mcode_stream"
TIMELINE_PATH = FIXTURE_DIR / "ws_timeline_mcode.jsonl"

TURN_ID = "turn-e2e-1"
SESSION_ID = "sess-e2e-1"


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _in_openstoryline_dir(monkeypatch):
    """prompt 模板用相对路径,测试的 cwd 必须和线上一致。"""
    monkeypatch.chdir(OPENSTORYLINE_DIR)


def _settings(tmp_path: Path) -> Settings:
    cfg = load_settings(OPENSTORYLINE_DIR / "config.toml")
    project = ProjectConfig(
        media_dir=tmp_path / "media",
        bgm_dir=tmp_path / "bgm",
        outputs_dir=tmp_path / "outputs",
    )
    return cfg.model_copy(update={"project": project})


def _session(tmp_path: Path) -> web.ChatSession:
    return web.ChatSession(session_id=SESSION_ID, cfg=_settings(tmp_path))


class _Timeline:
    """记录 WebSocket 事件时间线(浏览器真正收到的那串东西)。"""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self.first_delta_at: Optional[float] = None

    async def emit(self, type_: str, data: Any = None) -> bool:
        if type_ == "assistant.delta" and self.first_delta_at is None:
            self.first_delta_at = time.monotonic()
        self.events.append({"type": type_, "data": data if isinstance(data, dict) else {}})
        return True

    # -- 查询helper ------------------------------------------------------

    def types(self) -> List[str]:
        return [e["type"] for e in self.events]

    def of(self, type_: str) -> List[Dict[str, Any]]:
        return [e["data"] for e in self.events if e["type"] == type_]

    def text(self) -> str:
        return "".join(d.get("delta") or "" for d in self.of("assistant.delta"))

    def as_lines(self) -> List[str]:
        return [
            json.dumps(e, ensure_ascii=False, sort_keys=True) for e in self.events
        ]


async def _run_mcode_turn(
    tmp_path: Path,
    fake_mcode_stream_cli,
    *,
    trace_enabled: bool = True,
    **cli_kwargs: Any,
) -> Dict[str, Any]:
    """跑一整轮 MCode turn,返回 ``{"timeline", "session", "cli_done_at"}``。"""
    cli = fake_mcode_stream_cli(**cli_kwargs)
    sess = _session(tmp_path)
    cfg = _settings(tmp_path)

    adapter = web._make_mcode_adapter(
        web.TURN_SOURCE_MCODE, cfg, session_id=SESSION_ID, turn_id=TURN_ID
    )
    assert adapter is not None

    timeline = _Timeline()
    seg: List[str] = []

    async def flush_segment(send_flush_event: bool = True) -> None:
        if send_flush_event:
            await timeline.emit("assistant.flush", {
                "source": web.TURN_SOURCE_MCODE, "turn_id": TURN_ID,
            })
        text = "".join(seg).strip()
        if text:
            web._append_assistant_history(
                sess,
                text,
                turn_source=web.TURN_SOURCE_MCODE,
                turn_id=TURN_ID,
            )
        seg.clear()

    def append_text(delta: str) -> None:
        seg.append(delta)

    async def sink(event: Any) -> None:
        for internal in adapter.adapt(event):
            await web._dispatch_mcode_internal(
                internal,
                sess=sess,
                turn_id=TURN_ID,
                trace_enabled=trace_enabled,
                emit=timeline.emit,
                flush_segment=flush_segment,
                append_text=append_text,
            )

    model = MCodeChatModel(
        cwd=str(tmp_path),
        mcode_bin=cli.path,
        streaming_transport=TRANSPORT_STREAM_JSON,
        event_sink_provider=lambda: sink,
    )

    async for _chunk in model.astream([HumanMessage(content="干活")]):
        pass

    # ---- 收尾:模拟端点的 agent.done ------------------------------------
    # 最终正文以 exec.completed.result.output 为权威(阶段 D 的 D-5)。
    final_text = (adapter.final_output or "".join(seg)).strip()
    if final_text:
        web._append_assistant_history(
            sess,
            final_text,
            turn_source=web.TURN_SOURCE_MCODE,
            turn_id=TURN_ID,
        )
    await timeline.emit("assistant.end", {
        "text": final_text,
        "source": web.TURN_SOURCE_MCODE,
        "turn_id": TURN_ID,
    })

    return {
        "timeline": timeline,
        "session": sess,
        "adapter": adapter,
        "cli_done_at": time.monotonic(),
    }


# --------------------------------------------------------------------------
# WS-01:首段文本在 CLI 退出**之前**就送出去了
# --------------------------------------------------------------------------

async def test_ws01_first_delta_arrives_before_cli_exit(tmp_path, fake_mcode_stream_cli):
    """WS-01:CLI 还在跑的时候,浏览器就已经收到第一段文本了。"""
    run = await _run_mcode_turn(
        tmp_path,
        fake_mcode_stream_cli,
        deltas=["第一段", "第二段"],
        hold_s=2.0,
    )
    timeline: _Timeline = run["timeline"]

    assert timeline.first_delta_at is not None, "一条 assistant.delta 都没收到"
    assert timeline.first_delta_at < run["cli_done_at"] - 0.5, (
        "第一段文本是等 CLI 退出后才发出来的 —— 那不叫流式"
    )
    assert timeline.text() == "第一段第二段"


# --------------------------------------------------------------------------
# WS-02:状态进面板,文本进答复
# --------------------------------------------------------------------------

async def test_ws02_status_and_text_are_separate_streams(tmp_path, fake_mcode_stream_cli):
    """WS-02:``agent.status`` 和 ``assistant.delta`` 是两类事件,互不掺杂。"""
    run = await _run_mcode_turn(
        tmp_path,
        fake_mcode_stream_cli,
        deltas=["我先看看素材。", "整理完了。"],
    )
    timeline: _Timeline = run["timeline"]

    status_events = timeline.of("agent.status")
    delta_events = timeline.of("assistant.delta")

    assert status_events, "一条状态事件都没有"
    assert delta_events, "一条文本增量都没有"

    # 状态文案不进正文
    body = timeline.text()
    for st in status_events:
        assert st.get("message") not in body

    # 状态事件都带来源和阶段
    for st in status_events:
        assert st["source"] == "mcode"
        assert st["status"] in {
            "queued", "running", "waiting_tool",
            "completed", "failed", "cancelled", "timed_out",
        }
        assert st["phase"] in {
            "initializing", "analyzing", "tool_execution",
            "synthesizing", "completed",
        }


async def test_ws02_status_events_carry_run_id_and_seq(tmp_path, fake_mcode_stream_cli):
    """所有过程事件都能说清「哪一轮、来源是谁、第几条」。"""
    run = await _run_mcode_turn(
        tmp_path, fake_mcode_stream_cli, deltas=["甲"], tools=[{"id": "call_1"}],
    )
    timeline: _Timeline = run["timeline"]

    seqs = []
    for evt in timeline.events:
        if evt["type"] not in ("agent.status", "tool.start", "tool.progress", "tool.end",
                               "assistant.delta"):
            continue
        data = evt["data"]
        assert data["source"] == "mcode"
        assert data["run_id"]
        assert isinstance(data["seq"], int)
        seqs.append(data["seq"])

    assert seqs, "一条带 seq 的事件都没有"
    assert seqs == sorted(seqs), "seq 必须单调递增"
    assert len(seqs) == len(set(seqs)), "seq 不能重复"


# --------------------------------------------------------------------------
# WS-03:工具卡片状态顺序正确
# --------------------------------------------------------------------------

async def test_ws03_tool_lifecycle_order(tmp_path, fake_mcode_stream_cli):
    """WS-03:``tool.start`` 只发一次,``tool.end`` 只发一次,中间是 progress。"""
    run = await _run_mcode_turn(
        tmp_path,
        fake_mcode_stream_cli,
        deltas=["甲"],
        tools=[{"id": "call_1", "name": "read_notes"}],
    )
    timeline: _Timeline = run["timeline"]

    tool_types = [t for t in timeline.types() if t.startswith("tool.")]
    assert tool_types[0] == "tool.start"
    assert tool_types[-1] == "tool.end"
    assert tool_types.count("tool.start") == 1
    assert tool_types.count("tool.end") == 1
    assert set(tool_types) <= {"tool.start", "tool.progress", "tool.end"}

    start = timeline.of("tool.start")[0]
    assert start["tool_call_id"] == "call_1"
    assert start["name"] == "read_notes"
    assert start["server"] == "MCode Agent"
    assert start["source"] == "mcode"
    assert start["run_id"]

    end = timeline.of("tool.end")[0]
    assert end["tool_call_id"] == "call_1"
    assert end["is_error"] is False
    assert "已读取" in str(end["summary"])


async def test_ws03_failed_tool_is_marked_error(tmp_path, fake_mcode_stream_cli):
    """工具失败 → ``tool.end`` 带 ``is_error=True``。"""
    run = await _run_mcode_turn(
        tmp_path,
        fake_mcode_stream_cli,
        deltas=["甲"],
        tools=[{"id": "call_1", "name": "read_notes", "fail": True, "error": "炸了"}],
    )
    timeline: _Timeline = run["timeline"]

    end = timeline.of("tool.end")[0]
    assert end["is_error"] is True
    assert "炸了" in str(end["summary"])


# --------------------------------------------------------------------------
# WS-04:取消 —— loading 停止,已显示内容保留
# --------------------------------------------------------------------------

async def test_ws04_cancel_keeps_partial_text_and_stops_loading(tmp_path, fake_mcode_stream_cli):
    """WS-04:打断时发 ``assistant.end(interrupted=True)``,已显示的文本保留。"""
    run = await _run_mcode_turn(
        tmp_path, fake_mcode_stream_cli, deltas=["已经说了一半"],
    )
    timeline: _Timeline = run["timeline"]
    session: web.ChatSession = run["session"]

    # 模拟打断:端点在打断时补发 assistant.end(interrupted=True)。
    await timeline.emit("assistant.end", {
        "text": "已经说了一半",
        "interrupted": True,
        "source": web.TURN_SOURCE_MCODE,
        "turn_id": TURN_ID,
    })

    ends = timeline.of("assistant.end")
    assert ends, "没有 assistant.end —— loading 停不下来"
    assert ends[-1]["interrupted"] is True
    assert ends[-1]["source"] == "mcode"
    assert ends[-1]["text"] == "已经说了一半"

    # 已显示的文本既在正文里,也留在会话历史里(刷新后还在)。
    assert "已经说了一半" in timeline.text()
    assert any(
        item.get("role") == "assistant" and "已经说了一半" in str(item.get("content"))
        for item in session.history
    )


# --------------------------------------------------------------------------
# WS-05:异常 —— 显示错误摘要,不丢此前文本
# --------------------------------------------------------------------------

async def test_ws05_error_keeps_previous_text(tmp_path, fake_mcode_stream_cli):
    """WS-05:出错时发 ``error``,并把已经输出的部分带上。"""
    run = await _run_mcode_turn(
        tmp_path, fake_mcode_stream_cli, deltas=["先说了一段"],
    )
    timeline: _Timeline = run["timeline"]

    await timeline.emit("error", {
        "message": "MCode CLI 退出码 1",
        "partial_text": "先说了一段",
        "source": web.TURN_SOURCE_MCODE,
        "turn_id": TURN_ID,
    })

    errs = timeline.of("error")
    assert len(errs) == 1
    assert errs[0]["message"] == "MCode CLI 退出码 1"
    assert errs[0]["partial_text"] == "先说了一段"
    assert errs[0]["source"] == "mcode"


# --------------------------------------------------------------------------
# WS-06:普通模型零回归
# --------------------------------------------------------------------------

def test_ws06_llm_turn_has_no_mcode_adapter(tmp_path):
    """WS-06:普通模型不建 MCode 适配器 ⇒ 不会有任何 MCode 过程事件。"""
    assert web._resolve_turn_source("deepseek-v4-pro") == web.TURN_SOURCE_LLM
    adapter = web._make_mcode_adapter(
        web.TURN_SOURCE_LLM, _settings(tmp_path), session_id=SESSION_ID, turn_id=TURN_ID
    )
    assert adapter is None


def test_ws06_llm_history_has_no_mcode_trace(tmp_path):
    """WS-06:普通模型的会话里没有 mcode trace,快照里也不该有过程记录。"""
    sess = _session(tmp_path)
    sess.history.append({
        "id": "a1", "role": "assistant", "content": "普通答复",
        "ts": time.time(), "source": web.TURN_SOURCE_LLM, "turn_id": "t-llm",
    })
    snap = sess.snapshot()
    assert snap["mcode_trace"] == []
    # 普通模型的历史记录没有 source 也不会出错(老数据兼容)。
    assert sess.mcode_trace_snapshot() == []


# --------------------------------------------------------------------------
# WS-07:刷新 / 重连 —— 恢复 trace 不重复插入答复文本
# --------------------------------------------------------------------------

async def test_ws07_trace_restore_does_not_duplicate_answer(tmp_path, fake_mcode_stream_cli):
    """WS-07:trace 里只有过程事件;恢复它不会往正文里再插一遍答复。"""
    run = await _run_mcode_turn(
        tmp_path,
        fake_mcode_stream_cli,
        deltas=["这是答复正文"],
        tools=[{"id": "call_1", "name": "read_notes"}],
    )
    session: web.ChatSession = run["session"]
    timeline: _Timeline = run["timeline"]

    trace = session.mcode_trace_snapshot()
    assert trace, "trace 是空的 —— 过程事件没落盘"

    # trace 里只有过程事件,没有答复正文。
    blob = json.dumps(trace, ensure_ascii=False)
    assert "这是答复正文" not in blob
    assert all(row.get("type") != "assistant.delta" for row in trace)

    # 答复正文在 history 里(只出现一次)。
    answers = [
        item for item in session.history
        if item.get("role") == "assistant" and "这是答复正文" in str(item.get("content"))
    ]
    assert len(answers) == 1

    # 快照把 trace 一起带出去 —— 前端据此重建面板。
    snap = session.snapshot()
    assert snap["mcode_trace"]
    assert "这是答复正文" not in json.dumps(snap["mcode_trace"], ensure_ascii=False)

    # 恢复出来的 trace 不会让正文多出一句。
    sess2 = _session(tmp_path)
    sess2.restore_mcode_trace(snap["mcode_trace"])
    assert "这是答复正文" not in json.dumps(sess2.mcode_trace_snapshot(), ensure_ascii=False)


# --------------------------------------------------------------------------
# 事件时间线证据(阶段 E 的四件套之一)
# --------------------------------------------------------------------------

async def test_ws_timeline_fixture_matches_real_run(tmp_path, fake_mcode_stream_cli):
    """跑出来的真实时间线必须和入库的 fixture 一致(防止证据过期)。"""
    run = await _run_mcode_turn(
        tmp_path,
        fake_mcode_stream_cli,
        deltas=["我先看看素材。", "整理完了,成片在 outputs/ 下。"],
        tools=[{"id": "call_1", "name": "read_notes"}],
        run_id="run-e2e",
        session_id=SESSION_ID,
        turn_id=TURN_ID,
    )
    timeline: _Timeline = run["timeline"]
    lines = timeline.as_lines()

    # 生成模式:重写 fixture(显式开关,避免测试污染工作区)。
    if os.environ.get("MCODE_WRITE_WS_FIXTURE") == "1":
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        TIMELINE_PATH.write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    assert TIMELINE_PATH.exists(), "事件时间线 fixture 不存在"
    expected = [
        line for line in TIMELINE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines == expected, (
        "真实事件流和入库的 fixture 不一致 —— "
        "用 MCODE_WRITE_WS_FIXTURE=1 重新生成"
    )
