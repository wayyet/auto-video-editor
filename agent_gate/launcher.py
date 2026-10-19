"""无人值守地拉起 Agent 程序(claude / mcode),让它去处理一批 LLM 任务。

和 cli_client 的区别:
    cli_client  把 Agent 当「大模型」用:一问一答,没有工具。
    launcher    把 Agent 当「操作员」用:它自己调 MCP 工具领任务、交答案。

两个程序都从 cwd(project_dir)读 `.mcp.json`,所以 project_dir 里要有 .mcp.json
和 CLAUDE.md / AGENTS.md。提示词一律走 stdin:
    - `claude --allowedTools` 是可变参数,后面跟的提示词会被当成工具名;
    - Windows 命令行有长度上限。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from agent_gate.cli_client import CLIBackendFailed, CLIBackendTimeout, run_process

MCP_SERVER_NAME = "video-agent"   # 必须与 .mcp.json 里的服务名一致


@dataclass
class LaunchResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class AgentLauncher:
    name = ""
    env_bin = ""
    default_bin = ""

    def __init__(
        self,
        *,
        project_dir: str | Path,
        executable: Optional[str] = None,
        model: Optional[str] = None,
        session_timeout_s: float = 900.0,
        extra_args: Sequence[str] = (),
        extra_env: Optional[dict[str, str]] = None,
        clean_env: bool = False,
    ) -> None:
        self.project_dir = str(project_dir)
        self.executable = executable or os.environ.get(self.env_bin) or self.default_bin
        self.model = model
        self.session_timeout_s = session_timeout_s
        self.extra_args = list(extra_args)
        self.extra_env = dict(extra_env or {})
        self.clean_env = clean_env

    def command(self) -> list[str]:
        raise NotImplementedError

    def env(self) -> dict[str, str]:
        if self.clean_env:
            keep = {"PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL"}
            env = {k: v for k, v in os.environ.items() if k in keep}
        else:
            env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env.update(self.extra_env)
        return env

    def run(self, prompt: str) -> LaunchResult:
        try:
            rc, out, err = run_process(self.command(), prompt, cwd=self.project_dir, env=self.env(),
                                       timeout_s=self.session_timeout_s, label=self.name)
        except CLIBackendTimeout as e:
            return LaunchResult(-1, "", str(e), timed_out=True)
        except CLIBackendFailed as e:
            return LaunchResult(-1, "", str(e))
        return LaunchResult(rc, out, err)


class ClaudeLauncher(AgentLauncher):
    """`claude -p`:模型只能看到 video-agent 的 MCP 工具,没有 Bash / 读写文件 / 联网。

    三个开关缺一不可(沙箱实测:用户配置里放行了 Bash 时,只靠 dontAsk + allowedTools,
    模型的工具列表里仍有 Bash / Write / WebFetch 和别的 MCP 服务;三个开关全加后只剩 6 个工具):
        --tools ""            去掉全部内置工具
        --mcp-config <文件>    只读这一个 MCP 配置
        --strict-mcp-config   忽略用户自己配置的其他 MCP 服务
    """

    name = "claude"
    env_bin = "AGENT_GATE_CLAUDE_BIN"
    default_bin = "claude"

    def __init__(self, *, max_turns: int = 80, **kw: Any) -> None:
        super().__init__(**kw)
        self.max_turns = max_turns

    def command(self) -> list[str]:
        cmd = [self.executable, "-p", "--output-format", "json",
               "--permission-mode", "dontAsk",
               "--tools", "",
               "--mcp-config", str(Path(self.project_dir) / ".mcp.json"), "--strict-mcp-config",
               "--allowedTools", f"mcp__{MCP_SERVER_NAME}__*",
               "--max-turns", str(self.max_turns),
               "--no-session-persistence"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd + self.extra_args

    def env(self) -> dict[str, str]:
        env = super().env()
        # -p 默认不等 MCP 服务连上;服务启动慢时首轮会拿不到工具(沙箱实测)。让首轮等最多 30 秒。
        env.setdefault("CLAUDE_CODE_MCP_STARTUP_WAIT_MS", "30000")
        return env


class McodeLauncher(AgentLauncher):
    """`mcode exec`。

    没有命令行开关去掉内置工具,三种 --permission 对 MCP / bash / write 实测都放行(软护栏)。
    硬隔离走配置文件:给 `config` 传一个「加锁配置」(用 `python -m agent_gate mcode-lock` 生成),
    模型的工具就只剩 MCP 工具 + 几个无害的内置工具(沙箱实测)。注意 --config 会**整个替换**
    mcode 的默认配置,所以加锁配置里要自带 provider / 模型。
    """

    name = "mcode"
    env_bin = "AGENT_GATE_MCODE_BIN"
    default_bin = "mcode"

    def __init__(self, *, permission: str = "smart", max_steps: int = 120,
                 config: Optional[str | Path] = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.permission = permission
        self.max_steps = max_steps
        self.config = str(config) if config else None

    def command(self) -> list[str]:
        cmd = [self.executable, "exec", "--input", "-", "--output-format", "json",
               "--permission", self.permission, "--max-steps", str(self.max_steps),
               "--timeout", f"{max(1, int(self.session_timeout_s))}s", "--cwd", self.project_dir]
        if self.config:
            cmd += ["--config", self.config]
        if self.model:
            cmd += ["--model", self.model]
        return cmd + self.extra_args


def build_launcher(agent: str, **kw: Any) -> AgentLauncher:
    a = (agent or "").strip().lower()
    if a == "claude":
        return ClaudeLauncher(**kw)
    if a == "mcode":
        return McodeLauncher(**kw)
    raise ValueError(f"未知 Agent:{agent!r}(本方案只验证了 claude / mcode)")


def summarize_output(agent: str, stdout: str) -> dict[str, Any]:
    """从 Agent 的 JSON 输出里取几个字段,写日志用。解析失败返回空字典。"""
    try:
        obj = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        return {}
    if agent == "claude":
        keys = ("is_error", "subtype", "terminal_reason", "num_turns", "total_cost_usd")
        out = {k: obj.get(k) for k in keys if k in obj}
        denied = [d.get("tool_name") for d in obj.get("permission_denials") or []]
        if denied:
            out["permission_denials"] = denied
        return out
    keys = ("status", "durationMs")
    out = {k: obj.get(k) for k in keys if k in obj}
    if obj.get("error"):
        out["error"] = obj["error"]
    return out
