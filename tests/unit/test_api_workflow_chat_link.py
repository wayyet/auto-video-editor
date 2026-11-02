"""``web.api_workflow`` 会话关联相关端点的单元测试(执行设计 §10.4 任务 2.4/3.5)。

不连库(存储用假类注入;``_snapshot`` / ``_spawn_cli_run`` 打桩):

- POST /runs(阶段 2 门禁,Web 入口透传):
  * 接收 ``chat_session_id`` 并写入 initial state(经 CLI state 文件到 runner);
  * 非法 ID → 400;
  * 不传 → 行为不变(旧调用兼容);
- GET /chat-sessions/{id}/run(阶段 3 门禁,四类):
  * 有记录 → 200 扁平结构;无记录 → 200 ``run: null``;
  * 非法 ID → 400;DB 不可用 → 503(不冒充"无关联");
  * 跨源 Origin → 403(沿用 §6.2-E 同源校验)。
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest
from fastapi import HTTPException, Request

from web import api_workflow

_CSID = "0123456789abcdef0123456789abcdef"
_LINK = {
    "chat_session_id": _CSID,
    "thread_id": "video-web-001",
    "run_id": "run-abc123",
    "status": "running",
    "updated_at": "2026-11-03T02:30:00Z",
}


# ---------------------------------------------------------------------------
# 工具:直接构造 Starlette Request(不启 HTTP 服务)
# ---------------------------------------------------------------------------
def _make_request(
    body: Optional[dict] = None, origin: Optional[str] = None
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if origin:
        headers.append((b"origin", origin.encode("ascii")))
    payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST" if body is not None else "GET",
        "path": "/api/workflow/runs" if body is not None else "/api/workflow/x",
        "headers": headers,
        "query_string": b"",
    }
    return Request(scope, receive)


def _fake_store_factory(link: Optional[dict], fail: bool = False):
    class _FakeStore:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def get_by_chat_session(self, csid: str) -> Optional[dict]:
            if fail:
                raise RuntimeError("psycopg: connection refused")
            return link

    return _FakeStore


@pytest.fixture
def _no_spawn(monkeypatch: pytest.MonkeyPatch):
    """打桩 POST /runs 的子进程拉起与快照查询;返回捕获容器。"""
    captured: dict[str, Any] = {}

    def _fake_spawn(thread_id: str, initial_state: Optional[dict], *, preflight: bool):
        captured["thread_id"] = thread_id
        captured["initial_state"] = initial_state
        captured["preflight"] = preflight
        return {"pid": 4321, "log": "x.log", "python": "python", "preflight": preflight}

    monkeypatch.setattr(api_workflow, "_spawn_cli_run", _fake_spawn)
    monkeypatch.setattr(
        api_workflow,
        "_snapshot",
        lambda tid: {"thread_id": tid, "available": False, "event_count": 0,
                     "state": "idle"},
    )
    return captured


# ---------------------------------------------------------------------------
# POST /runs:Web 入口透传(阶段 2)
# ---------------------------------------------------------------------------
async def test_post_runs_accepts_chat_session_id(_no_spawn) -> None:
    resp = await api_workflow.start_run(
        _make_request({"thread_id": "video-web-001", "chat_session_id": _CSID})
    )
    assert resp.status_code == 202
    assert _no_spawn["initial_state"]["chat_session_id"] == _CSID
    # 既有 session_id 语义不受影响
    body = {"thread_id": "video-web-002", "session_id": "biz-session",
            "chat_session_id": _CSID}
    await api_workflow.start_run(_make_request(body))
    assert _no_spawn["initial_state"]["session_id"] == "biz-session"


async def test_post_runs_rejects_invalid_chat_session_id(_no_spawn) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await api_workflow.start_run(
            _make_request({"thread_id": "video-web-001", "chat_session_id": "nope"})
        )
    assert excinfo.value.status_code == 400
    assert "initial_state" not in _no_spawn, "校验失败不得触发子进程拉起"


async def test_post_runs_without_chat_session_id_unchanged(_no_spawn) -> None:
    resp = await api_workflow.start_run(
        _make_request({"thread_id": "video-web-001", "session_id": "biz"})
    )
    assert resp.status_code == 202
    assert _no_spawn["initial_state"] is not None
    assert "chat_session_id" not in _no_spawn["initial_state"]
    assert _no_spawn["initial_state"]["session_id"] == "biz"


# ---------------------------------------------------------------------------
# GET /chat-sessions/{id}/run:四类场景(阶段 3)
# ---------------------------------------------------------------------------
async def test_get_found_returns_flattened(monkeypatch: pytest.MonkeyPatch) -> None:
    import monitoring.workflow_chat_link_store as store_module

    monkeypatch.setattr(store_module, "WorkflowChatLinkStore", _fake_store_factory(_LINK))
    resp = await api_workflow.chat_session_run(_make_request(), _CSID)
    assert resp.status_code == 200
    data = json.loads(bytes(resp.body))
    assert data["available"] is True
    assert data["thread_id"] == "video-web-001"
    assert data["chat_session_id"] == _CSID
    assert data["status"] == "running"


async def test_get_no_record_returns_run_null(monkeypatch: pytest.MonkeyPatch) -> None:
    import monitoring.workflow_chat_link_store as store_module

    monkeypatch.setattr(store_module, "WorkflowChatLinkStore", _fake_store_factory(None))
    resp = await api_workflow.chat_session_run(_make_request(), _CSID)
    assert resp.status_code == 200
    assert json.loads(bytes(resp.body)) == {"available": True, "run": None}


async def test_get_invalid_id_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    import monitoring.workflow_chat_link_store as store_module

    monkeypatch.setattr(store_module, "WorkflowChatLinkStore", _fake_store_factory(_LINK))
    with pytest.raises(HTTPException) as excinfo:
        await api_workflow.chat_session_run(_make_request(), "not-a-session-id")
    assert excinfo.value.status_code == 400


async def test_get_db_unavailable_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    import monitoring.workflow_chat_link_store as store_module

    monkeypatch.setattr(
        store_module, "WorkflowChatLinkStore", _fake_store_factory(None, fail=True)
    )
    with pytest.raises(HTTPException) as excinfo:
        await api_workflow.chat_session_run(_make_request(), _CSID)
    assert excinfo.value.status_code == 503


async def test_get_cross_origin_rejected_403() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await api_workflow.chat_session_run(
            _make_request(origin="http://evil.example"), _CSID
        )
    assert excinfo.value.status_code == 403
