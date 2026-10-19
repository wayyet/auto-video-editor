"""init(生成操作员目录)、guard_hook(钩子)、AGENT_GATE_REPO_ROOT 的测试。不需要 claude / mcode。"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_gate import guard_hook, runner
from agent_gate.mailbox import Mailbox
from agent_gate.scaffold import MCODE_LOCK_FILE, MCODE_LOCK_SNIPPET, RULES_TEXT, build_mcode_locked_config, init_project

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"
GRAPH = "toy_graph:build_graph"


def _init(tmp_path, **kw):
    return init_project(project_dir=tmp_path / "ops", repo_root=FIX, outputs_root=tmp_path / "out",
                        graph_factory=GRAPH, **kw)


# ------------------------------------------------------------------ init
def test_init_writes_expected_files(tmp_path):
    res = _init(tmp_path)
    ops = tmp_path / "ops"
    assert res["skipped"] == [] and len(res["written"]) == 6

    srv = json.loads((ops / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["video-agent"]
    assert srv["args"][:2] == ["-m", "agent_gate.mcp_server"]
    assert srv["args"][srv["args"].index("--graph-factory") + 1] == GRAPH
    assert "--with-human-tool" not in srv["args"]
    assert srv["timeout"] == 150000
    assert srv["env"]["AGENT_GATE_REPO_ROOT"] == FIX.resolve().as_posix()
    assert FIX.resolve().as_posix() in srv["env"]["PYTHONPATH"].split(os.pathsep)
    assert "\\" not in json.dumps([srv["command"], *srv["args"], srv["env"]])      # 全是正斜杠
    assert Path(srv["command"]).exists()

    assert (ops / "CLAUDE.md").read_text(encoding="utf-8") == RULES_TEXT
    assert (ops / "AGENTS.md").read_text(encoding="utf-8") == RULES_TEXT
    assert (ops / MCODE_LOCK_FILE).read_text(encoding="utf-8") == MCODE_LOCK_SNIPPET
    st = json.loads((ops / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert st["permissions"]["deny"] == ["mcp__video-agent__human_resume"]
    hook = st["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "mcp__video-agent__human_resume|Bash|Read|Write|Edit"
    assert (ops / ".claude" / "hooks" / "guard_hook.py").read_text(encoding="utf-8") == \
        Path(guard_hook.__file__).read_text(encoding="utf-8")
    assert "guard_hook.py" in hook["hooks"][0]["command"]


def test_init_does_not_overwrite_without_force(tmp_path):
    _init(tmp_path)
    (tmp_path / "ops" / "CLAUDE.md").write_text("我自己改过的", encoding="utf-8")
    res = _init(tmp_path)
    assert res["written"] == [] and len(res["skipped"]) == 6
    assert (tmp_path / "ops" / "CLAUDE.md").read_text(encoding="utf-8") == "我自己改过的"
    assert len(_init(tmp_path, force=True)["written"]) == 6
    assert (tmp_path / "ops" / "CLAUDE.md").read_text(encoding="utf-8") == RULES_TEXT


def test_init_with_human_tool(tmp_path):
    _init(tmp_path, with_human_tool=True)
    ops = tmp_path / "ops"
    assert "--with-human-tool" in json.loads((ops / ".mcp.json").read_text(encoding="utf-8")
                                             )["mcpServers"]["video-agent"]["args"]
    assert "permissions" not in json.loads((ops / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_init_cli_prints_json(tmp_path):
    cp = subprocess.run([sys.executable, "-m", "agent_gate", "init", "--project-dir", str(tmp_path / "ops"),
                         "--repo-root", str(FIX), "--outputs-root", str(tmp_path / "out"),
                         "--graph-factory", GRAPH],
                        capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60)
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    assert out["ok"] and len(out["written"]) == 6


def _mcp_env(tmp_path) -> dict:
    p = tmp_path / "ops" / ".mcp.json"
    return json.loads(p.read_text(encoding="utf-8"))["mcpServers"]["video-agent"]["env"]


def test_init_env_option(tmp_path):
    _init(tmp_path, env={"STORYLINE_MODE": "auto", "X": "a=b"})
    env = _mcp_env(tmp_path)
    assert env["STORYLINE_MODE"] == "auto" and env["X"] == "a=b"
    assert env["PYTHONUTF8"] == "1" and "AGENT_GATE_REPO_ROOT" in env and "PYTHONPATH" in env   # 自带的三个还在


def test_init_env_rejects_reserved_keys(tmp_path):
    with pytest.raises(ValueError):
        _init(tmp_path, env={"PYTHONPATH": "x"})
    assert not (tmp_path / "ops" / ".mcp.json").exists()                     # 报错时什么都没写


def test_parse_env_pairs():
    from agent_gate.scaffold import parse_env_pairs

    assert parse_env_pairs(["A=1", "B=x=y", " C =", "D=中文"]) == {"A": "1", "B": "x=y", "C": "", "D": "中文"}
    for bad in (["NOEQUALS"], ["=v"], [" =v"]):
        with pytest.raises(ValueError):
            parse_env_pairs(bad)


def test_init_cli_env(tmp_path):
    base = [sys.executable, "-m", "agent_gate", "init", "--project-dir", str(tmp_path / "ops"),
            "--repo-root", str(FIX), "--outputs-root", str(tmp_path / "out"), "--graph-factory", GRAPH]

    def run(extra):
        return subprocess.run(base + extra, capture_output=True, text=True, encoding="utf-8",
                              stdin=subprocess.DEVNULL, timeout=60)

    ok = run(["--env", "STORYLINE_MODE=auto", "--env", "A=b=c"])
    assert ok.returncode == 0, ok.stderr
    env = _mcp_env(tmp_path)
    assert env["STORYLINE_MODE"] == "auto" and env["A"] == "b=c"
    for bad_arg in ("NOEQUALS", "PYTHONPATH=x"):
        bad = run(["--force", "--env", bad_arg])
        assert bad.returncode == 1 and json.loads(bad.stdout)["code"] == "bad_env"


def test_rules_text_mentions_image_budget():
    assert "deferred_tasks" in RULES_TEXT and "images.shown" in RULES_TEXT       # 分批取任务 / 画面没看全时要如实说


def test_rules_text_covers_every_next_action():
    """service 能返回的每个 next_action,操作规则都要讲到;否则 Agent 会遇到不认识的值。"""
    import re

    src = (ROOT / "agent_gate" / "service.py").read_text(encoding="utf-8")
    values = set(re.findall(r'next_action"\] = "(\w+)"', src)) | set(re.findall(r'action = "(\w+)"', src))
    assert values >= {"start_run", "answer_tasks", "wait", "ask_human", "done", "restart_worker", "report_failure"}, values
    for v in values:
        assert f"`{v}`" in RULES_TEXT, f"操作规则里没有讲 next_action={v}"


# ------------------------------------------------------------------ mcode 加锁配置
BASE_CFG = "version: 1\ndefaultModel: mockprov/mock-main\nproviders:\n  - id: mockprov\n    apiKey: sk-x\n"


def test_build_mcode_locked_config_appends_and_keeps_base():
    out = build_mcode_locked_config(BASE_CFG)
    assert out.startswith(BASE_CFG.rstrip())                               # 原配置一字不改
    assert out.endswith(MCODE_LOCK_SNIPPET)
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(out)
    assert doc["defaultModel"] == "mockprov/mock-main"                     # provider / 模型还在
    assert doc["agents"]["default"]["tools"] == [] and doc["agents"]["default"]["builtinTools"] == []
    assert doc["agents"]["default"]["features"] == {"mavis": False, "delegation": False, "webSearch": False}
    assert doc["askUser"] == {"enabled": False} and doc["skills"]["external"] == {"enabled": False}


@pytest.mark.parametrize("key", ["agents", "askUser", "skills"])
def test_build_mcode_locked_config_rejects_duplicate_top_level_key(key):
    with pytest.raises(ValueError) as ei:
        build_mcode_locked_config(BASE_CFG + f"{key}:\n  x: 1\n")
    assert key in str(ei.value)
    assert build_mcode_locked_config(BASE_CFG + f"  {key}: nested is fine\n")   # 缩进的同名键不是顶层键,不拦


def test_mcode_lock_cli(tmp_path):
    base = tmp_path / "config.yaml"
    base.write_text(BASE_CFG, encoding="utf-8")
    out = tmp_path / "sub" / "locked.yaml"
    cmd = [sys.executable, "-m", "agent_gate", "mcode-lock", "--base", str(base), "--out", str(out)]

    def run(extra=()):
        return subprocess.run(cmd + list(extra), capture_output=True, text=True, encoding="utf-8",
                              stdin=subprocess.DEVNULL, timeout=60)

    ok = run()
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["ok"] and out.read_text(encoding="utf-8") == build_mcode_locked_config(BASE_CFG)
    again = run()                                                           # 已存在:不覆盖
    assert again.returncode == 1 and json.loads(again.stdout)["code"] == "exists"
    assert run(["--force"]).returncode == 0
    base.write_text(BASE_CFG + "agents: {}\n", encoding="utf-8")            # 底板里已有 agents:
    bad = run(["--force"])
    assert bad.returncode == 1 and json.loads(bad.stdout)["code"] == "bad_base"
    missing = subprocess.run([*cmd[:-4], "--base", str(tmp_path / "nope.yaml"), "--out", str(tmp_path / "o2.yaml")],
                             capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60)
    assert missing.returncode == 1 and json.loads(missing.stdout)["code"] == "bad_base"


# ------------------------------------------------------------------ 钩子
@pytest.mark.parametrize("event,blocked", [
    ({"tool_name": "mcp__video-agent__human_resume", "tool_input": {"checkpoint": "①"}}, True),
    ({"tool_name": "Bash", "tool_input": {"command": "cat C:\\repo\\checkpoints\\v1.sqlite"}}, True),
    ({"tool_name": "Read", "tool_input": {"file_path": "D:/o/v1/agent_gate/tasks/t_1.result.json"}}, True),
    ({"tool_name": "Write", "tool_input": {"file_path": "D:\\o\\v1\\agent_gate\\status.json", "content": "{}"}}, True),
    ({"tool_name": "Edit", "tool_input": {"file_path": "/o/v1/agent_gate/tasks/x.task.json"}}, True),
    ({"tool_name": "Bash", "tool_input": {"command": "python -m agent_gate resume --thread-id v1 --checkpoint ①"}}, True),
    ({"tool_name": "Bash", "tool_input": {"command": "python -m agent_gate worker --thread-id v1"}}, True),
    ({"tool_name": "Bash", "tool_input": {"command": "python -c \"from agent_gate.runner import spawn_worker\""}}, True),
    ({"tool_name": "Bash", "tool_input": {"command": "python -m agent_gate status --thread-id v1"}}, False),
    ({"tool_name": "Bash", "tool_input": {"command": "python -m agent_gate submit --thread-id v1 "
                                                      "--task-id t_1 --answer-file a.json"}}, False),
    ({"tool_name": "Write", "tool_input": {"file_path": "D:/o/v1/agent_gate/submissions/b1.json",
                                           "content": "{}"}}, False),
    ({"tool_name": "mcp__video-agent__run_status", "tool_input": {"thread_id": "v1"}}, False),
    ({"tool_name": "mcp__video-agent__gate_submit_result", "tool_input": {"answer": "{\"a\": 1}"}}, False),
])
def test_guard_hook_rules(event, blocked):
    assert (guard_hook.check(event) is not None) is blocked


def test_guard_hook_script_protocol():
    hook = Path(guard_hook.__file__)

    def run(stdin: bytes):
        return subprocess.run([sys.executable, str(hook)], input=stdin, capture_output=True, timeout=30)

    bad = run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat /x/v1.sqlite"}}).encode())
    assert bad.returncode == 2 and "禁止".encode() in bad.stderr                      # 退出码 2 + 原因回给模型
    assert run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}).encode()).returncode == 0
    assert run(b"not json").returncode == 0                                            # 看不懂的输入:放行,不挡路
    assert run(b"[1, 2]").returncode == 0


# ------------------------------------------------------------------ worker 的工作目录
def test_cli_resume_refuses_without_tty(tmp_path):
    cp = subprocess.run([sys.executable, "-m", "agent_gate", "resume", "--thread-id", "t1",
                         "--outputs-root", str(tmp_path / "out"), "--checkpoint", "①",
                         "--graph-factory", GRAPH],
                        capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60)
    assert cp.returncode == 1
    assert json.loads(cp.stdout)["code"] == "need_human_terminal"
    assert not (tmp_path / "out" / "t1" / "agent_gate" / "worker.log").exists()      # 根本没拉起 worker


def test_worker_runs_in_repo_root(tmp_path, monkeypatch):
    # PYTHONPATH 里故意不放 fixtures:toy_graph 只能靠 worker 的 cwd(= AGENT_GATE_REPO_ROOT)导入
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("AGENT_GATE_REPO_ROOT", str(FIX))
    monkeypatch.setenv("TOY_CKPT_DIR", str(tmp_path / "ckpt"))
    out = tmp_path / "out"
    mb = Mailbox(runner.gate_root(out, "r1"))
    init = mb.root / "initial_state.json"
    init.write_text(json.dumps({"session_id": "r1", "video_input_path": "", "error_log": []}), encoding="utf-8")
    res = runner.spawn_worker(thread_id="r1", outputs_root=out, graph_factory=GRAPH, initial_state_file=init)
    assert res["ok"], res
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not mb.pending_ids():
            time.sleep(0.2)
        assert mb.pending_ids(), (mb.root / "worker.log").read_text(encoding="utf-8", errors="replace")
    finally:
        pid = (mb.read_status() or {}).get("pid")
        if pid:
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
