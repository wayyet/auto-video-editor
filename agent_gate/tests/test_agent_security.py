"""Agent 程序的权限、启动竞态、工具超时、非交互终端。真实 claude / mcode + 本地假模型,无外网。

每个测试只验证一件事,「对照」那一段是为了证明测试本身不是空转(例如:不加开关时,工具确实多出来)。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from support import (
    CLAUDE_BIN,
    MCODE_BIN,
    claude_env,
    mcode_env,
    needs_claude,
    needs_mcode,
    tool_names,
    tool_results,
)

from agent_gate.cli_client import run_process
from agent_gate.launcher import ClaudeLauncher, McodeLauncher, summarize_output
from agent_gate.scaffold import init_project

HERE = Path(__file__).resolve().parent
FIX = HERE.parent / "fixtures"
GRAPH = "toy_graph:build_graph"
SLOW = HERE / "slow_mcp_server.py"
EXPECTED_TOOLS = sorted(f"mcp__video-agent__{n}" for n in (
    "run_start", "run_status", "gate_next_tasks", "gate_get_task", "gate_submit_result", "gate_submit_batch"))

# 假模型的脚本:先调一次 run_status,再收工
STATUS_SCRIPT = {"main_models": ["mock-main"], "default_text": "结束", "anthropic": [
    {"text": "看状态", "tools": [{"name_regex": "run_status$", "input": {"thread_id": "x1"}}]}, {"text": "结束"}]}


def _ops(tmp_path: Path, **kw) -> Path:
    ops = tmp_path / "ops"
    init_project(project_dir=ops, repo_root=FIX, outputs_root=tmp_path / "out", graph_factory=GRAPH,
                 python=sys.executable, **kw)
    return ops


def _edit_server(ops: Path, fn) -> None:
    p = ops / ".mcp.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    fn(cfg["mcpServers"]["video-agent"])
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _claude(ops: Path, tmp_path: Path, port: int, **kw) -> ClaudeLauncher:
    return ClaudeLauncher(project_dir=ops, executable=CLAUDE_BIN, model="mock-main", session_timeout_s=90,
                          clean_env=True, extra_env=claude_env(tmp_path, port), **kw)


def _nap_project(tmp_path: Path, timeout_ms: int) -> Path:
    """只有一个慢工具 nap 的 MCP 服务,服务名仍叫 video-agent(这样 --allowedTools 的通配符能命中)。"""
    ops = tmp_path / "nap_ops"
    ops.mkdir()
    (ops / ".mcp.json").write_text(json.dumps({"mcpServers": {"video-agent": {
        "command": Path(sys.executable).as_posix(), "args": [SLOW.as_posix()], "timeout": timeout_ms}}}),
        encoding="utf-8")
    return ops


def _nap_script(seconds: float) -> dict:
    step = {"text": "睡一会", "tools": [{"name_regex": "nap$", "input": {"seconds": seconds}}]}
    return {"main_models": ["mock-main"], "default_text": "结束", "anthropic": [step, {"text": "结束"}],
            "openai": [step, {"text": "结束"}]}


# ------------------------------------------------------------------ claude:工具面
@needs_claude
def test_claude_sees_only_video_agent_tools_despite_permissive_user_config(tmp_path, mock):
    ops = _ops(tmp_path)
    # 用户自己的 Claude 配置很宽松:放行 Bash / 读写,还配了另一个 MCP 服务
    cfg_dir = tmp_path / "home_cc" / ".claude"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": ["Bash", "Read", "Write", "Edit", "mcp__evil__nap"]}}), encoding="utf-8")
    (cfg_dir / ".claude.json").write_text(json.dumps(
        {"mcpServers": {"evil": {"command": sys.executable, "args": [str(SLOW)]}}}), encoding="utf-8")

    # 对照:只靠 dontAsk + allowedTools,模型的工具列表里仍有 Bash 和别的 MCP 服务
    old = mock(STATUS_SCRIPT)
    env = _claude(ops, tmp_path, old.port).env()
    run_process([CLAUDE_BIN, "-p", "--output-format", "json", "--model", "mock-main",
                 "--permission-mode", "dontAsk", "--allowedTools", "mcp__video-agent__*",
                 "--max-turns", "5", "--no-session-persistence"],
                "试", cwd=str(ops), env=env, timeout_s=90, label="claude-old")
    old_names = tool_names(old.main_requests()[0]["body"])
    assert "Bash" in old_names and "mcp__evil__nap" in old_names

    # 正式启动器:只剩 6 个 video-agent 工具
    new = mock(STATUS_SCRIPT)
    res = _claude(ops, tmp_path, new.port).run("试")
    assert res.returncode == 0, res.stderr
    assert sorted(tool_names(new.main_requests()[0]["body"])) == EXPECTED_TOOLS
    name, is_err, _ = tool_results(new.main_requests()[-1]["body"])[0]
    assert name == "mcp__video-agent__run_status" and not is_err


@needs_claude
def test_claude_dontask_refuses_human_tool(tmp_path, mock):
    ops = _ops(tmp_path, with_human_tool=True)       # 服务注册了 human_resume,settings 里也没有 deny 规则
    srv = mock({"main_models": ["mock-main"], "default_text": "结束", "anthropic": [
        {"text": "同一轮里调两个工具", "tools": [
            {"name_regex": "run_status$", "input": {"thread_id": "x1"}},
            {"name_regex": "human_resume$", "input": {"thread_id": "x1", "checkpoint": "①", "value": "true"}}]},
        {"text": "结束"}]})
    res = _claude(ops, tmp_path, srv.port).run("试")
    assert "mcp__video-agent__human_resume" in tool_names(srv.main_requests()[0]["body"])     # 工具确实可见
    assert summarize_output("claude", res.stdout)["permission_denials"] == ["mcp__video-agent__human_resume"]
    by_name = {n: err for n, err, _ in tool_results(srv.main_requests()[-1]["body"])}
    assert by_name["mcp__video-agent__run_status"] is False                                     # 对照:同轮的 run_status 放行


@needs_claude
def test_claude_deny_rule_hides_human_tool(tmp_path, mock):
    ops = _ops(tmp_path)                              # 默认带 deny 规则
    _edit_server(ops, lambda s: s["args"].append("--with-human-tool"))   # 但让服务照样注册这个工具
    srv = mock(STATUS_SCRIPT)
    _claude(ops, tmp_path, srv.port).run("试")
    names = tool_names(srv.main_requests()[0]["body"])
    assert "mcp__video-agent__run_status" in names
    assert not any(n.endswith("human_resume") for n in names)


@needs_claude
def test_claude_hook_blocks_direct_access_to_gate_files(tmp_path, mock):
    ops = _ops(tmp_path)
    secret = tmp_path / "ckpt" / "v1.sqlite"
    srv = mock({"main_models": ["mock-main"], "default_text": "结束", "anthropic": [
        {"text": "试两条命令", "tools": [
            {"name": "Bash", "input": {"command": f"cat {secret}", "description": "read db"}},
            {"name": "Bash", "input": {"command": "echo hello-from-bash", "description": "echo"}}]},
        {"text": "结束"}]})
    env = _claude(ops, tmp_path, srv.port).env()
    # 这里故意放行 Bash,看钩子能不能挡住
    run_process([CLAUDE_BIN, "-p", "--output-format", "json", "--model", "mock-main",
                 "--permission-mode", "dontAsk", "--allowedTools", "Bash", "--strict-mcp-config",
                 "--max-turns", "5", "--no-session-persistence"],
                "试", cwd=str(ops), env=env, timeout_s=90, label="claude-hook")
    results = tool_results(srv.main_requests()[-1]["body"])
    assert len(results) == 2
    assert "禁止" in results[0][2]                      # 钩子的原因文字回到了模型
    assert "hello-from-bash" in results[1][2]           # 不命中规则的命令照常执行


# ------------------------------------------------------------------ claude:启动竞态与工具超时
def _mcp_tools(srv) -> list[str]:
    return sorted(n for n in tool_names(srv.main_requests()[0]["body"]) if n.startswith("mcp__"))


@needs_claude
def test_claude_slow_mcp_server_startup(tmp_path, mock):
    """MCP 服务启动要 4 秒:自动加载 .mcp.json 时 -p 首轮请求里一个 MCP 工具都没有(竞态)。
    启动器用 --mcp-config 显式指定配置,首轮就有工具;再加 CLAUDE_CODE_MCP_STARTUP_WAIT_MS 作保险。"""
    ops = _ops(tmp_path)
    _edit_server(ops, lambda s: s.update(args=["-c", "import time; time.sleep(4); "
                                                     "from agent_gate.mcp_server import main; main()"] + s["args"][2:]))
    env = _claude(ops, tmp_path, 1).env()
    env.pop("CLAUDE_CODE_MCP_STARTUP_WAIT_MS")

    # 对照:自动加载 + 不设等待变量 → 0 个工具
    auto = mock(STATUS_SCRIPT)
    env.update(claude_env(tmp_path, auto.port))
    run_process([CLAUDE_BIN, "-p", "--output-format", "json", "--model", "mock-main",
                 "--permission-mode", "dontAsk", "--allowedTools", "mcp__video-agent__*",
                 "--max-turns", "5", "--no-session-persistence"],
                "试", cwd=str(ops), env=env, timeout_s=90, label="claude-auto-load")
    assert _mcp_tools(auto) == []

    # 启动器(--mcp-config)不设等待变量也行
    explicit = mock(STATUS_SCRIPT)
    env.update(claude_env(tmp_path, explicit.port))
    launcher = _claude(ops, tmp_path, explicit.port)
    run_process(launcher.command(), "试", cwd=str(ops), env=env, timeout_s=90, label="claude-explicit")
    assert _mcp_tools(explicit) == EXPECTED_TOOLS

    # 启动器的默认环境(带等待变量)
    full = mock(STATUS_SCRIPT)
    assert _claude(ops, tmp_path, full.port).run("试").returncode == 0
    assert _mcp_tools(full) == EXPECTED_TOOLS


@needs_claude
def test_claude_mcp_tool_timeout_comes_from_mcp_json(tmp_path, mock):
    ops = _nap_project(tmp_path, timeout_ms=3000)
    fast = mock(_nap_script(1))
    _claude(ops, tmp_path, fast.port).run("试")
    assert "slept 1.0" in tool_results(fast.main_requests()[-1]["body"])[0][2]

    slow = mock(_nap_script(6))
    _claude(ops, tmp_path, slow.port).run("试")
    assert "timed out after 3s" in tool_results(slow.main_requests()[-1]["body"])[0][2]


# ------------------------------------------------------------------ mcode
@needs_mcode
def test_mcode_mcp_tool_timeout_comes_from_mcp_json(tmp_path, mock):
    ops = _nap_project(tmp_path, timeout_ms=3000)
    fast = mock(_nap_script(1))
    McodeLauncher(project_dir=ops, executable=MCODE_BIN, session_timeout_s=90, clean_env=True,
                  extra_env=mcode_env(tmp_path, fast.port)).run("试")
    assert "slept 1.0" in tool_results(fast.main_requests()[-1]["body"])[0][2]

    slow = mock(_nap_script(6))
    McodeLauncher(project_dir=ops, executable=MCODE_BIN, session_timeout_s=90, clean_env=True,
                  extra_env=mcode_env(tmp_path, slow.port)).run("试")
    assert "timed out" in tool_results(slow.main_requests()[-1]["body"])[0][2].lower()


@needs_mcode
@pytest.mark.skipif(os.name == "nt", reason="脚本里用了 POSIX 的 env 命令")
def test_mcode_shell_cannot_resume_a_human_gate(tmp_path, mock):
    out = tmp_path / "out"
    cmd = (f"env PYTHONPATH={os.environ['PYTHONPATH']} {sys.executable} -m agent_gate resume "
           f"--thread-id t1 --outputs-root {out} --checkpoint ① --graph-factory {GRAPH}")
    step = {"text": "试试放行", "tools": [{"name": "bash", "input": {"command": cmd}}]}
    srv = mock({"main_models": ["mock-main"], "default_text": "结束", "openai": [step, {"text": "结束"}]})
    ops = tmp_path / "plain_ops"
    ops.mkdir()
    # mcode 没有「关掉 bash」的开关:这里 bash 是放行的,拦住放行的是 resume 命令自己的「必须有终端」检查
    McodeLauncher(project_dir=ops, executable=MCODE_BIN, permission="full", session_timeout_s=90, clean_env=True,
                  extra_env=mcode_env(tmp_path, srv.port)).run("试")
    text = tool_results(srv.main_requests()[-1]["body"])[0][2]
    assert "need_human_terminal" in text
    assert not (out / "t1" / "agent_gate" / "worker.log").exists()
