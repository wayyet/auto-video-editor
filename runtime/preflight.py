"""启动前自检(pre-flight)Python 调度层。

设计目标
--------
- 把 ``scripts/preflight.ps1`` 这个 PowerShell 服务控制脚本包装成可被主程序
  调用的同步 API,提供结构化结果与测试替身。
- 跨平台:Windows 上调 PowerShell 子进程;其他平台直接抛 ``NotImplementedError``,
  由主程序 ``scripts/run_workflow.py`` 决定是否降级。
- 单测友好:核心入口 ``run_preflight`` 和辅助函数都接受依赖注入的「子进程包装器」,
  便于 ``pytest-mock`` 替换。

公开 API
--------
- ``PreflightConfig`` —— 数据类,可从环境变量 ``AUTO_VIDEO_EDITOR_PREFLIGHT_*`` 覆盖。
- ``PreflightStepResult`` / ``PreflightResult`` —— 结构化结果。
- ``PreflightError`` —— 顶层失败异常(带 step 名 + 原始 stderr 摘要)。
- ``run_preflight(config)`` —— 主入口(Windows spawn PowerShell)。
- ``_postgres_reachable(uri, timeout)`` —— TCP 探测(从测试复制,独立单测)。
- ``_run_powershell_step(script, args, timeout)`` —— 子进程包装,可被 mock。

退出码约定(与 ``scripts/preflight.ps1`` 对齐):
    0      → 全部 OK
    10-16  → Step 1-6 失败
    99     → 未捕获异常
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
# runtime/ 在项目根下;scripts/ 是 runtime/ 的兄弟目录。
RUNTIME_DIR: Path = Path(__file__).resolve().parent
REPO_ROOT: Path = RUNTIME_DIR.parent
SCRIPTS_DIR: Path = REPO_ROOT / "scripts"
DEFAULT_PREFLIGHT_SCRIPT: Path = SCRIPTS_DIR / "preflight.ps1"

# ---------------------------------------------------------------------------
# PowerShell 退出码 → step 名称 + 错误前缀
# ---------------------------------------------------------------------------
_EXIT_CODE_MAP: dict[int, tuple[str, str]] = {
    10: ("docker_service", "Docker Desktop 服务启动失败"),
    11: ("docker_service", "Docker Desktop 服务启动需管理员权限"),
    12: ("daemon", "Docker daemon 在指定超时内未就绪"),
    13: ("proxy", "Docker 代理配置异常"),
    14: ("postgres_container", "Postgres 容器启动失败"),
    15: ("postgres_connect", "Postgres 连通性失败"),
    16: ("schema", "LangGraph checkpoint 表创建失败"),
}

_STEP_ORDER: tuple[str, ...] = (
    "docker_service",
    "daemon",
    "proxy",
    "postgres_container",
    "postgres_connect",
    "schema",
)


@dataclass(frozen=True)
class PreflightConfig:
    """pre-flight 配置(可被环境变量覆盖)。

    所有 ``skip_*`` 开关默认 False,生产路径全开;测试/CI 可单独关。
    """

    skip_docker_service: bool = False
    skip_proxy_check: bool = False
    skip_postgres_start: bool = False
    skip_schema_setup: bool = False
    docker_service_wait_sec: int = 60
    postgres_health_wait_sec: int = 60
    powershell_script: Path = DEFAULT_PREFLIGHT_SCRIPT
    timeout_sec: int = 600  # 整个 pre-flight 总超时
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "PreflightConfig":
        """从 ``AUTO_VIDEO_EDITOR_PREFLIGHT_*`` 环境变量覆盖默认值。

        约定的覆盖形式:
            AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_DOCKER_SERVICE=1   → True
            AUTO_VIDEO_EDITOR_PREFLIGHT_DOCKER_SERVICE_WAIT_SEC=120 → 120
        """
        env = os.environ
        return cls(
            skip_docker_service=_env_bool(env.get("AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_DOCKER_SERVICE")),
            skip_proxy_check=_env_bool(env.get("AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_PROXY_CHECK")),
            skip_postgres_start=_env_bool(env.get("AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_POSTGRES_START")),
            skip_schema_setup=_env_bool(env.get("AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_SCHEMA_SETUP")),
            docker_service_wait_sec=_env_int(
                env.get("AUTO_VIDEO_EDITOR_PREFLIGHT_DOCKER_SERVICE_WAIT_SEC"), 60
            ),
            postgres_health_wait_sec=_env_int(
                env.get("AUTO_VIDEO_EDITOR_PREFLIGHT_POSTGRES_HEALTH_WAIT_SEC"), 60
            ),
            dry_run=_env_bool(env.get("AUTO_VIDEO_EDITOR_PREFLIGHT_DRY_RUN")),
        )


def _env_bool(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(value: Optional[str], default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# 结果数据类
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PreflightStepResult:
    """单个 step 的执行结果。"""

    step: str  # "docker_service" / "daemon" / "proxy" / "postgres_container" / "postgres_connect" / "schema"
    ok: bool
    duration_ms: int
    error: Optional[str] = None


@dataclass(frozen=True)
class PreflightResult:
    """整体 pre-flight 执行结果。"""

    ok: bool
    steps: list[PreflightStepResult]
    total_duration_ms: int

    def first_failure(self) -> Optional[PreflightStepResult]:
        """返回第一个失败的 step;全部成功则返回 None。"""
        for s in self.steps:
            if not s.ok:
                return s
        return None


class PreflightError(RuntimeError):
    """pre-flight 失败时抛出,带失败 step 名 + 原始 stderr 摘要。"""


# ---------------------------------------------------------------------------
# TCP 探测(从 tests/integration/test_postgres_checkpointer.py:22 复制)
# ---------------------------------------------------------------------------
def _postgres_reachable(uri: str, timeout: float = 1.5) -> bool:
    """快速判断 Postgres URI 是否可达(TCP connect)。

    只解析 host:port,不建完整连接。失败(超时 / refused / 解析失败)统一返回 False。
    """
    try:
        from urllib.parse import urlparse

        parsed = urlparse(uri)
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 — 探测函数永远不应抛错
        return False


# ---------------------------------------------------------------------------
# PowerShell 子进程包装(可被 mock 替换)
# ---------------------------------------------------------------------------
def _run_powershell_step(
    script: Path,
    args: Sequence[str],
    timeout: int,
) -> tuple[int, str, str]:
    """调 PowerShell 跑一个脚本文件,返回 ``(returncode, stdout, stderr)``。

    Windows 专用;非 Windows 直接抛 ``NotImplementedError``,由 ``run_preflight``
    在更外层捕获,避免双层 try。

    使用 ``CREATE_NO_WINDOW`` 避免弹黑窗;``-NoProfile`` 让 PowerShell 启动更快。
    """
    if sys.platform != "win32":
        raise NotImplementedError(
            "PowerShell 子进程调用仅在 Windows 平台实现"
        )

    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW

    cmd: list[str] = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(script),
        *args,
    ]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=creationflags,
        check=False,
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run_preflight(config: Optional[PreflightConfig] = None) -> PreflightResult:
    """执行 pre-flight。

    Args:
        config: ``PreflightConfig`` 实例;None 时使用默认配置。

    Returns:
        ``PreflightResult``:每个 step 的执行结果 + 总耗时。

    Raises:
        NotImplementedError: 非 Windows 平台。
        PreflightError: 任一步骤失败 / PowerShell 不可用 / 子进程超时。
    """
    if sys.platform != "win32":
        raise NotImplementedError(
            "pre-flight 当前仅支持 Windows(Docker Desktop 是 Windows 服务)"
        )

    cfg = config or PreflightConfig()
    if cfg.dry_run:
        # DryRun 模式只打印计划,不实际执行;全 step 标 ok=True。
        steps = [
            PreflightStepResult(
                step=step_name,
                ok=True,
                duration_ms=0,
                error="dry-run skipped" if _is_skipped(cfg, step_name) else None,
            )
            for step_name in _STEP_ORDER
        ]
        return PreflightResult(ok=True, steps=steps, total_duration_ms=0)

    script = Path(cfg.powershell_script)
    if not script.is_absolute():
        script = (REPO_ROOT / script).resolve()
    if not script.exists():
        raise PreflightError(f"找不到 pre-flight 脚本:{script}")

    pwsh_args: list[str] = _config_to_pwsh_args(cfg)
    started = _now_ms()

    try:
        returncode, stdout, stderr = _run_powershell_step(
            script, pwsh_args, cfg.timeout_sec
        )
    except subprocess.TimeoutExpired as e:
        raise PreflightError(
            f"pre-flight 子进程超时 ({cfg.timeout_sec}s);"
            f"已捕获 stdout={_truncate(e.stdout or '')}, stderr={_truncate(e.stderr or '')}"
        ) from e
    except FileNotFoundError as e:
        # pwsh.exe 不在 PATH
        raise PreflightError(
            "找不到 pwsh.exe;请安装 PowerShell 7+ 并加入 PATH,或用 PowerShell 5.1 跑"
        ) from e

    duration_ms = _now_ms() - started

    if returncode == 0:
        steps = [
            PreflightStepResult(step=name, ok=True, duration_ms=duration_ms)
            for name in _STEP_ORDER
        ]
        return PreflightResult(ok=True, steps=steps, total_duration_ms=duration_ms)

    # 退出码非 0:按映射解析失败的 step,其余 step 标 ok=True(脚本串行早退)
    mapping = _EXIT_CODE_MAP.get(returncode)
    if mapping is None:
        raise PreflightError(
            f"pre-flight 异常退出 (returncode={returncode});"
            f"stdout={_truncate(stdout)}, stderr={_truncate(stderr)}"
        )

    failed_step, error_prefix = mapping
    failed_step_obj = PreflightStepResult(
        step=failed_step,
        ok=False,
        duration_ms=duration_ms,
        error=f"{error_prefix}: {_truncate(stderr)}".strip(": "),
    )

    steps: list[PreflightStepResult] = []
    for name in _STEP_ORDER:
        if name == failed_step:
            steps.append(failed_step_obj)
            break
        steps.append(PreflightStepResult(step=name, ok=True, duration_ms=duration_ms))

    return PreflightResult(
        ok=False,
        steps=steps,
        total_duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _now_ms() -> int:
    import time

    return int(time.monotonic() * 1000)


def _truncate(text: str, limit: int = 800) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...(truncated, total {len(text)} chars)"


def _is_skipped(cfg: PreflightConfig, step_name: str) -> bool:
    return {
        "docker_service": cfg.skip_docker_service,
        "daemon": False,
        "proxy": cfg.skip_proxy_check,
        "postgres_container": cfg.skip_postgres_start,
        "postgres_connect": False,
        "schema": cfg.skip_schema_setup,
    }.get(step_name, False)


def _config_to_pwsh_args(cfg: PreflightConfig) -> list[str]:
    """把 dataclass 配置翻译成 PowerShell 脚本的命令行参数。"""
    args: list[str] = []
    if cfg.skip_docker_service:
        args.append("-SkipDockerService")
    if cfg.skip_proxy_check:
        args.append("-SkipProxyCheck")
    if cfg.skip_postgres_start:
        args.append("-SkipPostgresStart")
    if cfg.skip_schema_setup:
        args.append("-SkipSchemaSetup")
    args.extend(["-DockerServiceWaitSec", str(cfg.docker_service_wait_sec)])
    args.extend(["-PostgresHealthWaitSec", str(cfg.postgres_health_wait_sec)])
    return args


__all__ = [
    "PreflightConfig",
    "PreflightStepResult",
    "PreflightResult",
    "PreflightError",
    "run_preflight",
    "_postgres_reachable",
    "_run_powershell_step",
    "DEFAULT_PREFLIGHT_SCRIPT",
    "REPO_ROOT",
    "SCRIPTS_DIR",
]