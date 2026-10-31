"""Postgres DSN 解析与传播回归(执行设计 §7.7 阶段 C3;对应 §8 阶段 D 的 T1–T7)。

覆盖:
- T1–T5  ``resolve_postgres_target()`` 的来源优先级与空值回退
         (``AGENT_GATE_POSTGRES_URI`` → ``POSTGRES_URI`` → ``config.POSTGRES_URI`` → 默认)
- T6     脱敏诊断字段不泄漏密码;无密码 URI 连接失败时可定位
- T7     MCP 子进程 env → worker 的传播:worker 用的是**启动瞬间**的环境快照,
         不依赖父进程之后的环境修改(2026-10-31 `no password supplied` 故障的根因形态)
- 另加   ``start_production.ps1`` 的静态回归:POSTGRES_URI 注入必须发生在
         任何 ``Start-Process`` 之前,且 ``AGENT_GATE_POSTGRES_URI`` 被备份并清除

T8–T10 依赖真实部署,在执行设计阶段 E 回填,不在本文件覆盖。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
START_PS1 = REPO_ROOT / "start_production.ps1"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_agent_gate_postgres.py"

ENV_AGENT = "AGENT_GATE_POSTGRES_URI"
ENV_PROD = "POSTGRES_URI"

# 合成 DSN(不会真的连):用于优先级断言,不指向任何真实库。
GOOD_URI = "postgresql://alice:s3cret@db.example:5432/other_db"
# 无密码 DSN,指向本机真实服务端(它要求密码)—— T6 的"可定位失败"场景。
NO_PW_URI = "postgresql://postgres@localhost:5432/postgres"
GRAPH = "toy_graph:build_graph"


def _clear_dsn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_AGENT, raising=False)
    monkeypatch.delenv(ENV_PROD, raising=False)


# ---------------------------------------------------------------------------
# T1–T5:resolve_postgres_target 的优先级与空值回退
# ---------------------------------------------------------------------------

def test_t1_only_postgres_uri_with_password_is_chosen(monkeypatch):
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv(ENV_PROD, GOOD_URI)
    from agent_gate.runner import resolve_postgres_target

    t = resolve_postgres_target()
    assert t.source == "env:POSTGRES_URI"
    assert t.uri == GOOD_URI


def test_t2_agent_gate_var_keeps_priority(monkeypatch):
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv(ENV_AGENT, GOOD_URI)
    monkeypatch.setenv(ENV_PROD, "postgresql://bob:s3cret@other.example:5432/prod_db")
    from agent_gate.runner import resolve_postgres_target

    t = resolve_postgres_target()
    assert t.source == "env:AGENT_GATE_POSTGRES_URI"
    assert t.uri == GOOD_URI


def test_t3_blank_agent_gate_var_falls_to_postgres_uri(monkeypatch):
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv(ENV_AGENT, "   ")  # 空白 = 未提供
    monkeypatch.setenv(ENV_PROD, GOOD_URI)
    from agent_gate.runner import resolve_postgres_target

    t = resolve_postgres_target()
    assert t.source == "env:POSTGRES_URI"
    assert t.uri == GOOD_URI


def test_t4_blank_postgres_uri_falls_to_config(monkeypatch):
    """``POSTGRES_URI`` 为空串/空白时不返回空 DSN,落到 config 分支。"""
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv(ENV_PROD, "")
    fake = types.ModuleType("config")
    fake.POSTGRES_URI = "postgresql://cfg:c@cfg.example:5432/cfgdb"
    monkeypatch.setitem(sys.modules, "config", fake)
    from agent_gate.runner import resolve_postgres_target

    t = resolve_postgres_target()
    assert t.source == "config.POSTGRES_URI"
    assert t.uri == fake.POSTGRES_URI


def _resolve_in_subprocess(extra_env: dict[str, str]) -> tuple[str, str, str]:
    """在干净子进程里跑 resolve_postgres_target,返回 (source, uri, config.POSTGRES_URI)。

    走子进程是因为 ``config.py`` 在导入时读环境变量;pytest 进程里 config 可能
    已被别的模块用另一套 env 导入过,结果不确定。
    """
    env = {
        k: v for k, v in os.environ.items()
        if k not in (ENV_AGENT, ENV_PROD, "PGPASSWORD", "PGPASSFILE")
    }
    env.update(extra_env)
    env["PYTHONIOENCODING"] = "utf-8"
    code = (
        f"import sys; sys.path.insert(0, r'{REPO_ROOT}');"
        "from agent_gate.runner import resolve_postgres_target;"
        "from config import POSTGRES_URI as cfg;"
        "t = resolve_postgres_target();"
        "print(t.source + '|' + t.uri + '|' + cfg)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", cwd=str(REPO_ROOT), env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    source, uri, cfg = r.stdout.strip().split("|", 2)
    return source, uri, cfg


def test_t4_empty_env_var_does_not_shadow_config_default():
    """config.py 的 C2 修复:``POSTGRES_URI=""`` 必须回退默认 DSN,而不是空串。"""
    source, uri, cfg = _resolve_in_subprocess({ENV_PROD: ""})
    assert source == "config.POSTGRES_URI"
    assert uri == cfg
    assert uri  # 永远不返回空 DSN
    assert uri == "postgresql://postgres:devpass@localhost:5432/video_workflow"


def test_t5_both_vars_unset_uses_config(monkeypatch):
    _clear_dsn_env(monkeypatch)
    monkeypatch.setitem(sys.modules, "config", None)  # 强制 ImportError → 默认分支
    from agent_gate.runner import resolve_postgres_target

    t = resolve_postgres_target()
    assert t.source == "runner.DEFAULT_POSTGRES_URI"
    assert t.uri  # 非空


def test_t5_subprocess_unset_env_falls_to_config():
    source, uri, cfg = _resolve_in_subprocess({})
    assert source == "config.POSTGRES_URI"
    assert uri == cfg
    assert uri


def test_postgres_uri_wrapper_keeps_str_contract(monkeypatch):
    """``postgres_uri()`` 保留返回 str 的旧签名(既有调用点零改动)。"""
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv(ENV_PROD, GOOD_URI)
    from agent_gate.runner import postgres_uri

    assert postgres_uri() == GOOD_URI


# ---------------------------------------------------------------------------
# T6:脱敏诊断不泄漏密码;无密码 URI 连接失败可定位
# ---------------------------------------------------------------------------

def test_t6_diagnostic_masks_password_and_flags_missing():
    from agent_gate.runner import PostgresTarget, describe_postgres_target

    info = describe_postgres_target(PostgresTarget(uri=GOOD_URI, source="env:TEST"))
    assert info["password_present"] is True
    assert "s3cret" not in json.dumps(info, ensure_ascii=False)
    assert info["uri_masked"] == "postgresql://alice:***@db.example:5432/other_db"

    info2 = describe_postgres_target(PostgresTarget(uri=NO_PW_URI, source="env:TEST"))
    assert info2["password_present"] is False
    assert "***" not in info2["uri_masked"]


def test_t6_diagnostic_drops_query_string():
    """query 可能带敏感参数(sslpassword 等),脱敏 URI 必须丢弃。"""
    from agent_gate.runner import PostgresTarget, describe_postgres_target

    uri = "postgresql://alice:s3cret@db.example:5432/other_db?sslpassword=leak&sslmode=require"
    info = describe_postgres_target(PostgresTarget(uri=uri, source="env:TEST"))
    blob = json.dumps(info, ensure_ascii=False)
    assert "leak" not in blob and "sslpassword" not in blob


def test_t6_passwordless_uri_failure_is_diagnosable():
    """无密码 URI 连真实服务端(要求密码)→ 脚本退出码 1,诊断可见、密码不可见。"""
    env = {
        k: v for k, v in os.environ.items()
        if k not in (ENV_AGENT, ENV_PROD, "PGPASSWORD", "PGPASSFILE")
    }
    env[ENV_AGENT] = NO_PW_URI
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, str(VERIFY_SCRIPT)], capture_output=True,
                       text=True, encoding="utf-8", cwd=str(REPO_ROOT), env=env, timeout=60)
    assert r.returncode == 1, f"stdout={r.stdout}\nstderr={r.stderr}"
    blob = (r.stdout + r.stderr).lower().replace(" ", "")
    assert "password_present:false" in blob          # 定位线索明确(脚本输出用冒号)
    assert "✗" in (r.stdout + r.stderr)              # 失败原因落了异常信息
    assert "devpass" not in blob                     # 绝不泄漏其它 DSN 的密码


def test_verify_script_success_path_reports_source():
    """成功路径:退出码 0,输出当前会话测试库的来源与脱敏目标。"""
    uri = os.environ.get(ENV_AGENT, "")
    if not uri:
        pytest.skip("当前会话没有 AGENT_GATE_POSTGRES_URI(conftest 未指向测试库)")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, str(VERIFY_SCRIPT)], capture_output=True,
                       text=True, encoding="utf-8", cwd=str(REPO_ROOT), env=env, timeout=60)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "source          : env:AGENT_GATE_POSTGRES_URI" in r.stdout
    assert "✓ 连接成功" in r.stdout
    assert "devpass" not in (r.stdout + r.stderr)


# ---------------------------------------------------------------------------
# T7:MCP 子进程 → worker 的环境传播(启动瞬间快照)
# ---------------------------------------------------------------------------

def test_t7_worker_env_snapshot_from_mcp_child(tmp_path, monkeypatch, _agent_gate_postgres):
    """MCP 子进程 env 里的 ``POSTGRES_URI`` 被继承到 worker;pytest 进程事后改 env 不影响。

    生产形态对齐:子进程 env 只有 ``POSTGRES_URI``(没有测试隔离变量),
    与 ``start_production.ps1`` 注入后的形态一致。
    """
    media = tmp_path / "media"
    media.mkdir()
    video = media / "30s.mp4"
    video.write_bytes(b"\x00" * 16)  # run_start 只要求"白名单目录内的文件"
    outputs = tmp_path / "o"

    env = dict(os.environ)
    env.pop(ENV_AGENT, None)                     # 生产形态:无 AGENT_GATE 覆盖值
    env[ENV_PROD] = _agent_gate_postgres         # 会话级测试库
    env["AGENT_GATE_MEDIA_ROOTS"] = str(media)
    env["PYTHONUTF8"] = "1"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_gate.mcp_server", "--outputs-root", str(outputs),
              "--graph-factory", GRAPH],
        env=env,
    )
    want_db = urlsplit(_agent_gate_postgres).path.lstrip("/")

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                st = await s.call_tool("run_start", {"thread_id": "t7", "video_path": str(video)})
                assert st.structuredContent["ok"] is True, st.structuredContent

                # worker 已被 MCP 拉起;此刻才改 pytest 进程的 env。
                # worker 用的是 MCP 子进程 env 的启动快照,不应受影响。
                monkeypatch.setenv(ENV_PROD, "postgresql://stale:stale@127.0.0.1:9/after_spawn")

                gate = outputs / "t7" / "agent_gate"
                deadline = time.monotonic() + 30
                target_ev = None
                while time.monotonic() < deadline and target_ev is None:
                    p = gate / "events.jsonl"
                    if p.exists():
                        for line in p.read_text(encoding="utf-8").splitlines():
                            ev = json.loads(line)
                            if ev.get("event") == "postgres_target":
                                target_ev = ev
                                break
                    if target_ev is None:
                        await asyncio.sleep(0.2)
                assert target_ev is not None, "worker 没有落 postgres_target 事件"
                assert target_ev["source"] == "env:POSTGRES_URI"
                assert target_ev["password_present"] is True
                assert target_ev["host"] == "localhost"
                assert target_ev["dbname"] == want_db

                # 等 worker 真正建起 checkpointer 并开始跑图。``STATE_RUNNING`` 是在
                # ``checkpointer_session`` 成功(连接 + saver.setup())**之后**才设置的,
                # 所以到达 running 就证明了继承的 DSN 可连,而不只是被解析出来。
                # (跑到 waiting_human 的全流程由 test_mcp.py 覆盖;无 LLM 降级路径
                # 在本机较慢,不在此重复等待。)
                deadline = time.monotonic() + 90
                state = None
                while time.monotonic() < deadline:
                    cur = (await s.call_tool("run_status", {"thread_id": "t7"})).structuredContent
                    state = cur["worker_state"]
                    if state in ("running", "waiting_human", "completed", "failed", "crashed"):
                        break
                    await asyncio.sleep(0.3)
                assert state in ("running", "waiting_human", "completed"), f"worker 状态异常: {state}"

    asyncio.run(go())


# ---------------------------------------------------------------------------
# 生产启动脚本静态回归(T2 生产脚本部分 / T10 的静态部分)
# ---------------------------------------------------------------------------

def _first_command_index(text: str, needle: str) -> int:
    """首个**非注释行**里 needle 的偏移;找不到返回 -1。"""
    idx = 0
    for line in text.splitlines():
        if not line.strip().startswith("#"):
            pos = line.find(needle)
            if pos >= 0:
                return idx + pos
        idx += len(line) + 1
    return -1


def test_production_script_injects_postgres_uri_before_any_start_process():
    text = START_PS1.read_text(encoding="utf-8-sig")
    inject = re.search(r"^\s*\$env:POSTGRES_URI\s*=\s*\$PostgresUri", text, re.M)
    assert inject, "start_production.ps1 必须显式注入 $env:POSTGRES_URI"
    first_start = _first_command_index(text, "Start-Process")
    assert first_start >= 0
    assert inject.start() < first_start, "POSTGRES_URI 注入必须早于任何 Start-Process"

    clear = _first_command_index(text, "Remove-Item Env:AGENT_GATE_POSTGRES_URI")
    assert clear >= 0 and clear < first_start, "AGENT_GATE_POSTGRES_URI 必须在任何子进程前清除"

    assert re.search(r"'AGENT_GATE_POSTGRES_URI'", text), "envBackup 备份列表必须包含该变量"
    assert re.search(r"foreach \(\$name in \$envBackup\.Keys\)", text), "finally 必须按 envBackup 恢复"

    # 只允许一处注入(C1 删除了旧的 Phase 0.7 后置重复注入块)
    assert len(re.findall(r"^\s*\$env:POSTGRES_URI\s*=", text, re.M)) == 1


def test_production_script_parses_cleanly_under_pwsh():
    """pwsh 7+ 解析无语法错误(Windows PowerShell 5.1 的解析器不认脚本里的新语法)。"""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("本机没有 pwsh;该脚本必须用 pwsh 7+ 解析(5.1 解析器有已知不兼容)")
    # ⚠️ pwsh -Command 会把后续参数拼进命令串(不会绑定到 $args),路径必须插值进来。
    ps_path = str(START_PS1).replace("'", "''")
    r = subprocess.run(
        [pwsh, "-NoProfile", "-Command",
         "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
         f"$path='{ps_path}';"
         "$tok=$null;$err=$null;"
         "[System.Management.Automation.Language.Parser]::ParseFile($path,[ref]$tok,[ref]$err)|Out-Null;"
         "if($err.Count){$err|ForEach-Object{$_.Message};exit 1}else{exit 0}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
