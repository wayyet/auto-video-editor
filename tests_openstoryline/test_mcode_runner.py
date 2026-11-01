"""``mcode_runner`` 的单元测试。

覆盖设计文档 §10.1 的 UT-03(不等待 CLI 退出)、UT-09(stderr 大量输出)、
UT-10(取消与超时),外加子进程契约:stdin 写入、退出码、协议错误上抛。

用**真的子进程**测(不是 mock):进程树清理这种行为,只有真跑才验得了。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import sys
import time
from types import SimpleNamespace

import pytest

from open_storyline.mcode_events import (
    EVENT_EXEC_COMPLETED,
    EVENT_EXEC_STARTED,
    MCodeSchemaError,
)
from open_storyline.mcode_runner import (
    MCodeRunner,
    MCodeRunnerError,
    MCodeRunnerTimeout,
)

from conftest import wait_until_dead


# --------------------------------------------------------------------------
# 假 CLI:说 stream-json 的话
# --------------------------------------------------------------------------

_FAKE_CLI_PY = r'''
import json, os, subprocess, sys, time

spec = {}
spec_path = os.environ.get("FAKE_STREAM_SPEC", "")
if spec_path and os.path.isfile(spec_path):
    with open(spec_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)

data = sys.stdin.buffer.read().decode("utf-8", errors="replace")


def emit_line(text):
    """stdout 按 **UTF-8** 写。

    父进程用 ``decode("utf-8")`` 读子进程输出,子进程 ``sys.stdout`` 默认却是
    系统 locale 编码(本机 Windows 是 GBK)—— 两边不一致中文就成乱码。
    """
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


child_pid = None
if spec.get("spawn_child"):
    # 孙进程:验证「超时 / 取消后整棵进程树都得死」。
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid = child.pid

record_path = os.environ.get("FAKE_STREAM_RECORD", "")
if record_path:
    with open(record_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"stdin": data, "pid": os.getpid(), "child_pid": child_pid},
            fh,
            ensure_ascii=False,
        )

if spec.get("stderr_lines"):
    for _ in range(int(spec["stderr_lines"])):
        sys.stderr.write("E" * 200 + "\n")
    sys.stderr.flush()

def emit(seq, event_type, **extra):
    obj = {"schemaVersion":1,"sequence":seq,"timestampMs":1700000000000+seq,
           "runId":"run-1","sessionId":"sess-1","turnId":"turn-1","type":event_type}
    obj.update(extra)
    emit_line(json.dumps(obj, ensure_ascii=False) + "\n")

if spec.get("bad_schema"):
    obj = {"schemaVersion":2,"sequence":1,"timestampMs":1,"runId":"r","sessionId":"s",
           "turnId":"t","type":"exec.started"}
    emit_line(json.dumps(obj) + "\n")
    time.sleep(30)
elif spec.get("no_events"):
    pass
else:
    first = spec.get("first_delta", "第一段")
    emit(1, "exec.started")
    emit(2, "turn.started")
    emit(3, "item.started", item={"id":"u1:message","type":"agent_message","contentDelta":first})
    if spec.get("hold_s"):
        time.sleep(float(spec["hold_s"]))
    emit(4, "item.updated", item={"id":"u1:message","type":"agent_message","contentDelta":"第二段"})
    emit(5, "exec.completed", result={"schemaVersion":1,"type":"exec.result","status":"succeeded",
                                      "output":first + "第二段"})

exit_code = spec.get("exit_code")
if exit_code:
    sys.exit(int(exit_code))
'''


def _make_cli(tmp_path, monkeypatch, name="fake_stream_cli", **spec) -> SimpleNamespace:
    """写一个说 stream-json 的假 CLI。

    返回的 namespace 有 ``path``(可执行文件)、``record_path``(子进程写的运行记录)。
    """
    spec_path = tmp_path / f"{name}_spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    record_path = tmp_path / f"{name}_record.json"
    monkeypatch.setenv("FAKE_STREAM_SPEC", str(spec_path))
    monkeypatch.setenv("FAKE_STREAM_RECORD", str(record_path))

    script_path = tmp_path / f"{name}.py"
    script_path.write_text(_FAKE_CLI_PY, encoding="utf-8")
    if os.name == "nt":
        cli_path = tmp_path / f"{name}.cmd"
        cli_path.write_text(
            "@echo off\r\n" f'"{sys.executable}" "{script_path}" %*\r\n',
            encoding="ascii",
        )
    else:
        cli_path = script_path
        script_path.write_text(
            "#!/usr/bin/env python3\n" + _FAKE_CLI_PY, encoding="utf-8"
        )
        cli_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
    return SimpleNamespace(path=str(cli_path), record_path=record_path, spec_path=spec_path)


def _read_record(cli: SimpleNamespace) -> dict:
    assert cli.record_path.is_file(), f"假 CLI 没有写记录文件: {cli.record_path}"
    return json.loads(cli.record_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------

async def test_yields_events_and_reports_exit_code(tmp_path, monkeypatch):
    cli = _make_cli(tmp_path, monkeypatch)
    runner = MCodeRunner([cli.path], stdin_text="提示词", timeout_s=30)
    events = []
    async for event in runner.stream():
        events.append(event)

    assert [e.type for e in events] == [
        EVENT_EXEC_STARTED,
        "turn.started",
        "item.started",
        "item.updated",
        EVENT_EXEC_COMPLETED,
    ]
    assert runner.result.exit_code == 0
    assert runner.result.completed is True
    assert runner.result.events == len(events)
    assert runner.result.truncated is False


async def test_first_event_arrives_before_process_exit(tmp_path, monkeypatch):
    """**UT-03**:首段文本必须在 CLI 进程退出**之前**到达。"""
    cli = _make_cli(tmp_path, monkeypatch, hold_s=2.0)
    runner = MCodeRunner([cli.path], stdin_text="提示词", timeout_s=30)

    started = time.monotonic()
    first_at = None
    deltas = []
    async for event in runner.stream():
        if first_at is None:
            first_at = time.monotonic() - started
        if event.text_delta:
            deltas.append(event.text_delta)
    elapsed = time.monotonic() - started

    assert first_at is not None
    assert first_at < 1.5, f"首段文本等了 {first_at:.2f}s,像是等进程退出才给的"
    assert elapsed >= 2.0, "进程本身应该跑了 2 秒"
    assert deltas == ["第一段", "第二段"]


async def test_writes_prompt_to_stdin(tmp_path, monkeypatch):
    cli = _make_cli(tmp_path, monkeypatch)
    runner = MCodeRunner([cli.path], stdin_text="这是提示词", timeout_s=30)
    async for _ in runner.stream():
        pass
    record = _read_record(cli)
    assert "这是提示词" in record["stdin"]


async def test_text_deltas_concatenate_to_final_output(tmp_path, monkeypatch):
    """增量拼接 == ``exec.completed.result.output``。"""
    cli = _make_cli(tmp_path, monkeypatch, first_delta="甲")
    runner = MCodeRunner([cli.path], stdin_text="x", timeout_s=30)
    deltas = []
    final = None
    async for event in runner.stream():
        if event.text_delta:
            deltas.append(event.text_delta)
        if event.is_exec_completed:
            final = event.final_output
    assert "".join(deltas) == final


# --------------------------------------------------------------------------
# UT-09 stderr 大量输出
# --------------------------------------------------------------------------

async def test_large_stderr_is_drained_and_bounded(tmp_path, monkeypatch):
    """stderr 塞满管道也不能把 stdout 饿死,内存占用要有上限。"""
    cli = _make_cli(tmp_path, monkeypatch, stderr_lines=3000)
    runner = MCodeRunner([cli.path], stdin_text="x", timeout_s=60)
    events = []
    async for event in runner.stream():
        events.append(event)

    assert [e.type for e in events] == [
        EVENT_EXEC_STARTED,
        "turn.started",
        "item.started",
        "item.updated",
        EVENT_EXEC_COMPLETED,
    ]
    # stderr 只保留尾部若干行,不能把 60 万字符全留在内存里
    assert len(runner.stderr_tail) < 50 * 300
    assert runner.result.exit_code == 0


# --------------------------------------------------------------------------
# UT-10 超时 / 取消
# --------------------------------------------------------------------------

async def test_timeout_kills_process_tree(tmp_path, monkeypatch):
    cli = _make_cli(tmp_path, monkeypatch, hold_s=60, spawn_child=True)
    runner = MCodeRunner([cli.path], stdin_text="x", timeout_s=2)
    with pytest.raises(MCodeRunnerTimeout):
        async for _ in runner.stream():
            pass

    record = _read_record(cli)
    assert wait_until_dead(record["pid"]), "CLI 主进程应该已经被杀掉"
    assert wait_until_dead(record["child_pid"]), "CLI 的子进程也必须被杀(不留孤儿)"


async def test_cancel_kills_process_tree(tmp_path, monkeypatch):
    cli = _make_cli(tmp_path, monkeypatch, hold_s=60, spawn_child=True)
    runner = MCodeRunner([cli.path], stdin_text="x", timeout_s=120)

    async def consume():
        async for _ in runner.stream():
            pass

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(1.0)  # 等子进程真的起来
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = _read_record(cli)
    assert wait_until_dead(record["pid"]), "取消后 CLI 主进程应该已经被杀掉"
    assert wait_until_dead(record["child_pid"]), "取消后子进程也必须被杀"


# --------------------------------------------------------------------------
# 错误路径
# --------------------------------------------------------------------------

async def test_schema_error_is_raised(tmp_path, monkeypatch):
    cli = _make_cli(tmp_path, monkeypatch, bad_schema=True)
    runner = MCodeRunner([cli.path], stdin_text="x", timeout_s=30)
    with pytest.raises(MCodeSchemaError):
        async for _ in runner.stream():
            pass


async def test_nonzero_exit_without_result_raises(tmp_path, monkeypatch):
    cli = _make_cli(tmp_path, monkeypatch, no_events=True, exit_code=3)
    runner = MCodeRunner([cli.path], stdin_text="x", timeout_s=30)
    with pytest.raises(MCodeRunnerError):
        async for _ in runner.stream():
            pass


async def test_nonzero_exit_with_result_is_not_fatal(tmp_path, monkeypatch):
    """拿到了 exec.completed 就以它为准,退出码非零不额外报错。"""
    cli = _make_cli(tmp_path, monkeypatch, exit_code=3)
    runner = MCodeRunner([cli.path], stdin_text="x", timeout_s=30)
    events = []
    async for event in runner.stream():
        events.append(event)
    assert runner.result.exit_code == 3
    assert runner.result.completed is True
    assert events[-1].final_output


async def test_missing_executable_raises(tmp_path):
    runner = MCodeRunner([str(tmp_path / "不存在的可执行文件.cmd")], timeout_s=5)
    with pytest.raises(MCodeRunnerError):
        async for _ in runner.stream():
            pass


async def test_consumer_break_terminates_child(tmp_path, monkeypatch):
    """消费方提前 break 时,子进程也要被收掉。

    注意:必须走 ``async with``(或显式 ``aclose()``)。
    只写 ``async for ... break`` 的话,异步生成器要等 GC 才关闭,
    进程树会晚一会儿才被回收。
    """
    cli = _make_cli(tmp_path, monkeypatch, hold_s=60, spawn_child=True)
    runner = MCodeRunner([cli.path], stdin_text="x", timeout_s=120)

    async with runner:
        async for _ in runner.stream():
            break

    record = _read_record(cli)
    assert wait_until_dead(record["pid"]), "提前退出后 CLI 主进程应该已经被杀掉"
    assert wait_until_dead(record["child_pid"]), "提前退出后子进程也必须被杀"


async def test_explicit_aclose_terminates_child(tmp_path, monkeypatch):
    """不用 ``async with`` 时,显式 ``aclose()`` 也要收掉进程树。"""
    cli = _make_cli(tmp_path, monkeypatch, hold_s=60, spawn_child=True)
    runner = MCodeRunner([cli.path], stdin_text="x", timeout_s=120)

    async for _ in runner.stream():
        break
    await runner.aclose()

    record = _read_record(cli)
    assert wait_until_dead(record["pid"]), "aclose 后 CLI 主进程应该已经被杀掉"
    assert wait_until_dead(record["child_pid"]), "aclose 后子进程也必须被杀"


async def test_runner_cannot_be_reused(tmp_path, monkeypatch):
    """一个 runner 只能跑一次,防止上一轮流被孤立。"""
    cli = _make_cli(tmp_path, monkeypatch)
    runner = MCodeRunner([cli.path], stdin_text="x", timeout_s=30)
    first = runner.stream()
    with pytest.raises(MCodeRunnerError):
        runner.stream()
    await runner.aclose()
    with contextlib.suppress(Exception):
        await first.aclose()


# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------

def test_empty_command_rejected():
    with pytest.raises(ValueError):
        MCodeRunner([])
