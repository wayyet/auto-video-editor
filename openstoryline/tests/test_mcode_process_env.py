"""MCodeChatModel 子进程受控环境快照回归(阶段 D-T1/D-T2,执行设计 §10.2 P2/P3)。

覆盖:
- P3  同步 ``_generate`` / 异步 ``_agenerate`` / 能力探测
      ``_probe_stream_json_support`` / 流式 ``_astream_stream_json`` 四个子进程
      创建点,全部显式收到**同一份**构造时环境快照;
- P2  快照语义:构造之后全局环境再怎么变,子进程拿到的仍是构造瞬间的值;
- D-T1 ``build_process_env``:Windows 基础变量(PATH/SYSTEMROOT/TEMP/TMP/
      USERPROFILE)缺失时从当前进程环境补齐,显式传入的键原样保留。

测法:用一个自带的假 CLI(真子进程),把「本进程实际收到的环境变量」按
``ENV_PROBE_KEYS`` 白名单写进记录文件 —— 断言的是**真实传播结果**,不是桩。
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from open_storyline.mcode_chat_model import (
    BASE_ENV_KEYS,
    MCodeChatModel,
    build_process_env,
)

#: 探测用的金丝雀环境变量(只在测试进程里设置,不碰真实配置)。
CANARY = "OPENSTORYLINE_ENV_CANARY"

#: 环境探测白名单,通过 ``ENV_PROBE_KEYS`` 传给假 CLI(``|`` 分隔)。
_PROBE_KEYS = "|".join((CANARY, "PATH", "SYSTEMROOT", "TEMP", "USERPROFILE"))

#: 自带假 CLI:``exec --help`` 报告支持 stream-json;正式运行按 ``--output-format``
#: 输出合法结果,并把本进程收到的探测环境写进记录文件。
#: 事件形状与 ``conftest._FAKE_STREAM_CLI_PY`` 的最小事件序列保持一致。
_FAKE_ENV_CLI_PY = r'''
import json, os, sys

here = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]


def emit(text):
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


probe_keys = [k for k in os.environ.get("ENV_PROBE_KEYS", "").split("|") if k]
env_seen = {k: os.environ.get(k) for k in probe_keys}

if "--help" in argv:
    # 能力探测路径(阶段 D-T2):探测子进程同样留环境记录。
    with open(os.path.join(here, "probe_record.json"), "w", encoding="utf-8") as fh:
        json.dump({"argv": argv, "env": env_seen}, fh, ensure_ascii=False)
    emit("--output-format <format>  output format: text, json, or stream-json\n")
    sys.exit(0)

data = sys.stdin.buffer.read().decode("utf-8", errors="replace")
fmt = "json"
if "--output-format" in argv:
    fmt = argv[argv.index("--output-format") + 1]

with open(os.path.join(here, "record.json"), "w", encoding="utf-8") as fh:
    json.dump({"argv": argv, "stdin": data, "env": env_seen}, fh, ensure_ascii=False)


def event(seq, etype, **extra):
    obj = {
        "schemaVersion": 1, "sequence": seq, "timestampMs": 1700000000000 + seq,
        "runId": "run-1", "sessionId": "sess-1", "turnId": "turn-1", "type": etype,
    }
    obj.update(extra)
    emit(json.dumps(obj, ensure_ascii=False) + "\n")


if fmt == "stream-json":
    event(1, "exec.started")
    event(2, "item.started", item={"id": "u1:m", "type": "agent_message",
                                   "contentDelta": "好"})
    event(3, "exec.completed", result={
        "schemaVersion": 1, "type": "exec.result", "status": "succeeded", "output": "好",
    })
else:
    emit(json.dumps({"type": "exec.result", "status": "succeeded", "output": "好"},
                    ensure_ascii=False))
sys.exit(0)
'''


@pytest.fixture
def env_cli_factory(tmp_path, monkeypatch):
    """假 CLI 工厂:每个实例独立目录,记录文件互不串台(同 conftest 惯例)。"""
    counter = {"n": 0}

    def make() -> SimpleNamespace:
        counter["n"] += 1
        work = tmp_path / f"fake_env_cli_{counter['n']}"
        work.mkdir()
        script_path = work / "fake_env_cli.py"
        script_path.write_text(_FAKE_ENV_CLI_PY, encoding="utf-8")
        if os.name == "nt":
            cli_path = work / "fake_env_cli.cmd"
            cli_path.write_text(
                "@echo off\r\n"
                f'"{sys.executable}" "{script_path}" %*\r\n',
                encoding="ascii",
            )
        else:
            cli_path = script_path
            script_path.write_text(
                "#!/usr/bin/env python3\n" + _FAKE_ENV_CLI_PY, encoding="utf-8"
            )
            cli_path.chmod(cli_path.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("ENV_PROBE_KEYS", _PROBE_KEYS)
        return SimpleNamespace(
            path=str(cli_path),
            record_path=work / "record.json",
            probe_record_path=work / "probe_record.json",
        )

    return make


def _model(cli: SimpleNamespace, tmp_path, **kwargs) -> MCodeChatModel:
    return MCodeChatModel(
        cwd=str(tmp_path),
        mcode_bin=cli.path,
        session_id="env-test",
        **kwargs,
    )


def _read_env(record_path: Path) -> dict:
    assert record_path.is_file(), f"假 CLI 没有写记录: {record_path}"
    return json.loads(record_path.read_text(encoding="utf-8"))["env"]


# ---------------------------------------------------------------------------
# D-T1:build_process_env
# ---------------------------------------------------------------------------

def test_build_process_env_snapshots_current_environ():
    env = build_process_env()
    assert env == dict(os.environ)  # 缺省 = 当前进程环境的完整快照
    env["SHOULD_NOT_PROPAGATE"] = "1"
    assert "SHOULD_NOT_PROPAGATE" not in os.environ  # 返回新字典,不影响全局


def test_build_process_env_fills_missing_base_keys(monkeypatch):
    inherited = {k: os.environ[k] for k in BASE_ENV_KEYS if os.environ.get(k)}
    assert inherited, "前提:当前进程环境至少有一个基础变量"

    env = build_process_env({"ONLY": "1"})
    assert env["ONLY"] == "1"
    for key, value in inherited.items():
        assert env[key] == value  # 缺失的基础变量从当前进程环境补齐


def test_build_process_env_keeps_explicit_values(monkeypatch):
    monkeypatch.setenv("TEMP", r"C:\real-temp")
    env = build_process_env({"TEMP": r"C:\explicit-temp"})
    assert env["TEMP"] == r"C:\explicit-temp"  # 显式传入的键不被覆盖


def test_model_snapshot_taken_at_construction(tmp_path):
    # 构造不解析可执行文件,快照与 bin 无关;基础变量在构造瞬间就已定格。
    model = MCodeChatModel(cwd=str(tmp_path), mcode_bin="whatever.cmd")
    for key in BASE_ENV_KEYS:
        if os.environ.get(key):
            assert model._env_snapshot[key] == os.environ[key]


# ---------------------------------------------------------------------------
# P3:四个创建点全部显式收到同一份快照
# ---------------------------------------------------------------------------

def test_sync_generate_receives_snapshot(tmp_path, env_cli_factory, monkeypatch):
    monkeypatch.setenv(CANARY, "v1")
    cli = env_cli_factory()
    model = _model(cli, tmp_path)
    monkeypatch.setenv(CANARY, "v2")  # 构造后改全局:不影响快照(见 P2)

    model.invoke([HumanMessage(content="hi")])

    env = _read_env(cli.record_path)
    assert env[CANARY] == "v1" == model._env_snapshot[CANARY]


async def test_async_generate_receives_snapshot(tmp_path, env_cli_factory, monkeypatch):
    monkeypatch.setenv(CANARY, "v1")
    cli = env_cli_factory()
    model = _model(cli, tmp_path)
    monkeypatch.setenv(CANARY, "v2")

    await model.ainvoke([HumanMessage(content="hi")])

    env = _read_env(cli.record_path)
    assert env[CANARY] == "v1"


async def test_probe_receives_same_snapshot(tmp_path, env_cli_factory, monkeypatch):
    """能力探测(``exec --help``)也用同一份快照 —— 探测与正式执行同环境。"""
    monkeypatch.setenv(CANARY, "v1")
    cli = env_cli_factory()
    model = _model(cli, tmp_path, streaming_transport="auto")

    supported = await model._probe_stream_json_support()
    assert supported is True  # 假 CLI 的 --help 报告支持 stream-json

    env = _read_env(cli.probe_record_path)
    assert env[CANARY] == "v1" == model._env_snapshot[CANARY]


async def test_stream_receives_same_snapshot(tmp_path, env_cli_factory, monkeypatch):
    """流式路径:MCodeRunner(env=快照)→ 子进程实际收到同一份。"""
    monkeypatch.setenv(CANARY, "v1")
    cli = env_cli_factory()
    model = _model(cli, tmp_path, streaming_transport="stream_json")
    monkeypatch.setenv(CANARY, "v2")

    chunks = [chunk async for chunk in model.astream([HumanMessage(content="hi")])]
    assert "".join(c.content for c in chunks) == "好"

    env = _read_env(cli.record_path)
    assert env[CANARY] == "v1" == model._env_snapshot[CANARY]


async def test_all_four_paths_share_one_snapshot(tmp_path, env_cli_factory, monkeypatch):
    """四个创建点收到的环境**完全一致**,且等于构造时的快照(P3 收口)。"""
    monkeypatch.setenv(CANARY, "v1")

    cli_sync = env_cli_factory()
    cli_async = env_cli_factory()
    cli_probe = env_cli_factory()
    cli_stream = env_cli_factory()
    model_sync = _model(cli_sync, tmp_path)
    model_async = _model(cli_async, tmp_path)
    model_probe = _model(cli_probe, tmp_path, streaming_transport="auto")
    model_stream = _model(cli_stream, tmp_path, streaming_transport="stream_json")

    model_sync.invoke([HumanMessage(content="hi")])
    await model_async.ainvoke([HumanMessage(content="hi")])
    await model_probe._probe_stream_json_support()
    async for _ in model_stream.astream([HumanMessage(content="hi")]):
        pass

    snapshots = [m._env_snapshot for m in (model_sync, model_async, model_probe, model_stream)]
    assert all(s == snapshots[0] for s in snapshots)  # 同一进程内构造 → 同一份内容

    envs = [
        _read_env(cli_sync.record_path),
        _read_env(cli_async.record_path),
        _read_env(cli_probe.probe_record_path),
        _read_env(cli_stream.record_path),
    ]
    for env in envs:
        assert env[CANARY] == "v1"
        assert env["PATH"] == snapshots[0]["PATH"]


# ---------------------------------------------------------------------------
# P2:快照语义(构造后全局环境变化不影响子进程)
# ---------------------------------------------------------------------------

def test_env_change_after_construction_does_not_leak(tmp_path, env_cli_factory, monkeypatch):
    monkeypatch.setenv(CANARY, "before")
    cli = env_cli_factory()
    model = _model(cli, tmp_path)

    monkeypatch.setenv(CANARY, "after")
    monkeypatch.setenv("OPENSTORYLINE_ENV_LATE", "late-1")

    model.invoke([HumanMessage(content="hi")])
    env = _read_env(cli.record_path)
    assert env[CANARY] == "before"          # 拿到的仍是构造瞬间的值
    assert "OPENSTORYLINE_ENV_LATE" not in env  # 构造后新增的变量不出现


def test_explicit_process_env_is_respected(tmp_path, env_cli_factory, monkeypatch):
    """显式传 ``process_env``:以此为基础,缺失基础变量自动补齐。"""
    monkeypatch.delenv(CANARY, raising=False)
    cli = env_cli_factory()
    model = MCodeChatModel(
        cwd=str(tmp_path),
        mcode_bin=cli.path,
        session_id="env-test",
        # 显式快照是**完整替代**:探测白名单也要自己带上(生产语义如此,见断言)
        process_env={CANARY: "explicit", "ENV_PROBE_KEYS": _PROBE_KEYS},
    )
    model.invoke([HumanMessage(content="hi")])

    env = _read_env(cli.record_path)
    assert env[CANARY] == "explicit"
    if os.environ.get("PATH"):
        assert env["PATH"] == os.environ["PATH"]  # 基础变量补齐自当前进程环境


async def test_stream_snapshot_survives_runtime_env_change(
    tmp_path, env_cli_factory, monkeypatch
):
    """异步上下文里改全局环境(asyncio 场景),流式子进程仍拿构造快照。"""
    monkeypatch.setenv(CANARY, "v1")
    cli = env_cli_factory()
    model = _model(cli, tmp_path, streaming_transport="stream_json")

    async def _corrupt():
        monkeypatch.setenv(CANARY, "v2")
        await asyncio.sleep(0)

    await _corrupt()
    async for _ in model.astream([HumanMessage(content="hi")]):
        pass

    env = _read_env(cli.record_path)
    assert env[CANARY] == "v1"
