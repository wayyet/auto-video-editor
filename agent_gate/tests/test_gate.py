"""agent_gate 的单元与集成测试。不依赖任何真实 LLM,也不运行仓库业务代码。"""
from __future__ import annotations

import json
import operator
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from support import seed_tasks

from agent_gate import runner, service
from agent_gate.gate_client import GateLLMClient, LLMGateRejected, LLMGateTimeout
from agent_gate.mailbox import Mailbox
from agent_gate.protocol import task_id_for
from agent_gate.validate import NotJSON, parse_json_value, validate_result

GRAPH = "toy_graph:build_graph"


# ------------------------------------------------------------------ 基础
def test_task_id_stable_and_sensitive():
    a = {"system_prompt": "s", "user_prompt": "u", "media": [], "json_schema": None}
    assert task_id_for(a) == task_id_for(dict(reversed(list(a.items()))))
    assert task_id_for(a) != task_id_for({**a, "user_prompt": "u2"})
    assert task_id_for(a).startswith("t_") and len(task_id_for(a)) == 18


def test_parse_and_validate():
    assert parse_json_value('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_value('\ufeff{"a": 1}') == {"a": 1}                    # 开头的 BOM 不算内容
    with pytest.raises(NotJSON):
        parse_json_value("{bad")
    schema = {"type": "object", "properties": {"n": {"type": "number"}}, "required": ["n"]}
    assert validate_result(schema, {"n": 1}) == []
    errs = validate_result(schema, {"n": "x"})
    assert errs and errs[0].startswith("$.n:")
    assert validate_result(None, {"anything": 1}) == []
    assert validate_result({"type": "nonsense-type"}, 1) == []  # 非法 schema 不拦


def _agent_answer(task: dict) -> str:
    """测试用的「Agent」:按任务内容给出合格答案。"""
    p = task["user_prompt"]
    if "美学评分" in p:
        return json.dumps({"caption": "画面:" + p[4:10], "aes_score": 8.0}, ensure_ascii=False)
    if "要保留的 clip id" in p:
        return json.dumps({"keep_clip_ids": ["c1", "c3"]})
    if "文案" in p and "20 字" in p:
        return "两个好镜头"
    if "总结" in p:  # 必须放在「转场」之前:总结任务的 prompt 里也带「转场」二字
        return json.dumps({"summary": "ok"}, ensure_ascii=False)
    if "转场" in p:
        return json.dumps({"transition": "fade"})
    raise AssertionError("没见过的任务:" + p)


def _drive(mb: Mailbox, thread_id: str, *, until: set[str], budget_s: float = 60.0) -> dict:
    """扮演 Agent:循环领任务、交答案,直到 worker 进入 until 里的状态。"""
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        res = service.wait_for_work(mb, thread_id=thread_id, wait_s=2.0, poll_s=0.1)
        for t in res["tasks"]:
            full = mb.read_task(t["task_id"])
            r = service.submit_result(mb, t["task_id"], _agent_answer(full))
            assert r["ok"], r
        if res["worker_state"] in until and not res["tasks"]:
            return res
    raise AssertionError(f"超时,最后状态:{service.status_summary(mb, thread_id=thread_id)}")


def test_cli_json_files_with_utf8_bom_are_accepted(tmp_path):
    """Windows PowerShell 5.1 的 `Set-Content -Encoding utf8` 会在文件开头写 BOM:--answer-file / --results-file 要能读。"""
    out = tmp_path / "out"
    mb = Mailbox(runner.gate_root(out, "bom"))
    t1, t2 = seed_tasks(mb, n_tasks=2, frames=0)
    good = json.dumps({"caption": "画面", "aes_score": 5}, ensure_ascii=False).encode("utf-8")

    def cli(*args):
        cp = subprocess.run([sys.executable, "-m", "agent_gate", *args, "--thread-id", "bom", "--outputs-root", str(out)],
                            capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60)
        return cp.returncode, json.loads(cp.stdout)

    one = tmp_path / "one.json"
    one.write_bytes(b"\xef\xbb\xbf" + good)
    assert cli("submit", "--task-id", t1, "--answer-file", str(one)) == (0, {"ok": True})

    many = tmp_path / "many.json"
    many.write_bytes(b"\xef\xbb\xbf" + json.dumps({t2: json.loads(good)}, ensure_ascii=False).encode("utf-8"))
    rc, res = cli("submit-batch", "--results-file", str(many))
    assert rc == 0 and res["ok"] is True


# ------------------------------------------------------------------ GateLLMClient
def test_gate_roundtrip_and_replay(tmp_path):
    mb = Mailbox(tmp_path)
    client = GateLLMClient(mb, poll_s=0.05, timeout_s=10)
    out: dict = {}
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}

    def call():
        out["v"] = client.chat(system_prompt="s", user_prompt="u", json_schema=schema)

    th = threading.Thread(target=call)
    th.start()
    for _ in range(100):
        if mb.pending_ids():
            break
        time.sleep(0.05)
    (tid,) = mb.pending_ids()
    assert service.submit_result(mb, tid, '{"x": "bad"}')["code"] == "invalid"       # 类型不对
    assert service.submit_result(mb, tid, "not json")["code"] == "invalid"            # 不是 JSON
    assert service.submit_result(mb, tid, '{"x": 7}') == {"ok": True}
    th.join(5)
    assert json.loads(out["v"]) == {"x": 7}
    # 重放:同样的请求直接拿到上次的答案,不再产生新任务
    assert json.loads(client.chat(system_prompt="s", user_prompt="u", json_schema=schema)) == {"x": 7}
    assert len(list(mb.tasks_dir.glob("*.task.json"))) == 1


def test_attempts_exhausted_raises_and_is_catchable(tmp_path):
    mb = Mailbox(tmp_path)
    client = GateLLMClient(mb, poll_s=0.05, timeout_s=10, max_attempts=2)
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    box: dict = {}

    def call():
        try:
            client.chat(system_prompt="s", user_prompt="u", json_schema=schema)
        except Exception as e:  # noqa: BLE001  与仓库一致:能力函数用 except Exception 接住并降级
            box["err"] = e

    th = threading.Thread(target=call)
    th.start()
    for _ in range(100):
        if mb.pending_ids():
            break
        time.sleep(0.05)
    (tid,) = mb.pending_ids()
    assert service.submit_result(mb, tid, "{}")["attempts_left"] == 1
    last = service.submit_result(mb, tid, "{}")
    assert last["code"] == "attempts_exhausted"
    th.join(5)
    assert isinstance(box["err"], LLMGateRejected)
    assert service.submit_result(mb, tid, '{"x": 1}')["code"] == "task_closed"   # 关闭后不再收
    assert mb.count("error") == 1 and mb.pending_ids() == []


def test_timeout_is_catchable(tmp_path):
    mb = Mailbox(tmp_path)
    client = GateLLMClient(mb, poll_s=0.02, timeout_s=0.2)
    with pytest.raises(LLMGateTimeout):
        client.chat(system_prompt="s", user_prompt="u")
    with pytest.raises(LLMGateRejected):  # 同进程再问同一个问题:立即放弃,不再干等
        client.chat(system_prompt="s", user_prompt="u")
    assert mb.reset_errors() == 1          # 下次 worker 启动会重新开放
    assert mb.pending_ids() == [task_id_for({"system_prompt": "s", "user_prompt": "u",
                                             "media": [], "json_schema": None})]


def test_media_frames_from_video(tmp_path):
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    img = tmp_path / "a.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64",
                    "-frames:v", "1", str(img)], check=True)
    mb = Mailbox(tmp_path / "gate")
    client = GateLLMClient(mb, poll_s=0.02, timeout_s=0.2, frames_per_clip=4)
    with pytest.raises(LLMGateTimeout):
        client.chat(system_prompt="s", user_prompt="u",
                    media=[{"path": str(video), "in_sec": 0.5, "out_sec": 2.5}, {"path": str(img)}])
    (tid,) = [p.name[:-len(".task.json")] for p in mb.tasks_dir.glob("*.task.json")]
    task = mb.read_task(tid)
    assert len(task["frames"]) == 5                      # 视频 4 帧 + 图片 1 张
    assert all(Path(f).exists() for f in task["frames"])
    assert task["frames"][-1] == str(img)


# ------------------------------------------------------------------ 为什么不用 interrupt() 做闸门
def test_interrupt_inside_except_exception_is_swallowed():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    class St(TypedDict, total=False):
        log: Annotated[list, operator.add]

    def node(state):
        try:
            interrupt({"checkpoint": "X"})
        except Exception as e:  # noqa: BLE001
            return {"log": [type(e).__name__]}
        return {"log": ["resumed"]}

    g = StateGraph(St)
    g.add_node("n", node)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    app = g.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t"}}
    out = app.invoke({"log": []}, cfg)
    assert out["log"] == ["GraphInterrupt"]               # 被吞了
    assert app.get_state(cfg).interrupts == ()            # 图根本没有暂停


# ------------------------------------------------------------------ 端到端:worker 子进程 + 玩具图
@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TOY_CKPT_DIR", str(tmp_path / "ckpt"))
    outputs = tmp_path / "outputs"
    return outputs, "job1", Mailbox(runner.gate_root(outputs, "job1"))


def _start(outputs, thread_id, **kw):
    res = runner.spawn_worker(thread_id=thread_id, outputs_root=outputs, graph_factory=GRAPH, **kw)
    assert res["ok"], res
    return res


def _state_values(outputs, thread_id):
    graph = runner.load_callable(GRAPH)(thread_id=thread_id)
    return graph.get_state({"configurable": {"thread_id": thread_id}}).values


def test_e2e_agent_drives_pipeline_with_human_gate(env, tmp_path):
    outputs, tid, mb = env
    init = tmp_path / "init.json"
    init.write_text(json.dumps({"session_id": tid, "video_input_path": "", "error_log": []}))
    _start(outputs, tid, initial_state_file=init)

    res = _drive(mb, tid, until={"waiting_human"})
    assert res["next_action"] == "ask_human"
    assert res["interrupts"][0]["checkpoint"] == "①"
    assert "agent_gate resume" in res["human_commands"][0]

    # 人工关卡放行(子进程里的 resume)
    _start(outputs, tid, resume_values={"①": True})
    res = _drive(mb, tid, until={"completed"})
    assert res["next_action"] == "done"

    v = _state_values(outputs, tid)
    assert v["kept"] == ["c1", "c3"]
    assert v["transition"] == "fade" and v["script"] == "两个好镜头"
    assert v["final"] == {"summary": "ok"}
    assert all(c["aes_score"] == 8.0 for c in v["captions"].values())
    assert "filter_fallback" not in v["status_log"]       # 没有降级
    assert mb.count("error") == 0
    assert mb.count("result") == 7                         # 3 + 1 + 1 + 1 + 1


def test_e2e_crash_then_continue_replays_answers(env, tmp_path):
    outputs, tid, mb = env
    init = tmp_path / "init.json"
    init.write_text(json.dumps({"session_id": tid, "video_input_path": "", "error_log": []}))
    res = _start(outputs, tid, initial_state_file=init)

    # 只答第一个任务,然后杀掉 worker
    for _ in range(200):
        if mb.pending_ids():
            break
        time.sleep(0.05)
    first = mb.pending_ids()[0]
    assert service.submit_result(mb, first, _agent_answer(mb.read_task(first)))["ok"]
    for _ in range(200):                                  # 等第二个任务出现(第一个已被消费)
        if first not in mb.pending_ids() and mb.pending_ids():
            break
        time.sleep(0.05)
    os.kill(res["pid"], signal.SIGKILL)

    for _ in range(100):                                  # 心跳过期 → crashed
        s = service.status_summary(mb, thread_id=tid)
        if s["worker_state"] == "crashed":
            break
        time.sleep(0.1)
    assert s["worker_state"] == "crashed" and s["next_action"] == "restart_worker"

    before = {p.name[: -len(".task.json")] for p in mb.tasks_dir.glob("*.task.json")}
    _start(outputs, tid)                                  # 不给初始 state = 从检查点继续
    res = _drive(mb, tid, until={"waiting_human"})
    assert res["worker_state"] == "waiting_human"
    # 第一个任务没有被重复提问:它的 task_id 没有生成新文件,且结果文件只有一份
    assert first in before and mb.read_result(first)["attempts"] == 1


def test_resume_with_wrong_checkpoint_is_rejected(env, tmp_path):
    outputs, tid, mb = env
    init = tmp_path / "init.json"
    init.write_text(json.dumps({"session_id": tid, "video_input_path": "", "error_log": []}))
    _start(outputs, tid, initial_state_file=init)
    _drive(mb, tid, until={"waiting_human"})
    _start(outputs, tid, resume_values={"②": True})       # 没有关卡②
    for _ in range(100):
        s = service.status_summary(mb, thread_id=tid)
        if s["worker_state"] == "failed":
            break
        time.sleep(0.1)
    assert s["worker_state"] == "failed" and "没有匹配的挂起" in s["error"]["message"]


def test_cli_resume_refuses_without_tty(env, tmp_path):
    outputs, tid, _ = env
    cp = subprocess.run(
        [sys.executable, "-m", "agent_gate", "resume", "--thread-id", tid,
         "--outputs-root", str(outputs), "--checkpoint", "①", "--graph-factory", GRAPH],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
    assert cp.returncode == 1
    assert json.loads(cp.stdout)["code"] == "need_human_terminal"
