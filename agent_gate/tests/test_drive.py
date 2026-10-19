"""drive(无人值守驱动)测试。

前 5 个用「假 launcher」(直接调 service.submit_result 答题):不依赖任何 Agent 程序。
后 2 个用真实的 claude / mcode + 本地假模型服务,端到端跑 toy 图(无真实模型、无外网)。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from support import CLAUDE_BIN, MCODE_BIN, claude_env, mcode_env, needs_claude, needs_mcode, tool_names

from agent_gate import runner, service
from agent_gate.drive import drive
from agent_gate.launcher import ClaudeLauncher, LaunchResult, McodeLauncher
from agent_gate.mailbox import Mailbox
from agent_gate.scaffold import init_project

GRAPH = "toy_graph:build_graph"
FIX = Path(__file__).resolve().parent.parent / "fixtures"

def canned(task: dict) -> object:
    p = task["user_prompt"]
    if "美学评分" in p:
        return {"caption": "画面", "aes_score": 6.0}
    if "要保留的 clip id" in p:
        return {"keep_clip_ids": ["c1", "c3"]}
    if "20 字" in p:
        return "两个好镜头"
    if "总结" in p:
        return {"summary": "ok"}
    if "转场" in p:
        return {"transition": "fade"}
    return {"note": "?"}


class FakeLauncher:
    """假 Agent:每个会话直接答掉最多 per_session 个待办任务。"""

    name = "fake"

    def __init__(self, outputs: Path, thread: str, *, per_session: int = 2,
                 do_nothing: bool = False, on_session=None) -> None:
        self.outputs, self.thread = outputs, thread
        self.per_session, self.do_nothing, self.on_session = per_session, do_nothing, on_session
        self.sessions = 0

    def run(self, prompt: str) -> LaunchResult:
        self.sessions += 1
        if self.on_session:
            self.on_session(self.sessions)
        mb = Mailbox(runner.gate_root(self.outputs, self.thread))
        if not self.do_nothing:
            for tid in mb.pending_ids()[: self.per_session]:
                a = canned(mb.read_task(tid))
                service.submit_result(mb, tid, a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))
        return LaunchResult(0, "{}", "")


def _video(tmp_path: Path) -> Path:
    v = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=3:size=320x240:rate=10", "-pix_fmt", "yuv420p", str(v)], check=True)
    return v


def _init_file(outputs: Path, thread: str, video: Path) -> Path:
    mb = Mailbox(runner.gate_root(outputs, thread))
    mb.root.mkdir(parents=True, exist_ok=True)
    f = mb.root / "initial_state.json"
    f.write_text(json.dumps({"session_id": thread, "video_input_path": str(video), "error_log": []},
                            ensure_ascii=False), encoding="utf-8")
    return f


def _kill_worker(outputs: Path, thread: str) -> None:
    pid = (Mailbox(runner.gate_root(outputs, thread)).read_status() or {}).get("pid")
    if pid:
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass


def _human_resume(outputs: Path, thread: str) -> None:
    """模拟真人放行(真人场景是 CLI `resume`,且要求交互终端;这里直接调库)。"""
    r = runner.spawn_worker(thread_id=thread, outputs_root=outputs, graph_factory=GRAPH,
                            resume_values={"①": True}, mode="resume")
    assert r["ok"], r


def _setup(tmp_path: Path, monkeypatch, thread: str):
    monkeypatch.setenv("TOY_CKPT_DIR", str(tmp_path / "ckpt"))
    out = tmp_path / "out"
    return out, _init_file(out, thread, _video(tmp_path))


def _run(out, thread, launcher, init, **kw):
    return drive(thread_id=thread, outputs_root=out, graph_factory=GRAPH, launcher=launcher,
                 initial_state_file=init, poll_s=0.2, **kw)


# ------------------------------------------------------------------ 假 launcher
def test_drive_until_human_gate_then_done(tmp_path, monkeypatch):
    out, init = _setup(tmp_path, monkeypatch, "d1")
    fl = FakeLauncher(out, "d1", per_session=2)
    res = _run(out, "d1", fl, init)
    assert (res["outcome"], res["exit_code"]) == ("ask_human", 10)
    assert res["status"]["answered_tasks"] == 6 and fl.sessions >= 3
    assert res["status"]["interrupts"][0]["checkpoint"] == "①"

    _human_resume(out, "d1")
    res2 = _run(out, "d1", fl, init)
    assert (res2["outcome"], res2["exit_code"]) == ("done", 0)
    assert res2["status"]["answered_tasks"] == 7


def test_drive_stops_when_agent_makes_no_progress(tmp_path, monkeypatch):
    out, init = _setup(tmp_path, monkeypatch, "d2")
    try:
        res = _run(out, "d2", FakeLauncher(out, "d2", do_nothing=True), init, stall_limit=2)
        assert (res["outcome"], res["exit_code"]) == ("stalled", 4)
        assert res["sessions"] == 2
    finally:
        _kill_worker(out, "d2")


def test_drive_stops_at_session_budget(tmp_path, monkeypatch):
    out, init = _setup(tmp_path, monkeypatch, "d3")
    try:
        res = _run(out, "d3", FakeLauncher(out, "d3", do_nothing=True), init, stall_limit=99, max_sessions=2)
        assert (res["outcome"], res["exit_code"]) == ("budget", 3)
    finally:
        _kill_worker(out, "d3")


def test_drive_needs_input_when_nothing_started(tmp_path, monkeypatch):
    monkeypatch.setenv("TOY_CKPT_DIR", str(tmp_path / "ckpt"))
    res = drive(thread_id="d4", outputs_root=tmp_path / "out", graph_factory=GRAPH,
                launcher=FakeLauncher(tmp_path / "out", "d4"), poll_s=0.2)
    assert (res["outcome"], res["exit_code"]) == ("need_input", 2)


def test_cli_drive_warns_when_mcode_is_not_locked(tmp_path):
    base = [sys.executable, "-m", "agent_gate", "drive", "--thread-id", "w1", "--outputs-root", str(tmp_path / "out"),
            "--graph-factory", GRAPH, "--project-dir", str(tmp_path)]    # 没给 --video:不会真的拉起 Agent,直接 need_input

    def run(*extra):
        cp = subprocess.run(base + list(extra), capture_output=True, text=True, encoding="utf-8",
                            stdin=subprocess.DEVNULL, timeout=60)
        assert cp.returncode == 2, cp.stdout + cp.stderr                # need_input
        return json.loads(cp.stdout)

    warned = run("--agent", "mcode")
    assert warned["outcome"] == "need_input" and "mcode-lock" in warned["warnings"][0]
    assert "warnings" not in run("--agent", "mcode", "--mcode-config", str(tmp_path / "locked.yaml"))
    assert "warnings" not in run("--agent", "claude")                  # claude 用启动参数硬隔离,不需要配置文件


def test_launcher_commands(tmp_path):
    # 注意:executable 会被 resolve_agent_bin() 解析成**能直接 CreateProcess 的路径**。
    # Windows 上裸名 "claude" / "mcode" 会因 CreateProcess 不做 PATHEXT 补全而拉不起来
    # (见 test_agent_bin.py),所以这里断言「解析后的路径 + 同一个 basename」,
    # 而不是断言裸名。
    cl = ClaudeLauncher(project_dir=tmp_path, executable="claude", model="m1", max_turns=7).command()
    assert Path(cl[0]).name.lower().startswith("claude")
    assert cl[1:4] == ["-p", "--output-format", "json"] and cl[cl.index("--tools") + 1] == ""
    assert cl[cl.index("--mcp-config") + 1] == str(tmp_path / ".mcp.json") and "--strict-mcp-config" in cl
    assert cl[cl.index("--allowedTools") + 1] == "mcp__video-agent__*" and cl[cl.index("--max-turns") + 1] == "7"
    assert ClaudeLauncher(project_dir=tmp_path).env()["CLAUDE_CODE_MCP_STARTUP_WAIT_MS"] == "30000"

    mc = McodeLauncher(project_dir=tmp_path, executable="mcode", permission="full", max_steps=9,
                       session_timeout_s=60).command()
    assert Path(mc[0]).name.lower().startswith("mcode")
    assert mc[1:4] == ["exec", "--input", "-"] and mc[mc.index("--permission") + 1] == "full"
    assert mc[mc.index("--max-steps") + 1] == "9" and mc[mc.index("--timeout") + 1] == "60s"
    assert mc[mc.index("--cwd") + 1] == str(tmp_path) and "--config" not in mc
    locked = McodeLauncher(project_dir=tmp_path, executable="mcode", config=tmp_path / "l.yaml").command()
    assert locked[locked.index("--config") + 1] == str(tmp_path / "l.yaml")


def test_drive_restarts_a_crashed_worker_once(tmp_path, monkeypatch):
    out, init = _setup(tmp_path, monkeypatch, "d5")

    def crash_at_session_2(n: int) -> None:
        if n == 2:
            _kill_worker(out, "d5")   # kill -9:没机会写任何收尾状态

    res = _run(out, "d5", FakeLauncher(out, "d5", per_session=2, on_session=crash_at_session_2), init)
    assert res["outcome"] == "ask_human"
    assert res["status"]["answered_tasks"] == 6          # 重启后重放已答任务,不重复问
    events = [e["event"] for e in Mailbox(runner.gate_root(out, "d5")).read_events(500)]
    assert "drive_restart_worker" in events


# ------------------------------------------------------------------ 真实 claude / mcode
def _project(tmp_path: Path, out: Path) -> Path:
    """用 `agent_gate init` 同一个函数生成操作员目录,再补上测试专用的环境变量。"""
    proj = tmp_path / "proj"
    init_project(project_dir=proj, repo_root=FIX, outputs_root=out, graph_factory=GRAPH, python=sys.executable)
    p = proj / ".mcp.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["mcpServers"]["video-agent"]["env"].update({
        "TOY_CKPT_DIR": str(tmp_path / "ckpt"), "AGENT_GATE_HEARTBEAT_S": "0.3", "AGENT_GATE_STALE_AFTER_S": "1.5"})
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return proj


def _two_phase(out, thread, launcher, init):
    res = _run(out, thread, launcher, init, batch=2, max_sessions=20)
    assert res["outcome"] == "ask_human", res
    assert res["status"]["answered_tasks"] == 6
    assert res["sessions"] >= 2                          # 每会话最多 5 次工具调用,必须分好几个会话
    assert res["status"]["interrupts"][0]["checkpoint"] == "①"
    events = Mailbox(runner.gate_root(out, thread)).read_events(500)
    sessions = [e for e in events if e["event"] == "agent_session"]
    assert sessions and all(e["returncode"] == 0 and not e["timed_out"] for e in sessions)
    assert not any(e.get("permission_denials") for e in sessions)      # 没有工具被拒

    _human_resume(out, thread)
    res2 = _run(out, thread, launcher, init, batch=2, max_sessions=20)
    assert (res2["outcome"], res2["status"]["answered_tasks"]) == ("done", 7)


@needs_claude
def test_drive_with_real_claude(tmp_path, monkeypatch, mock):
    out, init = _setup(tmp_path, monkeypatch, "c1")
    srv = mock({"main_models": ["mock-main"], "policy": "batch_agent", "thread_id": "c1",
                "batch": 2, "session_calls": 5, "default_text": "ok"})
    launcher = ClaudeLauncher(project_dir=_project(tmp_path, out), executable=CLAUDE_BIN,
                              model="mock-main", session_timeout_s=120, clean_env=True,
                              extra_env=claude_env(tmp_path, srv.port))
    _two_phase(out, "c1", launcher, init)
    allowed = {f"mcp__video-agent__{n}" for n in ("run_start", "run_status", "gate_next_tasks",
                                                  "gate_get_task", "gate_submit_result", "gate_submit_batch")}
    reqs = srv.main_requests()
    assert reqs and all(set(tool_names(r["body"])) == allowed for r in reqs)   # 每个请求里模型只有这 6 个工具


MCP_TOOLS = ("run_start", "run_status", "gate_next_tasks", "gate_get_task", "gate_submit_result", "gate_submit_batch")
# mcode 加锁后仍会留下的内置工具(沙箱实测):查任务、跳技能、设目标——都碰不到文件、命令行、网络
MCODE_HARMLESS = {"skill", "task_query", "task_output", "task_stop", "update_goal", "get_goal"}


def _tool_set(srv) -> set:
    names: set = set()
    for r in srv.main_requests():
        names |= set(tool_names(r["body"]))
    return names


@needs_mcode
def test_drive_with_real_mcode(tmp_path, monkeypatch, mock):
    out, init = _setup(tmp_path, monkeypatch, "m1")
    srv = mock({"main_models": ["mock-main"], "policy": "batch_agent", "thread_id": "m1",
                "batch": 2, "session_calls": 5, "default_text": "ok"})
    launcher = McodeLauncher(project_dir=_project(tmp_path, out), executable=MCODE_BIN,
                             session_timeout_s=120, clean_env=True, extra_env=mcode_env(tmp_path, srv.port))
    _two_phase(out, "m1", launcher, init)
    assert {"bash", "write"} <= _tool_set(srv)           # 对照:不加锁时,模型手里有 bash 和写文件


@needs_mcode
def test_drive_with_real_mcode_locked_config(tmp_path, monkeypatch, mock):
    out, init = _setup(tmp_path, monkeypatch, "m2")
    srv = mock({"main_models": ["mock-main"], "policy": "batch_agent", "thread_id": "m2",
                "batch": 2, "session_calls": 5, "default_text": "ok"})
    data_dir = tmp_path / "mm_data"                      # 文档里的做法:专用的 MINIMAX_DATA_DIR
    env = mcode_env(tmp_path, srv.port, data_dir=data_dir)
    base = data_dir / "config.yaml"
    assert base.exists(), "provider add 没有把配置写进 MINIMAX_DATA_DIR"

    locked = tmp_path / "mcode.locked.yaml"
    cp = subprocess.run([sys.executable, "-m", "agent_gate", "mcode-lock", "--base", str(base), "--out", str(locked)],
                        capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr

    launcher = McodeLauncher(project_dir=_project(tmp_path, out), executable=MCODE_BIN, session_timeout_s=120,
                             clean_env=True, extra_env=env, config=locked)
    assert launcher.command()[launcher.command().index("--config") + 1] == str(locked)
    _two_phase(out, "m2", launcher, init)                # 加锁后整条流程照样跑通

    names = _tool_set(srv)
    mcp = {n for n in names if n.endswith(MCP_TOOLS)}
    assert len(mcp) == 6, names                                          # 6 个 MCP 工具都在
    assert names - mcp <= MCODE_HARMLESS, sorted(names - mcp - MCODE_HARMLESS)   # 除此之外没有别的有风险的工具
