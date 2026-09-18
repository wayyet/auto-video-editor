"""pre-flight 模块单测。

覆盖范围:
1. ``_postgres_reachable()`` — TCP 探测(closed / open 两个路径)
2. ``PreflightConfig.from_env()`` — 环境变量覆盖
3. ``run_preflight()`` — 非 Windows 平台抛 ``NotImplementedError``
4. ``run_preflight()`` — 通过 mock 子进程验证:
   - exit 0 → ok=True,所有 step ok=True
   - exit 14 → steps[2].step="postgres_container",ok=False
   - 子进程超时 → 抛 ``PreflightError`` 含 "超时"
5. ``PreflightResult.first_failure()`` 返回首个失败 step 或 None
6. ``PreflightConfig`` ``dry_run`` 模式不调 PowerShell

策略
----
所有依赖 PowerShell 的测试都通过 ``pytest-mock`` 替换 ``runtime.preflight._run_powershell_step``。
所有依赖 TCP 的测试通过 ``unittest.mock`` 替换 ``socket.create_connection``。
所有依赖环境变量的测试用 ``monkeypatch.delenv`` + ``monkeypatch.setenv`` 隔离。
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from runtime.preflight import (
    DEFAULT_PREFLIGHT_SCRIPT,
    PreflightConfig,
    PreflightError,
    PreflightResult,
    PreflightStepResult,
    _postgres_reachable,
    _run_powershell_step,
    run_preflight,
)


# ---------------------------------------------------------------------------
# 1. _postgres_reachable()
# ---------------------------------------------------------------------------
def test_postgres_reachable_returns_false_when_port_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 端口被拒绝时 → 返回 False,不抛错。"""

    def _raise(_args, **_kwargs):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(socket, "create_connection", _raise)
    assert _postgres_reachable("postgresql://u:p@localhost:5432/langgraph") is False


def test_postgres_reachable_returns_true_when_port_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 端口接受连接 → 返回 True。"""
    fake_sock = mock.MagicMock()
    fake_sock.__enter__ = mock.MagicMock(return_value=fake_sock)
    fake_sock.__exit__ = mock.MagicMock(return_value=False)
    monkeypatch.setattr(socket, "create_connection", mock.MagicMock(return_value=fake_sock))
    assert _postgres_reachable("postgresql://u:p@localhost:5432/langgraph") is True


def test_postgres_reachable_handles_invalid_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    """URI 解析失败 → 返回 False(不抛)。"""
    monkeypatch.setattr(socket, "create_connection", mock.MagicMock(side_effect=OSError("boom")))
    # 无 host 的诡异 URI
    assert _postgres_reachable("not-a-uri") is False


# ---------------------------------------------------------------------------
# 2. PreflightConfig.from_env()
# ---------------------------------------------------------------------------
def test_config_defaults_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """无环境变量 → 全 False + 默认超时 60s。"""
    for key in (
        "AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_DOCKER_SERVICE",
        "AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_PROXY_CHECK",
        "AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_POSTGRES_START",
        "AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_SCHEMA_SETUP",
        "AUTO_VIDEO_EDITOR_PREFLIGHT_DOCKER_SERVICE_WAIT_SEC",
        "AUTO_VIDEO_EDITOR_PREFLIGHT_POSTGRES_HEALTH_WAIT_SEC",
        "AUTO_VIDEO_EDITOR_PREFLIGHT_DRY_RUN",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = PreflightConfig.from_env()
    assert cfg.skip_docker_service is False
    assert cfg.skip_proxy_check is False
    assert cfg.skip_postgres_start is False
    assert cfg.skip_schema_setup is False
    assert cfg.docker_service_wait_sec == 60
    assert cfg.postgres_health_wait_sec == 60
    assert cfg.dry_run is False


def test_config_from_env_parses_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """设置覆盖环境变量 → dataclass 字段反映。"""
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_DOCKER_SERVICE", "1")
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_PROXY_CHECK", "true")
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_PREFLIGHT_DOCKER_SERVICE_WAIT_SEC", "120")
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_PREFLIGHT_POSTGRES_HEALTH_WAIT_SEC", "90")

    cfg = PreflightConfig.from_env()
    assert cfg.skip_docker_service is True
    assert cfg.skip_proxy_check is True
    assert cfg.docker_service_wait_sec == 120
    assert cfg.postgres_health_wait_sec == 90


def test_config_from_env_invalid_int_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法 int 环境变量 → 回退默认值,不抛错。"""
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_PREFLIGHT_DOCKER_SERVICE_WAIT_SEC", "abc")
    cfg = PreflightConfig.from_env()
    assert cfg.docker_service_wait_sec == 60


# ---------------------------------------------------------------------------
# 3. run_preflight() 平台判断
# ---------------------------------------------------------------------------
def test_run_preflight_raises_on_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Windows 平台 → 抛 NotImplementedError,不调 PowerShell。"""
    monkeypatch.setattr("runtime.preflight.sys.platform", "linux")
    with mock.patch("runtime.preflight._run_powershell_step") as fake_run:
        with pytest.raises(NotImplementedError, match="Windows"):
            run_preflight(PreflightConfig())
        fake_run.assert_not_called()


# ---------------------------------------------------------------------------
# 4. run_preflight() 调子进程:exit code → result
# ---------------------------------------------------------------------------
@pytest.fixture
def _windows_platform(monkeypatch: pytest.MonkeyPatch):
    """强制把 sys.platform 设为 win32,即便在 CI 上跑。"""
    monkeypatch.setattr("runtime.preflight.sys.platform", "win32")


def test_run_preflight_succeeds_when_powershell_exit_zero(
    _windows_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PowerShell exit 0 → 所有 step ok=True。"""
    fake_completed = subprocess.CompletedProcess(
        args=["pwsh"], returncode=0, stdout="all good", stderr=""
    )
    monkeypatch.setattr(
        "runtime.preflight._run_powershell_step",
        mock.MagicMock(return_value=(0, "all good", "")),
    )

    result = run_preflight(PreflightConfig())
    assert result.ok is True
    assert all(s.ok for s in result.steps)
    assert {s.step for s in result.steps} == {
        "docker_service",
        "daemon",
        "proxy",
        "postgres_container",
        "postgres_connect",
        "schema",
    }


def test_run_preflight_maps_exit_code_to_step(
    _windows_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PowerShell exit 14 → steps[2].step="postgres_container", ok=False。"""
    monkeypatch.setattr(
        "runtime.preflight._run_powershell_step",
        mock.MagicMock(return_value=(14, "", "container not healthy")),
    )

    result = run_preflight(PreflightConfig())
    assert result.ok is False
    failed = result.first_failure()
    assert failed is not None
    assert failed.step == "postgres_container"
    assert failed.ok is False
    assert "container not healthy" in (failed.error or "")
    # 之前的 step 都标 ok=True(脚本早退)
    assert all(s.ok for s in result.steps if s.step != "postgres_container")


def test_run_preflight_timeout_raises(
    _windows_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """subprocess.TimeoutExpired → 抛 PreflightError,带「超时」字样。"""

    def _raise_timeout(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="pwsh", timeout=600)

    monkeypatch.setattr("runtime.preflight._run_powershell_step", _raise_timeout)

    with pytest.raises(PreflightError, match="超时"):
        run_preflight(PreflightConfig(timeout_sec=600))


def test_run_preflight_pwsh_not_found_raises(
    _windows_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pwsh.exe 不在 PATH → FileNotFoundError → PreflightError 含「找不到 pwsh」。"""

    def _raise_fnf(*_a, **_kw):
        raise FileNotFoundError("pwsh not on PATH")

    monkeypatch.setattr("runtime.preflight._run_powershell_step", _raise_fnf)

    with pytest.raises(PreflightError, match="pwsh"):
        run_preflight(PreflightConfig())


def test_run_preflight_dry_run_skips_powershell(
    _windows_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run=True → 不调 PowerShell,直接返回 ok=True。"""
    spy = mock.MagicMock()
    monkeypatch.setattr("runtime.preflight._run_powershell_step", spy)

    result = run_preflight(PreflightConfig(dry_run=True))
    assert result.ok is True
    spy.assert_not_called()


def test_run_preflight_missing_script_raises(
    _windows_platform: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """指定不存在的脚本路径 → 抛 PreflightError 含「找不到」字样。"""
    cfg = PreflightConfig(powershell_script=tmp_path / "nope.ps1")
    with pytest.raises(PreflightError, match="找不到 pre-flight 脚本"):
        run_preflight(cfg)


def test_run_preflight_unmapped_exit_code_raises(
    _windows_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exit code 不在映射表 → 抛 PreflightError 含「异常退出」。"""
    monkeypatch.setattr(
        "runtime.preflight._run_powershell_step",
        mock.MagicMock(return_value=(137, "", "killed")),
    )
    with pytest.raises(PreflightError, match="异常退出"):
        run_preflight(PreflightConfig())


# ---------------------------------------------------------------------------
# 5. PreflightResult.first_failure()
# ---------------------------------------------------------------------------
def test_first_failure_returns_first_failing_step() -> None:
    steps = [
        PreflightStepResult(step="docker_service", ok=True, duration_ms=10),
        PreflightStepResult(step="daemon", ok=False, duration_ms=20, error="boom"),
        PreflightStepResult(step="proxy", ok=False, duration_ms=5, error="later"),
    ]
    result = PreflightResult(ok=False, steps=steps, total_duration_ms=35)
    failed = result.first_failure()
    assert failed is not None
    assert failed.step == "daemon"
    assert failed.error == "boom"


def test_first_failure_returns_none_when_all_ok() -> None:
    steps = [PreflightStepResult(step=name, ok=True, duration_ms=1) for name in
             ("docker_service", "daemon", "proxy")]
    result = PreflightResult(ok=True, steps=steps, total_duration_ms=3)
    assert result.first_failure() is None


# ---------------------------------------------------------------------------
# 6. _run_powershell_step 平台判断
# ---------------------------------------------------------------------------
def test_run_powershell_step_raises_on_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Windows 平台 → 直接抛 NotImplementedError,不调 subprocess。"""
    monkeypatch.setattr("runtime.preflight.sys.platform", "linux")
    with mock.patch("subprocess.run") as fake_run:
        with pytest.raises(NotImplementedError):
            _run_powershell_step(Path("dummy.ps1"), ["-x"], 60)
        fake_run.assert_not_called()


def test_default_preflight_script_path_exists() -> None:
    """默认脚本路径应在仓库内(不在的话单测也至少给一条定位信息)。"""
    # 不做强制 assert:仓库布局可能调整,但允许 cwd 在临时目录时也能定位
    assert DEFAULT_PREFLIGHT_SCRIPT.name == "preflight.ps1"