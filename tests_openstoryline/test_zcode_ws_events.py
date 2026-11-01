"""阶段 E:ZCode turn 的 WebSocket 事件时间线测试。

与 ``test_mcode_ws_events.py`` 同构:用假 CLI 跑真子进程,把 sink 接进
``ZCodeEventAdapter`` -> ``_dispatch_mcode_internal``,记录浏览器真正收到的
``{type, data}`` 时间线。差异(设计附录 A.5 的能力边界):

- zcode **没有**文本增量:``assistant.delta`` 不该出现;
- 状态事件只有 running / (心跳) / 终态,**没有工具事件**;
- 最终正文以结果 JSON ``response`` 为权威。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from langchain_core.messages import HumanMessage

import agent_fastapi as web
from open_storyline.config import ProjectConfig, Settings, load_settings
from open_storyline.mcode_event_adapter import STATUS_COMPLETED, STATUS_RUNNING
from open_storyline.zcode_agent_adapter import ZCodeChatModel
from open_storyline.zcode_event_adapter import ZCodeEventAdapter, make_zcode_sink

from conftest import read_record

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent
SESSION_ID = "sess-ws-zcode"
TURN_ID = "turn-zcode-1"


@pytest.fixture(autouse=True)
def _in_openstoryline_dir(monkeypatch):
    """ChatSession 初始化要读相对路径的 prompt 模板(与 mcode ws 测试同款)。"""
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

    def types(self) -> List[str]:
        return [e["type"] for e in self.events]

    def of(self, type_: str) -> List[Dict[str, Any]]:
        return [e["data"] for e in self.events if e["type"] == type_]

    def text(self) -> str:
        return "".join(d.get("delta") or "" for d in self.of("assistant.delta"))


async def _run_zcode_turn(
    tmp_path: Path,
    fake_zcode_cli,
    *,
    trace_enabled: bool = True,
    tick_s: float = 0.0,
    **cli_kwargs: Any,
) -> Dict[str, Any]:
    """跑一整轮 ZCode turn,返回 ``{"timeline", "session", "final_text", "error"}``。"""
    cli = fake_zcode_cli(**cli_kwargs)
    sess = _session(tmp_path)
    cfg = _settings(tmp_path)

    adapter = web._make_zcode_adapter(
        web.TURN_SOURCE_ZCODE, cfg, session_id=SESSION_ID, turn_id=TURN_ID
    )
    assert adapter is not None

    timeline = _Timeline()
    seg: List[str] = []
    final_text_box: List[str] = []

    async def flush_segment(send_flush_event: bool = True) -> None:
        if send_flush_event:
            await timeline.emit("assistant.flush", {
                "source": web.TURN_SOURCE_ZCODE, "turn_id": TURN_ID,
            })
        text = "".join(seg).strip()
        if text:
            web._append_assistant_history(
                sess,
                text,
                turn_source=web.TURN_SOURCE_ZCODE,
                turn_id=TURN_ID,
            )
        seg.clear()

    def append_text(delta: str) -> None:
        seg.append(delta)

    async def dispatch(internal: Any) -> None:
        final = await web._dispatch_mcode_internal(
            internal,
            sess=sess,
            turn_id=TURN_ID,
            trace_enabled=trace_enabled,
            emit=timeline.emit,
            flush_segment=flush_segment,
            append_text=append_text,
        )
        if final is not None:
            final_text_box.append(final)

    sink = make_zcode_sink(adapter, dispatch)

    model = ZCodeChatModel(
        cwd=str(tmp_path),
        zcode_bin=cli.path,
        session_id=SESSION_ID,
        status_tick_s=tick_s,
        event_sink_provider=lambda: sink,
    )

    error: Optional[BaseException] = None
    try:
        async for _chunk in model.astream([HumanMessage(content="干活")]):
            pass
    except Exception as exc:  # noqa: BLE001
        error = exc

    # ---- 收尾:模拟端点的 agent.done ------------------------------------
    # 最终正文以结果 JSON response 为权威(阶段 E)。
    final_text = (final_text_box[-1] if final_text_box else "".join(seg)).strip()
    if final_text:
        web._append_assistant_history(
            sess,
            final_text,
            turn_source=web.TURN_SOURCE_ZCODE,
            turn_id=TURN_ID,
        )
    await timeline.emit("assistant.end", {
        "text": final_text,
        "source": web.TURN_SOURCE_ZCODE,
        "turn_id": TURN_ID,
    })

    return {
        "timeline": timeline,
        "session": sess,
        "final_text": final_text,
        "error": error,
        "cli": cli,
    }


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

async def test_ws_z1_happy_path_timeline(tmp_path, fake_zcode_cli):
    """成功轮:status(running -> completed)+ assistant.end,文本只走 end。"""
    result = await _run_zcode_turn(
        tmp_path, fake_zcode_cli, response="正常"
    )
    tl: _Timeline = result["timeline"]

    assert result["error"] is None
    assert result["final_text"] == "正常"

    # 没有任何文本增量(zcode 无头形态没有增量通道)
    assert "assistant.delta" not in tl.types()
    assert tl.text() == ""

    # 状态事件:running 起步、completed 收尾,source 全部是 zcode
    statuses = tl.of("agent.status")
    assert statuses, "应有状态事件"
    assert statuses[0]["status"] == STATUS_RUNNING
    assert statuses[-1]["status"] == STATUS_COMPLETED
    assert all(d.get("source") == "zcode" for d in statuses)

    # 顺序:第一条状态在 assistant.end 之前
    assert tl.types().index("agent.status") < tl.types().index("assistant.end")
    assert tl.of("assistant.end")[0]["text"] == "正常"
    assert tl.of("assistant.end")[0]["source"] == "zcode"


async def test_ws_z2_no_tool_events(tmp_path, fake_zcode_cli):
    """降级契约:任何情况下都不伪造工具事件(设计 §6.3)。"""
    result = await _run_zcode_turn(tmp_path, fake_zcode_cli, response="ok")
    tl: _Timeline = result["timeline"]
    assert not any(t.startswith("tool.") for t in tl.types())


async def test_ws_z3_trace_recorded_separately(tmp_path, fake_zcode_cli):
    """过程事件单独落 trace(source=zcode),不进 history 正文(WS-07 同款)。"""
    result = await _run_zcode_turn(tmp_path, fake_zcode_cli, response="ok")
    sess = result["session"]

    trace = sess.mcode_trace_snapshot()
    status_rows = [r for r in trace if r.get("type") == "agent.status"]
    assert status_rows, "trace 里应有状态记录"
    assert all(r.get("source") == "zcode" for r in status_rows)
    assert all(r.get("turn_id") == TURN_ID for r in status_rows)

    # history 里只有用户可见正文,不掺过程事件
    body = [h for h in sess.history if h.get("role") == "assistant"]
    assert len(body) == 1
    assert body[0]["content"] == "ok"


async def test_ws_z4_trace_disabled_drops_status_but_keeps_final(
    tmp_path, fake_zcode_cli
):
    """过程开关关闭:agent.status 不发,最终正文照常(与 mcode 同款语义)。"""
    result = await _run_zcode_turn(
        tmp_path, fake_zcode_cli, response="ok", trace_enabled=False
    )
    tl: _Timeline = result["timeline"]
    assert "agent.status" not in tl.types()
    assert result["final_text"] == "ok"


async def test_ws_z5_seq_monotonic(tmp_path, fake_zcode_cli):
    """事件 seq 单调递增且唯一(前端去重门依赖)。"""
    result = await _run_zcode_turn(tmp_path, fake_zcode_cli, response="ok")
    tl: _Timeline = result["timeline"]
    seqs = [d.get("seq") for d in tl.of("agent.status")]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


async def test_ws_z6_error_path_emits_failed_status(tmp_path, fake_zcode_cli):
    """执行失败:failed 终态状态事件先到,模型异常向上传播。"""
    result = await _run_zcode_turn(
        tmp_path, fake_zcode_cli, raw="", exit_code=9
    )
    assert result["error"] is not None
    tl: _Timeline = result["timeline"]
    statuses = tl.of("agent.status")
    assert statuses and statuses[-1]["status"] == "failed"


async def test_ws_z7_cancel_emits_cancelled_status(tmp_path, fake_zcode_cli):
    """用户取消:cancelled 终态状态事件尽力发出,进程树被清理。"""
    cli = fake_zcode_cli(delay_s=60)
    sess = _session(tmp_path)
    cfg = _settings(tmp_path)
    adapter = web._make_zcode_adapter(
        web.TURN_SOURCE_ZCODE, cfg, session_id=SESSION_ID, turn_id=TURN_ID
    )
    timeline = _Timeline()
    final_box: List[str] = []

    async def dispatch(internal: Any) -> None:
        final = await web._dispatch_mcode_internal(
            internal,
            sess=sess,
            turn_id=TURN_ID,
            trace_enabled=True,
            emit=timeline.emit,
            flush_segment=None,  # 取消路径不会走到 flush
            append_text=lambda delta: None,
        )
        if final is not None:
            final_box.append(final)

    sink = make_zcode_sink(adapter, dispatch)
    model = ZCodeChatModel(
        cwd=str(tmp_path),
        zcode_bin=cli.path,
        session_id=SESSION_ID,
        timeout_s=120,
        status_tick_s=0.0,
        event_sink_provider=lambda: sink,
    )

    task = asyncio.ensure_future(model.ainvoke([HumanMessage(content="长任务")]))
    await asyncio.sleep(1.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    statuses = [d for d in timeline.of("agent.status")]
    assert statuses[-1]["status"] == "cancelled"

    from conftest import wait_until_dead
    record = read_record(cli)
    assert record.get("child_pid") or record.get("pid")
    # 直接子进程(node)必然已死;孙进程在有 spawn_child 时也应死(见 chat_model 测试)
    assert wait_until_dead(record["pid"]), "node 进程必须已死"


async def test_ws_z8_tick_events(tmp_path, fake_zcode_cli):
    """长运行 + 心跳开启:能收到「已运行」心跳(面板不卡 Running 空白)。"""
    result = await _run_zcode_turn(
        tmp_path, fake_zcode_cli, delay_s=2.0, response="晚到", tick_s=0.3,
    )
    tl: _Timeline = result["timeline"]
    assert result["final_text"] == "晚到"
    ticks = [d for d in tl.of("agent.status") if "已运行" in str(d.get("message", ""))]
    assert ticks, "应产生心跳状态"
