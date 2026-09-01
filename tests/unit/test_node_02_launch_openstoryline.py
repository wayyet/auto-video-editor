"""节点 2 单测(附件 1.3 节要点)。"""

from __future__ import annotations

import pytest

from nodes.node_02_launch_openstoryline import launch_openstoryline_service


class _StubProc:
    def __init__(self, pid: int = 999) -> None:
        self.pid = pid
        self.stdout = None
        self.stderr = None


def test_ready_true_when_health_check_succeeds() -> None:
    """健康检查通过 → ready=True。"""
    state: dict = {"error_log": []}

    def fake_popen(*args, **kwargs):
        return _StubProc(pid=1234)

    def fake_checker(url, timeout_s):
        return True

    out = launch_openstoryline_service(
        state,
        popen_factory=fake_popen,
        health_checker=fake_checker,
    )
    assert out["openstoryline_ready"] is True
    assert out["openstoryline_pid"] == 1234
    assert out["openstoryline_web_url"].startswith("http://127.0.0.1:")
    assert out["openstoryline_mcp_endpoint"].startswith("http://127.0.0.1:")
    assert out["error_log"] == []


def test_ready_false_when_health_check_times_out_no_raise() -> None:
    """健康检查超时 → ready=False,但节点不抛异常,记录到 error_log。"""
    state: dict = {"error_log": []}

    def fake_popen(*args, **kwargs):
        return _StubProc(pid=5678)

    def fake_checker(url, timeout_s):
        return False  # 全部超时

    out = launch_openstoryline_service(
        state,
        popen_factory=fake_popen,
        health_checker=fake_checker,
    )
    assert out["openstoryline_ready"] is False
    assert out["openstoryline_pid"] == 5678  # 进程起来了,只是健康检查没过
    assert any("健康检查超时" in e for e in out["error_log"])


def test_popen_failure_records_error_and_ready_false() -> None:
    """subprocess.Popen 抛 FileNotFoundError → ready=False,pid=None,error_log 有记录。"""
    state: dict = {"error_log": []}

    def fake_popen(*args, **kwargs):
        raise FileNotFoundError("python not found")

    out = launch_openstoryline_service(state, popen_factory=fake_popen)
    assert out["openstoryline_ready"] is False
    assert out["openstoryline_pid"] is None
    assert any("启动 OpenStoryline 失败" in e for e in out["error_log"])
