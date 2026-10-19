"""MCP 服务测试:用官方 MCP Python 客户端通过 stdio 连真实的服务进程。"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent, TextContent

GRAPH = "toy_graph:build_graph"


def _params(outputs, ckpt, *extra):
    env = dict(os.environ, TOY_CKPT_DIR=str(ckpt), PYTHONUTF8="1")
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_gate.mcp_server", "--outputs-root", str(outputs),
              "--graph-factory", GRAPH, *extra],
        env=env,
    )


def _text(res) -> dict:
    blocks = [c for c in res.content if isinstance(c, TextContent)]
    return json.loads(blocks[0].text)


def test_tool_list_and_human_tool_flag(tmp_path):
    async def go(extra):
        async with stdio_client(_params(tmp_path / "o", tmp_path / "c", *extra)) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name: t for t in (await s.list_tools()).tools}

    safe = asyncio.run(go([]))
    assert set(safe) == {"run_start", "run_status", "gate_next_tasks", "gate_get_task",
                         "gate_submit_result", "gate_submit_batch"}
    assert "human_resume" not in safe                      # 默认不暴露人工放行

    with_human = asyncio.run(go(["--with-human-tool"]))
    meta = with_human["human_resume"].meta
    assert meta == {"anthropic/requiresUserInteraction": True}


def test_mcp_flow_with_images_and_validation(tmp_path):
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=3:size=320x240:rate=10", "-pix_fmt", "yuv420p", str(video)], check=True)
    outputs, ckpt = tmp_path / "o", tmp_path / "c"

    async def go():
        async with stdio_client(_params(outputs, ckpt)) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                st = await s.call_tool("run_start", {"thread_id": "m1", "video_path": str(video)})
                assert st.structuredContent["ok"] is True

                res = await s.call_tool("gate_next_tasks", {"thread_id": "m1", "wait_s": 20, "limit": 1})
                head = _text(res)
                assert head["next_action"] == "answer_tasks" and len(head["tasks"]) == 1
                images = [c for c in res.content if isinstance(c, ImageContent)]
                assert len(images) == 6 and images[0].mimeType == "image/jpeg"   # 视频段抽了 6 帧
                tid = head["tasks"][0]["task_id"]

                bad = await s.call_tool("gate_submit_result", {"thread_id": "m1", "task_id": tid,
                                                               "answer": '{"caption": 1}'})
                assert bad.structuredContent["code"] == "invalid"
                good = await s.call_tool("gate_submit_result", {
                    "thread_id": "m1", "task_id": tid,
                    "answer": json.dumps({"caption": "测试画面", "aes_score": 7.5}, ensure_ascii=False)})
                assert good.structuredContent == {"ok": True}

                # 路径穿越被拒
                st2 = await s.call_tool("run_status", {"thread_id": "m1"})
                evil = await s.call_tool("gate_submit_batch", {"thread_id": "m1",
                                                               "results_file": "../../../etc/passwd"})
                assert evil.structuredContent["code"] == "bad_file"

                # 一次批量提交:写文件到 submissions_dir,再传文件名
                sub_dir = st2.structuredContent["submissions_dir"]
                os.makedirs(sub_dir, exist_ok=True)
                res = await s.call_tool("gate_next_tasks", {"thread_id": "m1", "wait_s": 20, "limit": 5})
                tasks = _text(res)["tasks"]
                answers = {t["task_id"]: json.dumps({"caption": "x", "aes_score": 5}) for t in tasks}
                with open(os.path.join(sub_dir, "a.json"), "w", encoding="utf-8") as f:
                    json.dump(answers, f)
                batch = await s.call_tool("gate_submit_batch", {"thread_id": "m1", "results_file": "a.json"})
                assert batch.structuredContent["ok"] is True

    asyncio.run(go())


def test_mcp_run_start_continue_after_crash(tmp_path):
    """worker 被杀后,通过 MCP 的 run_start(continue_after_crash=true) 从检查点继续;已答任务不会重复提问。"""
    from test_gate import _agent_answer                       # 复用:按任务内容答题的「Agent」

    outputs, ckpt = tmp_path / "o", tmp_path / "c"
    init = tmp_path / "init.json"
    init.write_text(json.dumps({"session_id": "r1", "video_input_path": "", "error_log": []}), encoding="utf-8")

    async def next_tasks(s, wait_s):
        res = await s.call_tool("gate_next_tasks", {"thread_id": "r1", "wait_s": wait_s, "limit": 1})
        return _text(res)

    async def submit(s, task):
        res = await s.call_tool("gate_submit_result", {"thread_id": "r1", "task_id": task["task_id"],
                                                       "answer": _agent_answer(task)})
        assert res.structuredContent.get("ok") is True, res.structuredContent

    async def go():
        async with stdio_client(_params(outputs, ckpt)) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                st = await s.call_tool("run_start", {"thread_id": "r1", "initial_state_file": str(init)})
                assert st.structuredContent["ok"] is True
                pid = st.structuredContent["pid"]
                dup = await s.call_tool("run_start", {"thread_id": "r1", "initial_state_file": str(init)})
                assert dup.structuredContent["code"] == "already_running"       # worker 在跑:不会再起第二个

                first = (await next_tasks(s, 20))["tasks"][0]
                await submit(s, first)
                for _ in range(100):                                            # 第二个任务出现 = 第一个已被图消费
                    head = await next_tasks(s, 2)
                    if head["tasks"] and head["tasks"][0]["task_id"] != first["task_id"]:
                        break
                else:
                    raise AssertionError("第二个任务没有出现")
                os.kill(pid, signal.SIGKILL)

                for _ in range(100):                                            # 心跳过期 → crashed
                    cur = (await s.call_tool("run_status", {"thread_id": "r1"})).structuredContent
                    if cur["worker_state"] == "crashed":
                        break
                    await asyncio.sleep(0.1)
                assert cur["worker_state"] == "crashed" and cur["next_action"] == "restart_worker"

                again = await s.call_tool("run_start", {"thread_id": "r1", "continue_after_crash": True})
                assert again.structuredContent["ok"] is True

                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    head = await next_tasks(s, 5)
                    for t in head["tasks"]:
                        await submit(s, t)
                    if head["next_action"] == "ask_human" and not head["tasks"]:
                        assert head["interrupts"][0]["checkpoint"] == "\u2460"   # ①
                        return first["task_id"]
                raise AssertionError("重启后没有走到人工关卡")

    first_id = asyncio.run(go())
    gate = outputs / "r1" / "agent_gate"
    events = [json.loads(l) for l in (gate / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    created = [e for e in events if e["event"] == "task_created" and e["task_id"] == first_id]
    assert len(created) == 1                                                    # 崩溃重放后没有再问第二遍
    assert json.loads((gate / "tasks" / f"{first_id}.result.json").read_text(encoding="utf-8"))["attempts"] == 1
