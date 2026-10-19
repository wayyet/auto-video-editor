"""方案 B 适配器测试:真实的 claude / mcode 程序 + 本地假模型服务(无真实模型、无外网)。

找不到 claude 或 mcode 时,对应测试自动跳过(见 support.py)。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from support import (
    CLAUDE_BIN,
    MCODE_BIN,
    claude_env,
    mcode_env,
    needs_claude,
    needs_mcode,
    tool_names as _tool_names,
)

from agent_gate import runner, service
from agent_gate.cli_client import (
    CLIBackendFailed,
    CLIBackendInvalid,
    CLIBackendTimeout,
    ClaudeCLIClient,
    McodeCLIClient,
)
from agent_gate.mailbox import Mailbox

CAP = {
    "type": "object",
    "properties": {"caption": {"type": "string"}, "aes_score": {"type": "number"}},
    "required": ["caption", "aes_score"],
    "additionalProperties": False,
}
PROMPT = "请描述镜头 c1 的画面,并给出 0-10 的美学评分。"


def claude_client(tmp_path: Path, port: int, **kw) -> ClaudeCLIClient:
    return ClaudeCLIClient(executable=CLAUDE_BIN, model="mock-main", timeout_s=120,
                           extra_env=claude_env(tmp_path, port), clean_env=True, **kw)


def mcode_client(tmp_path: Path, port: int, **kw) -> McodeCLIClient:
    return McodeCLIClient(executable=MCODE_BIN, timeout_s=120, extra_env=mcode_env(tmp_path, port),
                          clean_env=True, permission="off", **kw)


# ------------------------------------------------------------------ 纯逻辑(不起进程)
def test_command_shapes(tmp_path):
    c = ClaudeCLIClient(executable="claude", model="m1")
    cmd = c.build_command("系统提示", CAP)
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""                      # 不给任何内置工具
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == CAP
    assert cmd[cmd.index("--model") + 1] == "m1"

    stdin = json.loads(ClaudeCLIClient.build_stdin("题目", []))
    assert stdin["message"]["content"] == [{"type": "text", "text": "题目"}]

    m = McodeCLIClient(executable="mcode", permission="smart", timeout_s=90)       # 默认 lightweight
    cmd = m.build_command(tmp_path, "系统提示", ["/a.jpg", "/b.jpg"], CAP)
    assert cmd[:4] == ["mcode", "exec", "--input", "-"]
    assert cmd.count("--file") == 2
    assert cmd[cmd.index("--timeout") + 1] == "90s"
    assert json.loads((tmp_path / "output_schema.json").read_text(encoding="utf-8")) == CAP
    assert cmd[cmd.index("--mode") + 1] == "lightweight"
    assert "--system-prompt-file" not in cmd                                      # lightweight 会忽略它,不写
    assert "系统提示" in m.compose_prompt("系统提示", "题目") and "题目" in m.compose_prompt("系统提示", "题目")

    std = McodeCLIClient(executable="mcode", permission="smart", timeout_s=90, lightweight=False)
    cmd = std.build_command(tmp_path, "系统提示", [], None)
    assert "--mode" not in cmd
    assert (tmp_path / "system_prompt.txt").read_text(encoding="utf-8") == "系统提示"
    assert std.compose_prompt("系统提示", "题目") == "题目"


def test_missing_executable():
    with pytest.raises(CLIBackendFailed):
        ClaudeCLIClient(executable="definitely-not-a-real-binary-xyz").chat(
            system_prompt="s", user_prompt="u")


@pytest.mark.skipif(os.name == "nt", reason="用 shell 脚本造假进程,Windows 上不跑")
def test_timeout_kills_whole_process_tree(tmp_path):
    fake = tmp_path / "fake_claude.sh"
    fake.write_text("#!/bin/sh\nsleep 31.4159 &\nsleep 31.4159\n", encoding="utf-8")
    fake.chmod(0o755)
    c = ClaudeCLIClient(executable=str(fake), timeout_s=1.5, max_attempts=1)
    t0 = time.monotonic()
    with pytest.raises(CLIBackendTimeout):
        c.chat(system_prompt="s", user_prompt="u")
    assert time.monotonic() - t0 < 10
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "sleep 31.4159"], capture_output=True, text=True)
    assert left.stdout.strip() == ""                                 # 子进程也被杀掉了


# ------------------------------------------------------------------ claude
@needs_claude
def test_claude_structured_json(tmp_path, mock):
    srv = mock({"main_models": ["mock-main"], "policy": "structured_answer"})
    out = claude_client(tmp_path, srv.port).chat(
        system_prompt="你是视频镜头理解助手。只输出 JSON。", user_prompt=PROMPT, json_schema=CAP)
    assert json.loads(out) == {"caption": "画面:测试镜头", "aes_score": 8.0}
    body = srv.main_requests()[0]["body"]
    assert _tool_names(body) == ["StructuredOutput"]                 # 只有结构化输出工具
    assert "你是视频镜头理解助手" in json.dumps(body["system"], ensure_ascii=False)


@needs_claude
def test_claude_video_frames_go_inline(tmp_path, mock):
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    srv = mock({"main_models": ["mock-main"], "policy": "structured_answer"})
    claude_client(tmp_path, srv.port, frames_per_clip=3).chat(
        system_prompt="s", user_prompt=PROMPT, json_schema=CAP,
        media=[{"path": str(video), "in_sec": 0.0, "out_sec": 2.0}])
    first = srv.main_requests()[0]["body"]["messages"][0]["content"]
    assert [b["type"] for b in first].count("image") == 3


@needs_claude
def test_claude_plain_text_answer(tmp_path, mock):
    srv = mock({"main_models": ["mock-main"], "anthropic": [{"text": "两个好镜头"}]})
    assert claude_client(tmp_path, srv.port).chat(system_prompt="s", user_prompt="u") == "两个好镜头"


@needs_claude
def test_claude_plain_json_text_is_still_validated(tmp_path, mock):
    # 模型没调 StructuredOutput、只回了文本 JSON:适配器必须自己解析并校验
    srv = mock({"main_models": ["mock-main"], "policy": "json_text"})
    out = claude_client(tmp_path, srv.port).chat(system_prompt="s", user_prompt=PROMPT, json_schema=CAP)
    assert json.loads(out)["aes_score"] == 8.0


@needs_claude
def test_claude_gives_up_when_schema_never_satisfied(tmp_path, mock):
    bad = {"tools": [{"name": "StructuredOutput", "input": {"caption": 1}}]}
    srv = mock({"main_models": ["mock-main"], "anthropic": [bad] * 8})
    with pytest.raises(CLIBackendInvalid) as ei:
        claude_client(tmp_path, srv.port).chat(system_prompt="s", user_prompt=PROMPT, json_schema=CAP)
    assert "error_max_structured_output_retries" in str(ei.value)


# ------------------------------------------------------------------ mcode
@needs_mcode
def test_mcode_structured_json(tmp_path, mock):
    srv = mock({"main_models": ["mock-main"], "policy": "json_text"})
    out = mcode_client(tmp_path, srv.port).chat(
        system_prompt="你是视频镜头理解助手。只输出 JSON。", user_prompt=PROMPT, json_schema=CAP)
    assert json.loads(out) == {"caption": "画面:测试镜头", "aes_score": 8}
    body = srv.main_requests()[-1]["body"]
    assert body["response_format"]["json_schema"]["strict"] is True   # 走原生严格结构化输出
    assert _tool_names(body) == []                                    # lightweight:请求里没有任何工具
    users = json.dumps([m for m in body["messages"] if m["role"] == "user"], ensure_ascii=False)
    assert "你是视频镜头理解助手" in users                              # 系统提示词被并进了用户提示


@needs_mcode
def test_mcode_standard_mode_keeps_system_prompt_but_has_tools(tmp_path, mock):
    srv = mock({"main_models": ["mock-main"], "policy": "json_text"})
    out = mcode_client(tmp_path, srv.port, lightweight=False).chat(
        system_prompt="你是视频镜头理解助手。只输出 JSON。", user_prompt=PROMPT, json_schema=CAP)
    assert json.loads(out)["caption"] == "画面:测试镜头"
    body = srv.main_requests()[-1]["body"]
    assert "你是视频镜头理解助手" in json.dumps(body["messages"][0], ensure_ascii=False)   # 在系统消息里
    assert "bash" in _tool_names(body) and len(_tool_names(body)) >= 10                   # 对照:标准模式带一堆内置工具


@needs_mcode
def test_mcode_images_attached(tmp_path, mock):
    imgs = []
    for i in (1, 2):
        p = tmp_path / f"f{i}.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=1",
                        "-frames:v", "1", str(p)], check=True)
        imgs.append({"path": str(p)})
    srv = mock({"main_models": ["mock-main"], "policy": "json_text"})
    mcode_client(tmp_path, srv.port).chat(system_prompt="s", user_prompt=PROMPT, json_schema=CAP, media=imgs)
    user = [m for m in srv.main_requests()[-1]["body"]["messages"] if m["role"] == "user"][0]["content"]
    assert [b["type"] for b in user].count("image_url") == 2


@needs_mcode
def test_mcode_retries_with_hint_after_invalid_answer(tmp_path, mock):
    # provider add --use 会先消耗 1 次;所以 bad_first=2 = 1 次设置 + 第 1 次真实调用不合格
    srv = mock({"main_models": ["mock-main"], "policy": "flaky_json_text", "bad_first": 2})
    c = mcode_client(tmp_path, srv.port)
    out = c.chat(system_prompt="s", user_prompt=PROMPT, json_schema=CAP)
    assert json.loads(out)["caption"] == "画面:测试镜头"
    reqs = srv.main_requests()
    assert len(reqs) == 3                                             # 设置 1 + 不合格 1 + 重试 1
    assert "上一次的答案不合格" in json.dumps(reqs[-1]["body"]["messages"], ensure_ascii=False)


@needs_mcode
def test_mcode_gives_up_when_answers_never_valid(tmp_path, mock):
    srv = mock({"main_models": ["mock-main"], "openai": [], "default_text": "{\"caption\": 1}"})
    with pytest.raises(CLIBackendInvalid):
        mcode_client(tmp_path, srv.port, max_attempts=2).chat(
            system_prompt="s", user_prompt=PROMPT, json_schema=CAP)


# ------------------------------------------------------------------ 方案 B 整条路径:start --backend → worker → 玩具图
GRAPH = "toy_graph:build_graph"


def _wait_state(mb: Mailbox, thread_id: str, want: str, timeout_s: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = service.status_summary(mb, thread_id=thread_id)
        if last["worker_state"] == want:
            return last
        assert last["worker_state"] != "failed", last
        time.sleep(0.5)
    raise AssertionError(f"{timeout_s:.0f}s 内没等到 {want}:{last}")


def _run_toy_graph_with_backend(tmp_path, monkeypatch, backend: str) -> None:
    """worker 用 --backend <claude-cli|mcode-cli> 跑玩具图:每次 chat() 拉起一个真实的 claude / mcode(只连假模型)。"""
    monkeypatch.setenv("TOY_CKPT_DIR", str(tmp_path / "ckpt"))
    outputs = tmp_path / "outputs"
    mb = Mailbox(runner.gate_root(outputs, "b1"))
    init = mb.root / "initial_state.json"      # 初始 state 放在 gate_root 里(工具默认也这么写)
    init.write_text(json.dumps({"session_id": "b1", "video_input_path": "", "error_log": []}), encoding="utf-8")

    res = runner.spawn_worker(thread_id="b1", outputs_root=outputs, graph_factory=GRAPH,
                              initial_state_file=init, backend=backend, mode="start")
    assert res["ok"], res
    st = _wait_state(mb, "b1", "waiting_human")
    assert st["interrupts"][0]["checkpoint"] == "①"
    assert st["pending_tasks"] == 0                       # 方案 B 里 Agent 不领任务,信箱里没有任务

    res = runner.spawn_worker(thread_id="b1", outputs_root=outputs, graph_factory=GRAPH,
                              resume_values={"①": True}, backend=backend, mode="resume")
    assert res["ok"], res
    _wait_state(mb, "b1", "completed")

    graph = runner.load_callable(GRAPH)(thread_id="b1")
    v = graph.get_state({"configurable": {"thread_id": "b1"}}).values
    assert all(c["aes_score"] == 8.0 for c in v["captions"].values())
    assert v["kept"] == ["c1", "c3"] and v["transition"] == "fade"
    assert v["script"] == "两个好镜头" and v["final"] == {"summary": "ok"}
    assert "filter_fallback" not in v["status_log"] and v["error_log"] == []     # 没有任何一次降级


@needs_claude
def test_worker_with_claude_cli_backend(tmp_path, mock, monkeypatch):
    srv = mock({"main_models": ["mock-main"], "policy": "structured_answer"})
    for k, v in claude_env(tmp_path, srv.port).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AGENT_GATE_CLAUDE_BIN", CLAUDE_BIN)
    monkeypatch.setenv("AGENT_GATE_CLI_MODEL", "mock-main")
    _run_toy_graph_with_backend(tmp_path, monkeypatch, "claude-cli")
    reqs = srv.main_requests()
    assert len(reqs) >= 7                                 # 7 次 chat,每次至少 1 个主请求
    assert all(set(_tool_names(r["body"])) <= {"StructuredOutput"} for r in reqs)   # 任何请求里都没有别的工具


@needs_mcode
def test_worker_with_mcode_cli_backend(tmp_path, mock, monkeypatch):
    srv = mock({"main_models": ["mock-main"], "policy": "json_text"})
    for k, v in mcode_env(tmp_path, srv.port).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AGENT_GATE_MCODE_BIN", MCODE_BIN)
    _run_toy_graph_with_backend(tmp_path, monkeypatch, "mcode-cli")
    reqs = srv.main_requests()
    assert len(reqs) >= 8                                 # provider add --use 的测试请求 1 次 + 7 次 chat
    # lightweight:没有任何工具。唯一的例外:mcode 自己会为每个会话多发一个「生成会话标题」的请求,
    # 里面只带它的内部工具 submit_session_title(实测)
    offenders = [(i, _tool_names(r["body"])) for i, r in enumerate(reqs)
                 if set(_tool_names(r["body"])) - {"submit_session_title"}]
    assert not offenders, offenders
