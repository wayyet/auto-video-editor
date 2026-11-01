"""阶段 E:ZCode 的 WebSocket **端到端**验收(真连 ``/ws/sessions/{id}/chat``)。

与 ``test_mcode_ws_e2e.py`` 同构,走 zcode 分支:浏览器连真的 WebSocket 端点、
发真的 ``chat.send``、服务端真的起 zcode 子进程(假 CLI)、真的把事件推回来。
专门防「端点闭包里的 ``nonlocal`` 写错,单层测试看不出来」的回归。

被测链路::

    TestClient(websocket)
      -> /ws/sessions/{sid}/chat
      -> chat.send(llm_model = "ZCode Agent")
      -> ensure_agent() -> ZCodeChatModel(真子进程:假 CLI)
      -> zcode.event 队列 -> _handle_zcode_internal(闭包)
      -> WebSocket 帧
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import agent_fastapi as web
from open_storyline.config import MCodeConfig, ProjectConfig, Settings, ZCodeConfig, load_settings

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent

SESSION_ID = "b" * 32  # 必须是合法 hex id
ZKEY = web.ZCODE_MODEL_KEY


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


def _settings(tmp_path: Path, cli_path: str) -> Settings:
    cfg = load_settings(OPENSTORYLINE_DIR / "config.toml")
    project = ProjectConfig(
        media_dir=tmp_path / "media",
        bgm_dir=tmp_path / "bgm",
        outputs_dir=tmp_path / "outputs",
    )
    ws = tmp_path / "zcode-ws"
    ws.mkdir(parents=True, exist_ok=True)
    zcode = ZCodeConfig(cwd=str(ws), bin=cli_path, mode="edit", timeout_s=60)
    # 本机 config.toml.local 可能配了 [mcode].cwd —— 显式清空,避免默认档位漂移。
    mcode = MCodeConfig(cwd="", bin="")
    return cfg.model_copy(update={"project": project, "zcode": zcode, "mcode": mcode})


def _run_ws_turn(
    tmp_path: Path,
    fake_zcode_cli,
    *,
    model_key: str = ZKEY,
    **cli_kwargs: Any,
) -> List[Dict[str, Any]]:
    """连上真的 WebSocket 端点跑一轮,返回收到的全部事件。"""
    cli = fake_zcode_cli(**cli_kwargs)
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
        assert ZKEY in (first["data"].get("chat_models") or []), \
            "快照的下拉列表里必须有 ZCode Agent"

        ws.send_json({
            "type": "chat.send",
            "data": {"text": "干活", "llm_model": model_key, "vlm_model": model_key},
        })

        while True:
            msg = ws.receive_json()
            events.append(msg)
            if msg.get("type") in ("assistant.end", "error"):
                break

    web.app.state.sessions = None
    return events, sess


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

def test_e2e_zcode_turn_full_pipeline(tmp_path, fake_zcode_cli):
    events, _sess = _run_ws_turn(tmp_path, fake_zcode_cli, response="端到端正常")
    types = [e["type"] for e in events]

    # 起步事件带 zcode 来源
    start = next(e for e in events if e["type"] == "assistant.start")
    assert start["data"]["source"] == "zcode"

    # 状态事件(source=zcode)出现在 end 之前
    statuses = [e["data"] for e in events if e["type"] == "agent.status"]
    assert statuses and statuses[0]["status"] == "running"
    assert all(d.get("source") == "zcode" for d in statuses)

    # 没有文本增量、没有工具事件(降级契约)
    assert "assistant.delta" not in types
    assert not any(t.startswith("tool.") for t in types)

    # 最终正文 = 结果 JSON response(权威)
    end = next(e for e in events if e["type"] == "assistant.end")
    assert end["data"]["text"] == "端到端正常"
    assert end["data"]["source"] == "zcode"


def test_e2e_zcode_session_id_captured_for_resume(tmp_path, fake_zcode_cli):
    """一轮结束后,结果 sessionId 落进会话状态(dump_state 持久化)。"""
    events, sess = _run_ws_turn(
        tmp_path, fake_zcode_cli,
        response="ok", session_id="sess_e2e_new",
    )
    assert events[-1]["type"] == "assistant.end"

    assert sess.zcode_session_id == "sess_e2e_new"
    assert sess.dump_state()["zcode_session_id"] == "sess_e2e_new"


def test_e2e_zcode_error_path(tmp_path, fake_zcode_cli):
    """CLI 非零退出且无输出:error 帧 + 明确消息(不挂死连接)。"""
    events, _sess = _run_ws_turn(tmp_path, fake_zcode_cli, raw="", exit_code=7)
    types = [e["type"] for e in events]
    assert types[-1] == "error"
    assert "退出码 7" in events[-1]["data"]["message"]
