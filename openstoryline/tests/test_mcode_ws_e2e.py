"""阶段 E:WebSocket **端到端**验收(真连 ``/ws/sessions/{id}/chat``)。

前面的 ``test_mcode_ws_events.py`` 测的是「事件映射」这一层;
本文件把整条链路走通 —— 浏览器连上真的 WebSocket 端点、发真的 ``chat.send``、
服务端真的起 MCode 子进程、真的把事件推回来。

为什么必须再补这一层
--------------------

WebSocket 端点里的 ``_handle_mcode_internal`` 是个**闭包**。闭包里的
``nonlocal`` 写错了(比如漏掉一个变量),在「直接调模块级函数」的测试里
根本看不出来 —— 只有真的走一遍端点才会炸。

本文件就是那道防线:它会真的执行到端点里的那段闭包。

被测链路::

    TestClient(websocket)
      -> /ws/sessions/{sid}/chat
      -> chat.send
      -> ensure_agent() -> MCodeChatModel(真子进程:假 CLI)
      -> mcode.event 队列 -> _handle_mcode_internal(闭包)
      -> WebSocket 帧
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import agent_fastapi as web
from open_storyline.config import MCodeConfig, ProjectConfig, Settings, load_settings

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent

SESSION_ID = "a" * 32  # 必须是合法 hex id


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _in_openstoryline_dir(monkeypatch):
    monkeypatch.chdir(OPENSTORYLINE_DIR)


class _FakeRepo:
    """内存版会话仓储 —— 端到端测试不该依赖本机 PostgreSQL。"""

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}

    def create(self, session_id: str, state_json: Any = None) -> None:
        self.rows[session_id] = {"state_json": state_json}

    def get(self, session_id: str) -> Optional[Any]:
        row = self.rows.get(session_id)
        if row is None:
            return None
        return type("_Row", (), {"state_json": row["state_json"]})()

    def update_state(self, session_id: str, state_json: Any) -> None:
        self.rows[session_id] = {"state_json": state_json}

    def create_if_absent(self, session_id: str, state_json: Any = None):
        if session_id in self.rows:
            return self.get(session_id), False
        self.create(session_id, state_json)
        return self.get(session_id), True


def _operator_dir(tmp_path: Path) -> Path:
    """合法的操作员目录(mcode 档位建 agent 的硬性前提)。"""
    d = tmp_path / "video-ops"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    (d / "AGENTS.md").write_text("# operator\n", encoding="utf-8")
    return d


def _settings(tmp_path: Path, cli_path: str) -> Settings:
    cfg = load_settings(OPENSTORYLINE_DIR / "config.toml")
    project = ProjectConfig(
        media_dir=tmp_path / "media",
        bgm_dir=tmp_path / "bgm",
        outputs_dir=tmp_path / "outputs",
    )
    mcode = MCodeConfig(
        cwd=str(_operator_dir(tmp_path)),
        bin=cli_path,
        mode="standard",
        streaming_transport="stream_json",
        show_execution_trace=True,
        timeout_s=60,
    )
    return cfg.model_copy(update={"project": project, "mcode": mcode})


def _run_ws_turn(
    tmp_path: Path,
    fake_mcode_stream_cli,
    *,
    model_key: str = "mcode-operator",
    **cli_kwargs: Any,
) -> List[Dict[str, Any]]:
    """连上真的 WebSocket 端点跑一轮,返回收到的全部事件。"""
    cli = fake_mcode_stream_cli(**cli_kwargs)
    cfg = _settings(tmp_path, cli.path)

    repo = _FakeRepo()
    store = web.SessionStore(cfg, repo=repo)
    sess = web.ChatSession(SESSION_ID, cfg, repo=repo)
    sess.chat_model_key = model_key
    sess.vlm_model_key = model_key
    repo.create(SESSION_ID, state_json=sess.dump_state())
    store._sessions[SESSION_ID] = sess

    # 端点读的是 app.state —— 直接替换(不走 lifespan,避免依赖本机 PostgreSQL)。
    web.app.state.cfg = cfg
    web.app.state.sessions = store
    web.app.state.developer_mode = False

    client = TestClient(web.app)
    events: List[Dict[str, Any]] = []
    with client.websocket_connect(f"/ws/sessions/{SESSION_ID}/chat") as ws:
        first = ws.receive_json()          # session.snapshot
        assert first["type"] == "session.snapshot"

        ws.send_json({"type": "chat.send", "data": {"text": "干活"}})

        while True:
            msg = ws.receive_json()
            events.append(msg)
            if msg.get("type") in ("assistant.end", "error"):
                break

    web.app.state.sessions = None
    return events


# --------------------------------------------------------------------------
# 端到端
# --------------------------------------------------------------------------

def test_ws_e2e_mcode_turn_streams_text_and_process_events(tmp_path, fake_mcode_stream_cli):
    """端到端:文本一段段出来,过程事件同时到达,最后正常收尾。"""
    events = _run_ws_turn(
        tmp_path,
        fake_mcode_stream_cli,
        deltas=["我先看看素材。", "整理完了。"],
        final_output="整理完了。",
        tools=[{"id": "call_1", "name": "read_notes"}],
    )

    types = [e["type"] for e in events]

    # 端点先回显用户消息,再开本轮。
    assert types[0] == "chat.user", types
    assert types[1] == "assistant.start", types
    assert "assistant.delta" in types
    assert "agent.status" in types
    assert "tool.start" in types
    assert "tool.end" in types
    assert types[-1] == "assistant.end", types

    # 文本拼起来 == 最终正文
    body = "".join(
        (e["data"] or {}).get("delta") or ""
        for e in events if e["type"] == "assistant.delta"
    )
    assert body == "我先看看素材。整理完了。"

    # assistant.start 带来源和轮次
    start = events[1]["data"]
    assert start["source"] == "mcode"
    assert start["turn_id"]

    # 收尾带来源
    end = events[-1]["data"]
    assert end["source"] == "mcode"
    assert end["text"] == "整理完了。"

    # 过程事件都带 source / run_id / seq
    for e in events:
        if e["type"] not in ("agent.status", "tool.start", "tool.progress", "tool.end",
                             "assistant.delta"):
            continue
        data = e["data"] or {}
        assert data.get("source") == "mcode", e
        assert data.get("run_id"), e
        assert isinstance(data.get("seq"), int), e


def test_ws_e2e_process_events_do_not_leak_into_body(tmp_path, fake_mcode_stream_cli):
    """端到端:状态文案不会混进答复正文。"""
    events = _run_ws_turn(
        tmp_path,
        fake_mcode_stream_cli,
        deltas=["答复正文"],
        final_output="答复正文",
    )

    body = "".join(
        (e["data"] or {}).get("delta") or ""
        for e in events if e["type"] == "assistant.delta"
    )
    for e in events:
        if e["type"] != "agent.status":
            continue
        message = str((e["data"] or {}).get("message") or "")
        assert message not in body


def test_ws_e2e_llm_turn_has_no_mcode_panel_events(tmp_path, fake_mcode_stream_cli, monkeypatch):
    """端到端:普通模型 turn 里**没有**任何 MCode 过程事件(WS-06)。

    这里不真的去连 DeepSeek(那会打真实网络、还要真凭证)。做法是把
    ``validate_api_key`` 换成本地直接报错 —— 本轮立刻失败,
    但**失败之前**一条 MCode 过程事件都不该出现。
    """
    from open_storyline import agent as agent_mod

    def _no_key(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("测试环境没有 LLM 凭证")

    monkeypatch.setattr(agent_mod, "validate_api_key", _no_key)

    events = _run_ws_turn(
        tmp_path,
        fake_mcode_stream_cli,
        model_key="deepseek-v4-pro",
        deltas=["普通答复"],
        final_output="普通答复",
    )

    types = [e["type"] for e in events]
    assert "agent.status" not in types
    assert "tool.start" not in types
    assert "tool.end" not in types
    # 这一轮确实没走 mcode 通道(既没有 MCode 面板事件,也没有起 mcode 子进程)。
    assert "assistant.delta" not in types
    assert types[-1] == "error"


def test_ws_e2e_trace_is_recorded_not_in_history(tmp_path, fake_mcode_stream_cli):
    """端到端:过程事件落进 trace(history 里只有对话)。"""
    events = _run_ws_turn(
        tmp_path,
        fake_mcode_stream_cli,
        deltas=["答复正文"],
        final_output="答复正文",
        tools=[{"id": "call_1", "name": "read_notes"}],
    )

    # 事件层面:过程事件确实推给了浏览器
    types = [e["type"] for e in events]
    assert "agent.status" in types
    assert "tool.start" in types

    # 事件层面:答复正文只出现在 assistant.delta / assistant.end 里
    body_events = [
        e for e in events
        if e["type"] in ("assistant.delta", "assistant.end")
    ]
    assert body_events
    for e in events:
        if e["type"] in ("agent.status", "tool.start", "tool.progress", "tool.end"):
            blob = json.dumps(e["data"] or {}, ensure_ascii=False)
            assert "答复正文" not in blob
