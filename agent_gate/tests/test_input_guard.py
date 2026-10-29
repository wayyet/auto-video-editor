"""F2(素材/初始 state 白名单)+ F6(生产护栏、preflight 开关)。

F2 的核心:``run_start`` **不再接受** initial_state_file 参数;``video_path`` 必须落在
``AGENT_GATE_MEDIA_ROOTS`` 里;拒绝时写本机事件,但**返回给 Agent 的文案不含本机绝对路径**。
"""
from __future__ import annotations

import json
import os

import pytest

from agent_gate import runner
from agent_gate.mailbox import Mailbox
from agent_gate.safety import UnsafePath, media_roots, safe_media_path, safe_state_file

GRAPH = "toy_graph:build_graph"


@pytest.fixture()
def media(tmp_path):
    d = tmp_path / "inputs"
    d.mkdir()
    (d / "30s.mp4").write_bytes(b"\x00" * 16)
    return d


# ------------------------------------------------------------------ safe_media_path
def test_media_path_inside_root_ok(media):
    assert safe_media_path(str(media / "30s.mp4"), roots=[media]) == (media / "30s.mp4").resolve()


@pytest.mark.parametrize("bad", [
    "",                       # 空
    "../x.mp4",
    "..\\x.mp4",
    "/etc/passwd",
    "C:\\Windows\\System32\\config\\SAM",
    "%2e%2e/%2e%2e/etc/passwd",
])
def test_media_path_outside_root_rejected(media, bad):
    with pytest.raises(UnsafePath):
        safe_media_path(bad, roots=[media])


def test_media_path_traversal_from_inside_root_rejected(media):
    """根目录内部的 .. 也会被 resolve 掉 → 落到根外 → 拒。"""
    with pytest.raises(UnsafePath):
        safe_media_path(str(media / ".." / ".." / "secret.mp4"), roots=[media])


def test_media_roots_unset_is_fail_closed(media):
    with pytest.raises(UnsafePath) as e:
        safe_media_path(str(media / "30s.mp4"), roots=[])
    assert str(e.value) == "media_roots_unset"


def test_media_roots_reads_env(media, monkeypatch):
    monkeypatch.setenv("AGENT_GATE_MEDIA_ROOTS", str(media))
    assert media_roots() == [media.resolve()]
    assert safe_media_path(str(media / "30s.mp4")) == (media / "30s.mp4").resolve()


def test_media_roots_multi_and_missing_entries(media, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_GATE_MEDIA_ROOTS", f"{media}{os.pathsep}{tmp_path / 'nope'}"
                                                  f"{os.pathsep}")
    assert len(media_roots()) == 2
    with pytest.raises(UnsafePath) as e:
        safe_media_path(str(tmp_path / "nope" / "x.mp4"))
    assert str(e.value) == "media_not_found"


def test_safe_state_file_whitelist(media, tmp_path):
    f = media / "init.json"
    f.write_text("{}", encoding="utf-8")
    assert safe_state_file(f, roots=[media]) == f.resolve()
    outside = tmp_path / "init.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(UnsafePath):
        safe_state_file(outside, roots=[media])


# ------------------------------------------------------------------ MCP run_start 签名
def test_run_start_tool_schema_excludes_initial_state_file(tmp_path):
    """真连一次 MCP,列出工具入参,确认 initial_state_file 已被移除。"""
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=os.sys.executable,
        args=["-m", "agent_gate.mcp_server", "--outputs-root", str(tmp_path / "o"),
              "--graph-factory", GRAPH],
        env=dict(os.environ, TOY_CKPT_DIR=str(tmp_path / "c"), PYTHONUTF8="1"),
    )

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                out = {}
                for t in (await s.list_tools()).tools:
                    schema = t.inputSchema
                    if isinstance(schema, str):          # 这个 mcp 版本把 schema 序列化成字符串
                        schema = json.loads(schema)
                    out[t.name] = sorted(schema.get("properties") or {})
                return out

    props = asyncio.run(go())
    assert "initial_state_file" not in props["run_start"]
    assert "video_path" in props["run_start"]
    assert "continue_after_crash" in props["run_start"]


def test_mcp_run_start_rejects_media_outside_whitelist(tmp_path, media):
    """白名单外的 video_path 被拒,且返回文案不含本机绝对路径。"""
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"\x00" * 16)
    params = StdioServerParameters(
        command=os.sys.executable,
        args=["-m", "agent_gate.mcp_server", "--outputs-root", str(tmp_path / "o"),
              "--graph-factory", GRAPH],
        env=dict(os.environ, TOY_CKPT_DIR=str(tmp_path / "c"), PYTHONUTF8="1",
                 AGENT_GATE_MEDIA_ROOTS=str(media)),
    )

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("run_start", {"thread_id": "job1",
                                                     "video_path": str(outside)})
                return res.structuredContent

    body = asyncio.run(go())
    assert body["ok"] is False
    assert body["code"] == "media_path_not_allowed"
    blob = json.dumps(body, ensure_ascii=False)
    assert str(outside) not in blob and str(tmp_path) not in blob

    # 本机事件日志里有据可查(事件在 gate_root 下,不在对外文案里)
    mb = Mailbox(runner.gate_root(tmp_path / "o", "job1"))
    assert any(e["event"] == "input_rejected" for e in mb.read_events(20))


def test_mcp_run_start_rejects_when_whitelist_unset(tmp_path, media):
    """fail closed:没配白名单就一个素材都不接受。"""
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=os.sys.executable,
        args=["-m", "agent_gate.mcp_server", "--outputs-root", str(tmp_path / "o"),
              "--graph-factory", GRAPH],
        env={k: v for k, v in os.environ.items() if k != "AGENT_GATE_MEDIA_ROOTS"}
        | {"TOY_CKPT_DIR": str(tmp_path / "c"), "PYTHONUTF8": "1"},
    )

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("run_start", {"thread_id": "job1",
                                                     "video_path": str(media / "30s.mp4")})
                return res.structuredContent

    assert asyncio.run(go())["code"] == "media_roots_unset"


def test_mcp_tools_reject_bad_thread_id(tmp_path):
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=os.sys.executable,
        args=["-m", "agent_gate.mcp_server", "--outputs-root", str(tmp_path / "o"),
              "--graph-factory", GRAPH],
        env=dict(os.environ, TOY_CKPT_DIR=str(tmp_path / "c"), PYTHONUTF8="1"),
    )

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                a = await s.call_tool("run_status", {"thread_id": "../evil"})
                b = await s.call_tool("gate_submit_result", {"thread_id": "../evil",
                                                             "task_id": "t_" + "0" * 16,
                                                             "answer": "{}"})
                return a.structuredContent, b.structuredContent

    a, b = asyncio.run(go())
    assert a["code"] == "bad_thread_id" and b["code"] == "bad_thread_id"


# ------------------------------------------------------------------ CLI 侧
def test_cli_start_rejects_video_outside_whitelist(tmp_path, media, capsys, monkeypatch):
    from agent_gate import cli

    monkeypatch.setenv("AGENT_GATE_MEDIA_ROOTS", str(media))
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"\x00" * 16)
    code = cli.main(["start", "--thread-id", "job1", "--outputs-root", str(tmp_path / "o"),
                     "--graph-factory", GRAPH, "--backend", "stub", "--video", str(outside)])
    assert code == 1
    body = json.loads(capsys.readouterr().out)
    assert body["code"] == "media_path_not_allowed"
    assert str(outside) not in json.dumps(body, ensure_ascii=False)


def test_cli_status_rejects_bad_thread_id(tmp_path, capsys):
    from agent_gate import cli

    assert cli.main(["status", "--thread-id", "../evil", "--outputs-root", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "bad_thread_id"


def test_cli_worker_passes_require_preflight(tmp_path, monkeypatch):
    """--require-preflight 透传到 run_worker。"""
    from agent_gate import cli

    seen = {}
    monkeypatch.setattr(runner, "run_worker", lambda **kw: seen.update(kw) or 0)
    assert cli.main(["worker", "--thread-id", "job1", "--outputs-root", str(tmp_path),
                     "--require-preflight"]) == 0
    assert seen["require_preflight"] is True


# ------------------------------------------------------------------ F6 生产护栏
def test_no_blocker_on_production(monkeypatch):
    """PostgreSQL 统一改造后,production **不再**被拦下(方案 §4.7)。

    原来的护栏前提是"本方案只验收 SQLite 本地开发路径";SQLite 后端已删除,
    Postgres 是唯一后端,继续拦着等于让 agent 链路永远起不来(方案 §3.2 A1)。
    """
    monkeypatch.setenv("WORKFLOW_ENV", "production")
    monkeypatch.delenv("CHECKPOINTER_BACKEND", raising=False)
    monkeypatch.setenv("AGENT_GATE_CKPT_BACKEND", "postgres")
    assert runner.production_blocker() is None


def test_blocker_on_stale_sqlite_backend_env(monkeypatch):
    """残留的旧 ``CHECKPOINTER_BACKEND=sqlite`` → 明确拒绝(正向校验:必须是 Postgres)。"""
    monkeypatch.delenv("WORKFLOW_ENV", raising=False)
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "sqlite")
    reason = runner.production_blocker()
    assert reason is not None
    assert "CHECKPOINTER_BACKEND" in reason and "sqlite" in reason


def test_no_blocker_on_stale_postgres_backend_env(monkeypatch):
    """``CHECKPOINTER_BACKEND=postgres`` 是合法值,不拦。"""
    monkeypatch.delenv("WORKFLOW_ENV", raising=False)
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    assert runner.production_blocker() is None


def test_blocker_on_invalid_agent_gate_backend(monkeypatch):
    """``AGENT_GATE_CKPT_BACKEND`` 取值非法 → 明确拒绝。"""
    monkeypatch.delenv("CHECKPOINTER_BACKEND", raising=False)
    monkeypatch.setenv("AGENT_GATE_CKPT_BACKEND", "redis")
    reason = runner.production_blocker()
    assert reason is not None
    assert "AGENT_GATE_CKPT_BACKEND" in reason


def test_run_worker_exits_3_on_stale_sqlite_backend(tmp_path, monkeypatch):
    """命中护栏 → 退出码 3 + STATE_FAILED + production_unsupported 事件,不静默退回本地文件。"""
    monkeypatch.setenv("WORKFLOW_ENV", "production")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "sqlite")
    code = runner.run_worker(thread_id="job1", outputs_root=tmp_path, graph_factory=GRAPH,
                             initial_state={"session_id": "job1"}, backend="stub")
    assert code == 3
    mb = Mailbox(runner.gate_root(tmp_path, "job1"))
    st = mb.read_status()
    assert st["state"] == "failed"
    assert st["error"]["type"] == "ProductionUnsupported"
    assert any(e["event"] == "production_unsupported" for e in mb.read_events(20))


def test_run_worker_exits_0_on_postgres_backend(tmp_path, monkeypatch):
    """对照组:Postgres 后端照常能跑(用会失败的 graph 证明它确实往下走了)。"""
    monkeypatch.delenv("WORKFLOW_ENV", raising=False)
    monkeypatch.delenv("CHECKPOINTER_BACKEND", raising=False)
    monkeypatch.setenv("AGENT_GATE_CKPT_BACKEND", "memory")
    code = runner.run_worker(thread_id="job1", outputs_root=tmp_path,
                             graph_factory="no_such_module:build_graph",
                             initial_state={"session_id": "job1"}, backend="stub")
    assert code == 1                                  # 失败,但不是被护栏拦的 3
    mb = Mailbox(runner.gate_root(tmp_path, "job1"))
    assert not any(e["event"] == "production_unsupported" for e in mb.read_events(20))
