"""图片预算:一次最多返回 MAX_IMAGES 张画面帧;装不下的任务推迟到下一次取,绝不悄悄缺图。

前两组测试不需要 claude / mcode。最后两个用真实尺寸的画面帧(约 77 KB/张),
确认 12 张图能原样走完 MCP → claude / mcode → 模型请求这条路。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent, TextContent
from support import (
    CLAUDE_BIN,
    MCODE_BIN,
    claude_env,
    gate_tasks_seen,
    make_frame_jpg,
    mcode_env,
    needs_claude,
    needs_mcode,
    seed_tasks,
)
from test_drive import _project

from agent_gate import runner, service
from agent_gate.drive import BATCH_PROMPT
from agent_gate.launcher import ClaudeLauncher, McodeLauncher
from agent_gate.mailbox import Mailbox
from agent_gate.mcp_server import MAX_IMAGES

GRAPH = "toy_graph:build_graph"


# ------------------------------------------------------------------ pack_for_images(纯函数)
def _task(tmp_path, tid, n_frames, *, missing=0):
    """n_frames 张图,其中最后 missing 张只写在任务里、磁盘上没有。"""
    paths = []
    for i in range(n_frames):
        p = tmp_path / f"{tid}_{i}.jpg"
        if i < n_frames - missing:
            p.write_bytes(b"x")
        paths.append(str(p))
    return {"task_id": tid, "frames": paths, "notes": []}


def test_pack_stops_before_the_task_that_does_not_fit(tmp_path):
    tasks = [_task(tmp_path, f"a{i}", 6) for i in range(3)]
    chosen, deferred, pairs = service.pack_for_images(tasks, 12)
    assert [t["task_id"] for t in chosen] == ["a0", "a1"] and deferred == 1 and len(pairs) == 12
    assert all(t["images"] == {"shown": 6, "total": 6} and t["notes"] == [] for t in chosen)
    assert [tid for tid, _ in pairs] == ["a0"] * 6 + ["a1"] * 6


def test_pack_first_task_is_always_taken_even_if_over_budget(tmp_path):
    chosen, deferred, pairs = service.pack_for_images([_task(tmp_path, "big", 14), _task(tmp_path, "b", 2)], 12)
    assert [t["task_id"] for t in chosen] == ["big"] and deferred == 1 and len(pairs) == 12
    assert chosen[0]["images"] == {"shown": 12, "total": 14}
    assert "12/14" in chosen[0]["notes"][0] and "不要编造" in chosen[0]["notes"][0]


def test_pack_counts_only_files_that_exist(tmp_path):
    chosen, deferred, pairs = service.pack_for_images([_task(tmp_path, "m", 6, missing=2)], 12)
    assert chosen[0]["images"] == {"shown": 4, "total": 6} and len(pairs) == 4 and deferred == 0
    assert "4/6" in chosen[0]["notes"][0]


def test_pack_text_tasks_cost_no_budget_and_order_is_kept(tmp_path):
    tasks = [_task(tmp_path, "v1", 6), _task(tmp_path, "v2", 6), _task(tmp_path, "txt", 0), _task(tmp_path, "v3", 6)]
    chosen, deferred, pairs = service.pack_for_images(tasks, 12)
    assert [t["task_id"] for t in chosen] == ["v1", "v2", "txt"] and deferred == 1     # 到 v3 停,顺序不变
    assert chosen[2]["images"] == {"shown": 0, "total": 0} and chosen[2]["notes"] == []


def test_pack_empty():
    assert service.pack_for_images([], 12) == ([], 0, [])


# ------------------------------------------------------------------ MCP 服务(官方 MCP 客户端,不需要 claude / mcode)
def _params(outputs, ckpt):
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_gate.mcp_server", "--outputs-root", str(outputs), "--graph-factory", GRAPH],
        env=dict(os.environ, TOY_CKPT_DIR=str(ckpt), PYTHONUTF8="1"))


def _head(res) -> dict:
    return json.loads([c for c in res.content if isinstance(c, TextContent)][0].text)


def _n_images(res) -> int:
    return sum(1 for c in res.content if isinstance(c, ImageContent))


def test_mcp_returns_tasks_in_batches_that_fit_the_image_budget(tmp_path):
    assert MAX_IMAGES == 12
    outputs = tmp_path / "o"
    mb = Mailbox(runner.gate_root(outputs, "b1"))
    ids = seed_tasks(mb, n_tasks=3, frames=6)

    async def answer(s, task_id):
        ans = json.dumps({"caption": "x", "aes_score": 5})
        res = await s.call_tool("gate_submit_result", {"thread_id": "b1", "task_id": task_id, "answer": ans})
        assert res.structuredContent == {"ok": True}

    async def go():
        async with stdio_client(_params(outputs, tmp_path / "c")) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("gate_next_tasks", {"thread_id": "b1", "wait_s": 0, "limit": 3})
                head = _head(res)
                assert [t["task_id"] for t in head["tasks"]] == ids[:2]
                assert head["deferred_tasks"] == 1 and _n_images(res) == 12
                assert [t["images"] for t in head["tasks"]] == [{"shown": 6, "total": 6}] * 2

                for t in head["tasks"]:
                    await answer(s, t["task_id"])
                res2 = await s.call_tool("gate_next_tasks", {"thread_id": "b1", "wait_s": 0, "limit": 3})
                head2 = _head(res2)
                assert [t["task_id"] for t in head2["tasks"]] == ids[2:]
                assert head2["deferred_tasks"] == 0 and _n_images(res2) == 6

    asyncio.run(go())


def test_mcp_single_oversized_task_is_truncated_and_says_so(tmp_path):
    outputs = tmp_path / "o"
    mb = Mailbox(runner.gate_root(outputs, "b2"))
    [tid] = seed_tasks(mb, n_tasks=1, frames=14)

    async def go():
        async with stdio_client(_params(outputs, tmp_path / "c")) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for tool, args in (("gate_next_tasks", {"thread_id": "b2", "limit": 3}),
                                   ("gate_get_task", {"thread_id": "b2", "task_id": tid})):
                    res = await s.call_tool(tool, args)
                    head = _head(res)
                    task = head["tasks"][0] if "tasks" in head else head
                    assert _n_images(res) == 12, tool
                    assert task["images"] == {"shown": 12, "total": 14}, tool
                    assert any("12/14" in n for n in task["notes"]), tool       # 模型被告知「只看到 12/14 张」

    asyncio.run(go())


def test_mcp_batch_file_with_utf8_bom(tmp_path):
    outputs = tmp_path / "o"
    mb = Mailbox(runner.gate_root(outputs, "b3"))
    ids = seed_tasks(mb, n_tasks=2, frames=0)

    async def go():
        async with stdio_client(_params(outputs, tmp_path / "c")) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                st = await s.call_tool("run_status", {"thread_id": "b3"})
                sub = tmp_path / "o" / "b3" / "agent_gate" / "submissions"
                assert st.structuredContent["submissions_dir"] == str(sub)
                sub.mkdir(parents=True, exist_ok=True)
                answers = {t: {"caption": "x", "aes_score": 1} for t in ids}
                (sub / "bom.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(answers).encode("utf-8"))
                res = await s.call_tool("gate_submit_batch", {"thread_id": "b3", "results_file": "bom.json"})
                assert res.structuredContent["ok"] is True, res.structuredContent

    asyncio.run(go())


# ------------------------------------------------------------------ 真实尺寸画面帧 × 真实 claude / mcode
@pytest.fixture(scope="module")
def real_frame(tmp_path_factory):
    p = make_frame_jpg(tmp_path_factory.mktemp("frame") / "f.jpg")
    assert 40_000 < p.stat().st_size < 120_000                       # 和真实抽帧同一个量级
    return p.read_bytes()


def _run_one_session(tmp_path, mock, real_frame, launcher_cls, env_fn, **kw):
    out = tmp_path / "out"
    mb = Mailbox(runner.gate_root(out, "i1"))
    ids = seed_tasks(mb, n_tasks=3, frames=6, frame_bytes=real_frame)
    srv = mock({"main_models": ["mock-main"], "policy": "batch_agent", "thread_id": "i1", "batch": 3,
                "session_calls": 1, "default_text": "ok"})                  # 只取 1 次任务就结束
    launcher = launcher_cls(project_dir=_project(tmp_path, out), clean_env=True,
                            extra_env=env_fn(tmp_path, srv.port), session_timeout_s=120, **kw)
    res = launcher.run(BATCH_PROMPT.format(thread_id="i1", batch=3))
    assert res.returncode == 0 and not res.timed_out, (res.stdout[-800:], res.stderr[-800:])
    payload, n_img = gate_tasks_seen(srv.main_requests()[-1]["body"])
    assert payload is not None, "模型没有收到 gate_next_tasks 的结果"
    assert [t["task_id"] for t in payload["tasks"]] == ids[:2]
    assert payload["deferred_tasks"] == 1
    assert n_img == 12                                                      # 12 张真实尺寸的图一张不少
    assert [t["images"] for t in payload["tasks"]] == [{"shown": 6, "total": 6}] * 2


@needs_claude
def test_real_size_frames_reach_claude(tmp_path, mock, real_frame):
    _run_one_session(tmp_path, mock, real_frame, ClaudeLauncher, claude_env, executable=CLAUDE_BIN, model="mock-main")


@needs_mcode
def test_real_size_frames_reach_mcode(tmp_path, mock, real_frame):
    _run_one_session(tmp_path, mock, real_frame, McodeLauncher, mcode_env, executable=MCODE_BIN)
