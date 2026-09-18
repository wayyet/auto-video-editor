"""pre-flight Windows-only 集成测试。

覆盖范围:
1. 现有 84 条测试零改动 — ``build_graph(run_preflight=False)`` 是默认。
2. ``build_graph(run_preflight=True, ...)`` 在 ``_run_powershell_step`` mock 失败时
   抛 ``PreflightError``。
3. ``run_preflight(skip_docker_service=True, skip_proxy_check=True, skip_schema_setup=True)``
   直接调 PowerShell 子进程,只跑 Step 4/5(基线性能 < 30s,见验收 §3)。

所有真正动 Docker/Postgres 容器启停的测试都带 ``@pytest.mark.admin`` 标记,
由用户按需显式 ``-m admin`` 触发,默认 ``pytest`` 不跑。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from unittest import mock

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only 集成测试:依赖 Docker Desktop Windows 服务 + pwsh.exe",
)


def _is_windows_admin() -> bool:
    """检测当前进程是否有 Windows 管理员权限(用于 @pytest.mark.admin 测试的自动 skip)。"""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 — 探测函数永远不抛
        return False


# @pytest.mark.admin 测试在没有管理员权限时自动跳过,避免默认 pytest 跑卡死。
_admin_skipif = pytest.mark.skipif(
    not _is_windows_admin(),
    reason="需要本地管理员权限(以 -m admin 显式启用)",
)


# ---------------------------------------------------------------------------
# 默认参数不破坏既有测试(零改动验证)
# ---------------------------------------------------------------------------
def test_build_graph_default_run_preflight_false_does_not_call_powershell(tmp_path) -> None:
    """默认 ``run_preflight=False`` → 不调 PowerShell,沿用 84 条测试路径。"""
    # 动态 import 避免非 Windows 平台加载 graph 模块失败
    if sys.platform != "win32":
        pytest.skip("Windows-only")

    from langgraph.checkpoint.memory import InMemorySaver

    from graph import build_graph

    with mock.patch("runtime.preflight._run_powershell_step") as fake_run:
        g = build_graph(
            checkpointer=InMemorySaver(),
            start_heartbeat_thread=False,
            # run_preflight=False 是默认,显式不传
        )
        assert g is not None
        fake_run.assert_not_called()


def test_build_graph_run_preflight_true_raises_on_powershell_failure(tmp_path) -> None:
    """``run_preflight=True`` + PowerShell 失败 → 抛 PreflightError。"""
    from langgraph.checkpoint.memory import InMemorySaver

    from graph import build_graph
    from runtime.preflight import PreflightError

    with mock.patch(
        "runtime.preflight._run_powershell_step",
        return_value=(11, "", "需要管理员权限"),
    ):
        with pytest.raises(PreflightError, match="管理员权限"):
            build_graph(
                checkpointer=InMemorySaver(),
                start_heartbeat_thread=False,
                run_preflight=True,
            )


# ---------------------------------------------------------------------------
# 基线性能:仅启 Postgres 容器(已运行的 Docker Desktop)
# ---------------------------------------------------------------------------
@pytest.mark.admin
@pytest.mark.slow
@_admin_skipif
def test_preflight_skip_most_steps_baseline_performance() -> None:
    """``-SkipDockerService -SkipProxyCheck -SkipSchemaSetup`` 跑完整 pre-flight,
    应在 30s 内完成(验收标准 §3)。

    需要:
    - 本机 Docker Desktop 已在运行(Docker daemon 在线)
    - 已装 edb-postgres16 容器镜像
    """
    from runtime.preflight import PreflightConfig, run_preflight

    cfg = PreflightConfig(
        skip_docker_service=True,
        skip_proxy_check=True,
        skip_schema_setup=True,
        docker_service_wait_sec=60,
        postgres_health_wait_sec=60,
        timeout_sec=120,
    )
    result = run_preflight(cfg)
    # 基线断言:总耗时 < 30s
    assert result.total_duration_ms < 30_000, (
        f"pre-flight 超基线:{result.total_duration_ms} ms (>30s);steps={result.steps}"
    )
    # Postgres 容器这一步必通过(其他可能被环境 skip)
    container_step = next(
        (s for s in result.steps if s.step == "postgres_container"), None
    )
    assert container_step is not None
    assert container_step.ok is True


# ---------------------------------------------------------------------------
# 真实启停 Docker 服务/容器 — 需 admin,默认 skip
# ---------------------------------------------------------------------------
@pytest.mark.admin
@_admin_skipif
def test_preflight_starts_stopped_postgres_container() -> None:
    """edb-postgres16 已存在但 Stopped → pre-flight 应自动 ``docker start`` 并等 healthy。"""
    from runtime.preflight import PreflightConfig, run_preflight

    # 强制先停容器,前提:已存在 edb-postgres16
    subprocess.run(
        ["docker", "stop", "edb-postgres16"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    try:
        cfg = PreflightConfig(
            skip_docker_service=True,
            skip_proxy_check=True,
            skip_schema_setup=True,
            timeout_sec=120,
        )
        result = run_preflight(cfg)
        assert result.ok, f"pre-flight 失败:{result.steps}"
        container_step = next(
            (s for s in result.steps if s.step == "postgres_container"), None
        )
        assert container_step is not None
        assert container_step.ok is True
    finally:
        # 还原:停掉容器(测试副作用清理)
        subprocess.run(
            ["docker", "stop", "edb-postgres16"],
            capture_output=True,
            check=False,
            timeout=30,
        )


@pytest.mark.admin
@_admin_skipif
def test_preflight_starts_stopped_docker_service() -> None:
    """com.docker.service Stopped → pre-flight 应 ``Start-Service`` 拉起。"""
    from runtime.preflight import PreflightConfig, run_preflight

    # 强制先停服务(需 admin)
    stop = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Stop-Service -Name com.docker.service -Force"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if stop.returncode != 0:
        pytest.skip(f"停服务失败(可能缺 admin):{stop.stderr}")

    try:
        cfg = PreflightConfig(
            skip_proxy_check=True,
            skip_postgres_start=True,
            skip_schema_setup=True,
            docker_service_wait_sec=60,
            timeout_sec=120,
        )
        result = run_preflight(cfg)
        assert result.ok, f"pre-flight 失败:{result.steps}"
        # Step 1 应已起服务
        ds = next((s for s in result.steps if s.step == "docker_service"), None)
        assert ds is not None
        assert ds.ok is True
    finally:
        # 还原:重新停服务,避免干扰其他测试
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Stop-Service -Name com.docker.service -Force"],
            capture_output=True,
            check=False,
            timeout=60,
        )


# ---------------------------------------------------------------------------
# run_workflow.py 入口的烟雾测试
# ---------------------------------------------------------------------------
def test_run_workflow_skip_preflight_returns_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--skip-preflight`` → 直接进 LangGraph,不调 pre-flight。

    测试方法:替换 ``runtime.preflight._run_powershell_step`` 为 spy,
    调用 ``run_workflow.main()`` 配合 ``--skip-preflight``,
    断言 spy 未被调用。
    """
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    from scripts.run_workflow import main as run_main  # type: ignore[import-not-found]

    # 跳过 ainvoke 实际跑图:替换 build_graph 让它直接返回 mock
    with mock.patch("runtime.preflight._run_powershell_step") as fake_pwsh, \
         mock.patch("graph.build_graph", return_value=mock.MagicMock(invoke=mock.MagicMock(return_value={}))):
        monkeypatch.setattr(
            sys, "argv", ["run_workflow.py", "--skip-preflight", "--thread-id", "ci-test"]
        )
        rc = run_main()
        assert rc == 0
        fake_pwsh.assert_not_called()