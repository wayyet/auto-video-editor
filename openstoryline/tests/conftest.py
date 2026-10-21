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

data = sys.stdin.read()

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
    sys.stdout.write(spec["raw"])
elif spec.get("result") is not None:
    sys.stdout.write(json.dumps(spec["result"], ensure_ascii=False))
else:
    sys.stdout.write(json.dumps({"type": "exec.result", "status": "succeeded", "output": "OK"}))
sys.stdout.flush()

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
