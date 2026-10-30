"""openstoryline 测试的公共装配。

sys.path 顺序(有讲究,别乱插)
----------------------------
1. ``<repo>/openstoryline/src``  —— ``open_storyline`` 包在这里,必须最先
2. ``<repo>/openstoryline``      —— 顶层 ``agent_fastapi`` 在这里
3. ``<repo>``                    —— 只为 ``import agent_gate``,放最后

为什么 ``<repo>`` 放最后:仓库根有一个顶层 ``config.py``。
``<repo>`` 一旦进了 ``sys.path``,裸 ``import config`` 就会解析到它。
``open_storyline`` 内部一律用 ``from open_storyline.config import ...``,
**新测试也不得写裸 ``import config``**。
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent
REPO_ROOT = OPENSTORYLINE_DIR.parent
SRC_DIR = OPENSTORYLINE_DIR / "src"

# 倒序 insert(0) -> 最终顺序正好是 SRC -> OPENSTORYLINE -> REPO
for _p in (str(REPO_ROOT), str(OPENSTORYLINE_DIR), str(SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# --------------------------------------------------------------------------
# 假 mcode CLI
# --------------------------------------------------------------------------

#: 假 CLI 的实现。行为刻意贴近真的 mcode:
#: 读 stdin、写 JSON 到 stdout、退出码可控、还能拉一个「孙进程」用于验证进程树回收。
_FAKE_CLI_PY = r'''
import json, os, subprocess, sys, time

spec = {}
spec_path = os.environ.get("FAKE_MCODE_SPEC", "")
if spec_path and os.path.isfile(spec_path):
    with open(spec_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)

# 必须按 **UTF-8** 解码:生产侧(MCodeChatModel / MCodeRunner)写 stdin 用的是
# ``prompt.encode("utf-8")``。这里若用 ``sys.stdin.read()``,Python 会按系统
# locale 编码解码(本机 Windows 是 GBK/cp936),中文会被解成乱码甚至孤立代理字符,
# 后续 json.dump 直接炸 UnicodeEncodeError —— 那是**测试夹具**的锅,不是被测代码的。
data = sys.stdin.buffer.read().decode("utf-8", errors="replace")


def emit(text):
    """stdout 也必须按 **UTF-8** 写。

    父进程(MCodeChatModel / MCodeRunner)用 ``decode("utf-8")`` 读子进程输出,
    而子进程 ``sys.stdout`` 默认是系统 locale 编码(本机 Windows 是 GBK)——
    两边不一致就会把中文变成乱码。stdout / stdin 的编码必须对齐。
    """
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


child_pid = None
if spec.get("spawn_child"):
    # 孙进程:用来验证「超时 / 取消后整棵进程树都得死」。
    # stdio 全指 DEVNULL —— 否则它会一直持着 stdout 管道,读端等不到 EOF。
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid = child.pid

record_path = os.environ.get("FAKE_MCODE_RECORD", "")
if record_path:
    bridge = ""
    argv = sys.argv[1:]
    if "--append-system-prompt-file" in argv:
        idx = argv.index("--append-system-prompt-file")
        if idx + 1 < len(argv) and os.path.isfile(argv[idx + 1]):
            with open(argv[idx + 1], "r", encoding="utf-8") as fh:
                bridge = fh.read()
    with open(record_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "argv": argv,
                "stdin": data,
                "pid": os.getpid(),
                "child_pid": child_pid,
                "bridge": bridge,
            },
            fh,
            ensure_ascii=False,
        )

delay = float(spec.get("delay_s") or 0)
if delay:
    time.sleep(delay)

if spec.get("raw") is not None:
    emit(spec["raw"])
elif spec.get("result") is not None:
    emit(json.dumps(spec["result"], ensure_ascii=False))
else:
    emit(json.dumps({"type": "exec.result", "status": "succeeded", "output": "OK"}))

exit_code = spec.get("exit_code")
if exit_code:
    sys.exit(int(exit_code))
'''


@pytest.fixture
def fake_mcode_cli(tmp_path, monkeypatch):
    """假 mcode CLI 工厂。

    产物是一个**真的可执行文件**,形状与生产一致:
    Windows 上是 ``.cmd`` shim 调 python 脚本(生产里 ``mcode.cmd`` 也是这个形状),
    POSIX 上是带 shebang 的 python 脚本。

    用法::

        cli = fake_mcode_cli(result={"type": "exec.result", "status": "succeeded", "output": "hi"})
        model = MCodeChatModel(cwd=str(tmp_path), mcode_bin=cli.path)

    返回的 ``cli`` 是个 SimpleNamespace:
      path        —— 可执行文件路径(传给 ``MCodeChatModel.mcode_bin``)
      record_path —— 子进程会把 argv / stdin / pid 写到这里
    """

    counter = {"n": 0}

    def make(
        *,
        result: Optional[Dict[str, Any]] = None,
        raw: Optional[str] = None,
        delay_s: float = 0.0,
        spawn_child: bool = False,
        exit_code: Optional[int] = None,
    ) -> SimpleNamespace:
        counter["n"] += 1
        work = tmp_path / f"fake_mcode_{counter['n']}"
        work.mkdir()

        spec = {
            "result": result,
            "raw": raw,
            "delay_s": delay_s,
            "spawn_child": spawn_child,
            "exit_code": exit_code,
        }
        spec_path = work / "spec.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        record_path = work / "record.json"

        script_path = work / "fake_mcode.py"
        script_path.write_text(_FAKE_CLI_PY, encoding="utf-8")

        if os.name == "nt":
            cli_path = work / "fake_mcode.cmd"
            cli_path.write_text(
                "@echo off\r\n"
                f'"{sys.executable}" "{script_path}" %*\r\n',
                encoding="ascii",
            )
        else:
            cli_path = script_path
            script_path.write_text(
                "#!/usr/bin/env python3\n" + _FAKE_CLI_PY, encoding="utf-8"
            )
            cli_path.chmod(cli_path.stat().st_mode | stat.S_IEXEC)

        monkeypatch.setenv("FAKE_MCODE_SPEC", str(spec_path))
        monkeypatch.setenv("FAKE_MCODE_RECORD", str(record_path))
        return SimpleNamespace(path=str(cli_path), record_path=record_path, spec_path=spec_path)

    return make


def read_record(cli: SimpleNamespace) -> Dict[str, Any]:
    """读假 CLI 留下的运行记录。"""
    assert cli.record_path.is_file(), f"假 CLI 没有写记录文件: {cli.record_path}"
    return json.loads(cli.record_path.read_text(encoding="utf-8"))


def pid_alive(pid: Optional[int]) -> bool:
    """这个 PID 上还有活着的进程吗。"""
    if pid is None:
        return False
    if os.name == "nt":
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=30, errors="replace",
        )
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_until_dead(pid: Optional[int], timeout_s: float = 15.0) -> bool:
    """等进程真的消失(杀进程树是异步的)。返回是否已死。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


# --------------------------------------------------------------------------
# 临时素材根
# --------------------------------------------------------------------------

@pytest.fixture
def tmp_media_root(tmp_path):
    """临时的素材白名单根(``<tmp>/inputs``)。

    素材相关测试用。**不碰**仓库里真实的 ``inputs/``。
    """
    root = tmp_path / "inputs"
    root.mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------
# 假 mcode CLI(会说 stream-json 的那种)
# --------------------------------------------------------------------------

#: 会说 ``stream-json`` 的假 CLI。
#:
#: 和上面的 ``fake_mcode_cli`` 的区别:
#:
#: * 按 ``--output-format`` 决定输出形态 —— ``json`` 吐一整段结果(旧路径),
#:   ``stream-json`` 吐 NDJSON 事件流(阶段 C 的流式路径);
#: * 支持 ``exec --help`` 能力探测(用来测「CLI 不支持 stream-json 就降级」);
#: * 能控制首段增量与后续增量之间的停顿(测「首段早于进程退出」)。
_FAKE_STREAM_CLI_PY = r'''
import json, os, sys, time

# spec / record 的路径由**脚本自身所在目录**决定,不用环境变量。
# 环境变量是全局的 —— 两个并发的假 CLI 会互相覆盖对方的 spec,
# 那样测出来的「隔离」是假的。
_here = os.path.dirname(os.path.abspath(__file__))

spec = {}
spec_path = os.path.join(_here, "spec.json")
if os.path.isfile(spec_path):
    with open(spec_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)


def emit(text):
    """stdout 一律按 UTF-8 写(父进程用 UTF-8 解)。"""
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


argv = sys.argv[1:]

# ---- 能力探测:<bin> exec --help ----
if "--help" in argv:
    if spec.get("help_text") is not None:
        emit(spec["help_text"])
    elif spec.get("supports_stream_json", True):
        emit("--output-format <format>  output format: text, json, or stream-json\n")
    else:
        emit("--output-format <format>  output format: text, json\n")
    sys.exit(0)

data = sys.stdin.buffer.read().decode("utf-8", errors="replace")

record_path = os.path.join(_here, "record.json")
with open(record_path, "w", encoding="utf-8") as fh:
    json.dump(
        {"argv": argv, "stdin": data, "pid": os.getpid()},
        fh,
        ensure_ascii=False,
    )

output_format = "json"
if "--output-format" in argv:
    idx = argv.index("--output-format")
    if idx + 1 < len(argv):
        output_format = argv[idx + 1]

deltas = spec.get("deltas")
if deltas is None:
    deltas = [spec.get("first_delta", "第一段"), "第二段"]

final_output = spec.get("final_output")
if final_output is None:
    final_output = "".join(deltas)

if output_format != "stream-json":
    # 旧路径:一整段结果 JSON。
    if spec.get("fail_hard"):
        # 模拟「CLI 跑挂了」:什么都不输出,直接非零退出。
        sys.exit(int(spec.get("exit_code") or 1))
    emit(json.dumps({
        "type": "exec.result",
        "status": "succeeded",
        "output": final_output,
    }, ensure_ascii=False))
    exit_code = spec.get("exit_code")
    sys.exit(int(exit_code) if exit_code else 0)


def event(seq, event_type, **extra):
    obj = {
        "schemaVersion": 1,
        "sequence": seq,
        "timestampMs": 1700000000000 + seq,
        "runId": spec.get("run_id", "run-1"),
        "sessionId": spec.get("session_id", "sess-1"),
        "turnId": spec.get("turn_id", "turn-1"),
        "type": event_type,
    }
    obj.update(extra)
    emit(json.dumps(obj, ensure_ascii=False) + "\n")


seq = 1
if spec.get("fail_hard"):
    # 模拟「CLI 跑挂了」:一个事件都不吐,直接非零退出。
    sys.exit(int(spec.get("exit_code") or 1))

event(seq, "exec.started"); seq += 1
event(seq, "session.started"); seq += 1
event(seq, "turn.started"); seq += 1

if spec.get("include_reasoning"):
    # 隐藏思维链:整条都不该进 chunk。
    event(seq, "item.started", item={
        "id": "u1:reasoning", "type": "reasoning",
        "contentDelta": spec.get("reasoning_text", "内部思维链原文"),
    }); seq += 1

for i, delta in enumerate(deltas):
    etype = "item.started" if i == 0 else "item.updated"
    event(seq, etype, item={
        "id": "u1:message", "type": "agent_message", "contentDelta": delta,
    }); seq += 1
    if i == 0 and spec.get("hold_s"):
        time.sleep(float(spec["hold_s"]))

# 工具调用(阶段 E:WS-03 要验证工具卡片状态顺序)。
# 生命周期由 toolCall.status 枚举驱动:Preparing(4) -> Start(1) -> Finished(2)。
for tool in (spec.get("tools") or []):
    tid = tool.get("id") or "call_1"
    tname = tool.get("name") or "read_notes"
    event(seq, "item.started", item={
        "id": tid, "type": "tool_call",
        "toolCall": {"id": tid, "name": tname, "status": 4},
    }); seq += 1
    event(seq, "item.updated", item={
        "id": tid, "type": "tool_call",
        "toolCall": {
            "id": tid, "name": tname, "status": 1,
            "input": tool.get("input") or {"path": "notes.md"},
        },
    }); seq += 1
    if tool.get("fail"):
        event(seq, "item.updated", item={
            "id": tid, "type": "tool_call",
            "toolCall": {
                "id": tid, "name": tname, "status": 3,
                "error": tool.get("error") or "工具执行失败",
            },
        }); seq += 1
    else:
        event(seq, "item.updated", item={
            "id": tid, "type": "tool_call",
            "toolCall": {
                "id": tid, "name": tname, "status": 2,
                "output": tool.get("output") or {
                    "content": [{"type": "text", "text": tool.get("text") or "已读取 5 个素材文件"}],
                },
            },
        }); seq += 1

event(seq, "exec.completed", result={
    "schemaVersion": 1, "type": "exec.result", "status": "succeeded",
    "output": final_output,
})

exit_code = spec.get("exit_code")
if exit_code:
    sys.exit(int(exit_code))
'''


@pytest.fixture
def fake_mcode_stream_cli(tmp_path, monkeypatch):
    """假 mcode CLI 工厂(会吐 ``stream-json`` 事件流)。

    用法::

        cli = fake_mcode_stream_cli(deltas=["甲", "乙"], hold_s=1.0)
        model = MCodeChatModel(cwd=str(tmp_path), mcode_bin=cli.path,
                                streaming_transport="stream_json")

    参数:
      deltas               agent_message 的增量列表(默认 ``["第一段", "第二段"]``)
      hold_s               首段增量之后的停顿秒数(用来测「首段早于进程退出」)
      final_output         ``exec.completed.result.output``;不给就是 deltas 拼接
      include_reasoning    是否额外吐一条 reasoning 事件
      supports_stream_json 能力探测时 ``--help`` 里是否出现 stream-json
      exit_code            退出码
      tools               工具调用列表(每项 ``{"id","name","input","output","fail"}``)
    """
    counter = {"n": 0}

    def make(
        *,
        deltas: Optional[List[str]] = None,
        first_delta: str = "第一段",
        hold_s: float = 0.0,
        final_output: Optional[str] = None,
        include_reasoning: bool = False,
        reasoning_text: str = "内部思维链原文",
        supports_stream_json: bool = True,
        help_text: Optional[str] = None,
        exit_code: Optional[int] = None,
        fail_hard: bool = False,
        run_id: str = "run-1",
        session_id: str = "sess-1",
        turn_id: str = "turn-1",
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> SimpleNamespace:
        counter["n"] += 1
        work = tmp_path / f"fake_stream_{counter['n']}"
        work.mkdir()

        spec: Dict[str, Any] = {
            "deltas": deltas,
            "first_delta": first_delta,
            "hold_s": hold_s,
            "final_output": final_output,
            "include_reasoning": include_reasoning,
            "reasoning_text": reasoning_text,
            "supports_stream_json": supports_stream_json,
            "help_text": help_text,
            "exit_code": exit_code,
            "fail_hard": fail_hard,
            "run_id": run_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "tools": tools,
        }
        spec_path = work / "spec.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        record_path = work / "record.json"

        script_path = work / "fake_stream.py"
        script_path.write_text(_FAKE_STREAM_CLI_PY, encoding="utf-8")

        if os.name == "nt":
            cli_path = work / "fake_stream.cmd"
            cli_path.write_text(
                "@echo off\r\n"
                f'"{sys.executable}" "{script_path}" %*\r\n',
                encoding="ascii",
            )
        else:
            cli_path = script_path
            script_path.write_text(
                "#!/usr/bin/env python3\n" + _FAKE_STREAM_CLI_PY, encoding="utf-8"
            )
            cli_path.chmod(cli_path.stat().st_mode | stat.S_IEXEC)

        # 注意:spec / record 不用环境变量传(环境变量是全局的,并发时会串台)。
        # 假 CLI 按自身脚本所在目录定位这两个文件。
        return SimpleNamespace(path=str(cli_path), record_path=record_path, spec_path=spec_path)
    return make
