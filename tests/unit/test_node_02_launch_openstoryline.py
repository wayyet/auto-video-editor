"""节点 2 单测(2026-10 启动入口统一后)。

node_02 **不再启动任何进程** —— OpenStoryline Web 的生命周期归
``start_production.ps1`` 管,本节点只做 HTTP 探测。因此:

- human 模式探测成功 → ``openstoryline_ready=True`` + ``openstoryline_pid is None``
  + ``status_log`` 含 ``node_02_openstoryline_reused``
- human 模式探测失败 → ``openstoryline_ready=False`` + ``pid is None``
  + ``status_log`` 含 ``node_02_launch_openstoryline_failed`` + ``error_log`` 有记录
- auto 模式 → 行为不变(``openstoryline_ready=True`` + ``auto_skipped`` 标记)
- **任何模式都不会调用进程工厂** —— 本模块已没有 ``subprocess`` / ``popen_factory``
"""

from __future__ import annotations

import inspect
from importlib import reload

import pytest

from nodes.node_02_launch_openstoryline import launch_openstoryline_service


def test_human_mode_reuses_existing_service_no_process_started() -> None:
    """探测成功 → ready=True + pid=None + reused 标记,全程不启动进程。"""
    state: dict = {"error_log": []}

    def fake_checker(url, timeout_s):
        return True

    out = launch_openstoryline_service(state, health_checker=fake_checker)
    assert out["openstoryline_ready"] is True
    # 本节点不拥有进程 → 不记 PID
    assert out["openstoryline_pid"] is None
    assert out["openstoryline_web_url"].startswith("http://127.0.0.1:")
    assert out["openstoryline_mcp_endpoint"].startswith("http://127.0.0.1:")
    assert out["error_log"] == []
    assert "node_02_openstoryline_reused" in (out.get("status_log") or [])
    assert "node_02_launch_openstoryline_failed" not in (out.get("status_log") or [])


def test_human_mode_not_ready_records_error_no_raise() -> None:
    """探测失败 → ready=False,pid=None,error_log 有记录,节点不抛异常。"""
    state: dict = {"error_log": []}

    def fake_checker(url, timeout_s):
        return False  # 全部超时

    out = launch_openstoryline_service(state, health_checker=fake_checker)
    assert out["openstoryline_ready"] is False
    assert out["openstoryline_pid"] is None
    assert any("未就绪" in e for e in out["error_log"])
    assert "node_02_launch_openstoryline_failed" in (out.get("status_log") or [])


def test_module_has_no_process_launch_capability() -> None:
    """node_02 已彻底去掉进程启动能力(无 subprocess / popen_factory)。"""
    import nodes.node_02_launch_openstoryline as m2

    assert not hasattr(m2, "subprocess"), "node_02 不应再 import subprocess"
    assert not hasattr(m2, "_default_popen"), "node_02 不应再有默认进程工厂"

    params = inspect.signature(m2.launch_openstoryline_service).parameters
    assert "popen_factory" not in params, "launch_openstoryline_service 不应再有 popen_factory 形参"
    assert "health_checker" in params, "health_checker 必须保留(探测注入点)"


def test_auto_mode_skips_probe_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """auto 模式 → 不探测、不启动,直接放行(auto_skipped 标记)。"""
    monkeypatch.setenv("STORYLINE_MODE", "auto")

    import config as _cfg

    reload(_cfg)
    import nodes.node_02_launch_openstoryline as _n2

    reload(_n2)

    def boom_checker(url, timeout_s):
        raise AssertionError("auto 模式不应发起健康探测")

    state: dict[str, object] = {"error_log": [], "session_id": "auto_test"}
    out = _n2.launch_openstoryline_service(state, health_checker=boom_checker)
    assert out["openstoryline_ready"] is True
    assert out["openstoryline_pid"] is None
    assert any(
        t == "node_02_launch_openstoryline_auto_skipped"
        for t in out.get("status_log", [])
    ), f"缺少 auto_skipped 标记: status_log={out.get('status_log')}"
    assert out.get("error_log") == []
