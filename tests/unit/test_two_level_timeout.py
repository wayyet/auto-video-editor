"""两级超时单元测试 — Week 3 补全 §11/P1-3 接入验证。

覆盖:
(a) ``ENABLE_TWO_LEVEL_TIMEOUT=false``(默认)→ watchdog 行为旁路
(b) ``invoke_with_total_timeout`` 超时触发 → 抛 ``TotalExecutionTimeoutError``
(c) ``invoke_with_total_timeout`` 未超时 → 透传结果
(d) ``start_node_inactivity_watchdog`` idle 超时 → on_timeout 回调触发
(e) ``start_node_inactivity_watchdog`` 节点持续活动 → 不触发
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from monitoring.timeout_watchdog import (
    NodeTimeoutError,
    TotalExecutionTimeoutError,
    invoke_with_total_timeout,
    start_node_inactivity_watchdog,
)


# ---------------------------------------------------------------------------
# (a) 默认关闭 — 透传,不启用 watchdog
# ---------------------------------------------------------------------------
def test_default_off_invoke_with_total_timeout_passthrough() -> None:
    """ENABLE_TWO_LEVEL_TIMEOUT=false(默认):invoke_with_total_timeout 透传。"""
    graph = MagicMock()
    graph.invoke.return_value = {"ok": True}

    out = invoke_with_total_timeout(graph, {"k": "v"}, config=None, timeout_s=10)
    assert out == {"ok": True}
    graph.invoke.assert_called_once()


# ---------------------------------------------------------------------------
# (b) + (c) 超时触发 + 透传 — 通过 monkeypatch ENABLE_TWO_LEVEL_TIMEOUT
# ---------------------------------------------------------------------------
@pytest.fixture
def enable_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("monitoring.timeout_watchdog.ENABLE_TWO_LEVEL_TIMEOUT", True)


def test_invoke_with_total_timeout_triggers_on_slow_graph(enable_timeout) -> None:
    """启用 + graph.invoke 阻塞 > timeout_s → 抛 TotalExecutionTimeoutError。"""
    error_log: list[str] = []

    def slow_invoke(_state, _config):
        time.sleep(3)
        return {"ok": True}

    graph = MagicMock()
    graph.invoke.side_effect = slow_invoke

    with pytest.raises(TotalExecutionTimeoutError) as exc_info:
        invoke_with_total_timeout(
            graph, {"k": "v"}, config=None, timeout_s=1,
            error_log_sink=error_log,
        )
    assert "总执行时长" in str(exc_info.value)
    assert any("总执行时长" in e for e in error_log)


def test_invoke_with_total_timeout_passes_through_when_fast(enable_timeout) -> None:
    """启用 + graph.invoke 快于 timeout_s → 透传结果。"""
    graph = MagicMock()
    graph.invoke.return_value = {"result": "fast"}

    out = invoke_with_total_timeout(graph, {"k": "v"}, config=None, timeout_s=5)
    assert out == {"result": "fast"}


# ---------------------------------------------------------------------------
# (d) + (e) 单节点无响应 watchdog
# ---------------------------------------------------------------------------
def test_node_inactivity_watchdog_triggers_on_timeout(enable_timeout) -> None:
    """启用 + 节点 5 秒无活动 → on_timeout 回调触发。"""
    state_ref: dict = {"__last_activity_ts": time.monotonic() - 100}  # 已 idle 100s
    fired = threading.Event()

    def on_timeout():
        fired.set()

    thread = start_node_inactivity_watchdog(
        state_ref, timeout_s=10, poll_interval_s=0.1, on_timeout=on_timeout
    )
    fired.wait(timeout=2)

    assert fired.is_set(), "watchdog 应触发 on_timeout"
    assert any("无响应" in e for e in state_ref.get("error_log", []))
    thread.join(timeout=1)


def test_node_inactivity_watchdog_no_trigger_when_active(enable_timeout) -> None:
    """启用 + 节点持续活动(idle < timeout_s)→ 不触发。"""
    state_ref: dict = {"__last_activity_ts": time.monotonic()}

    # 启动 watchdog,在背景线程中持续更新 __last_activity_ts
    fired = threading.Event()

    def on_timeout():
        fired.set()

    thread = start_node_inactivity_watchdog(
        state_ref, timeout_s=2, poll_interval_s=0.2, on_timeout=on_timeout
    )

    # 持续刷新活动时间 1.5s,期间 watchdog 不应触发
    def keep_active():
        for _ in range(8):
            state_ref["__last_activity_ts"] = time.monotonic()
            time.sleep(0.2)

    refresher = threading.Thread(target=keep_active, daemon=True)
    refresher.start()
    refresher.join(timeout=2.0)

    assert not fired.is_set(), "持续活动时 watchdog 不应触发"
    thread.join(timeout=1)


def test_node_inactivity_watchdog_silent_without_callback(enable_timeout) -> None:
    """启用 + 无 on_timeout 回调 → 仅写 error_log,线程静默退出,不抛异常。"""
    state_ref: dict = {"__last_activity_ts": time.monotonic() - 100}

    thread = start_node_inactivity_watchdog(
        state_ref, timeout_s=10, poll_interval_s=0.1
    )
    # 等 watchdog 检查一轮
    time.sleep(0.5)
    thread.join(timeout=1)
    assert not thread.is_alive(), "无 on_timeout 时 watchdog 应自行退出"

    assert any("无响应" in e for e in state_ref.get("error_log", [])), \
        "watchdog 触发时应在 state.error_log 写入提示"


def test_node_timeout_error_can_be_raised_manually(enable_timeout) -> None:
    """若调用方想主动抛 NodeTimeoutError,仍可正常使用(不依赖 watchdog)。"""
    state_ref: dict = {"__last_activity_ts": time.monotonic() - 100}
    fired = threading.Event()

    def raise_it():
        fired.set()
        raise NodeTimeoutError("forced")

    thread = start_node_inactivity_watchdog(
        state_ref, timeout_s=10, poll_interval_s=0.1, on_timeout=raise_it
    )
    fired.wait(timeout=2)
    assert fired.is_set()
    # 不 join — on_timeout 抛异常后线程会异常退出,让 daemon 自然终止
    time.sleep(0.2)


def test_node_timeout_error_is_runtime_error() -> None:
    """NodeTimeoutError 应继承自 RuntimeError。"""
    assert issubclass(NodeTimeoutError, RuntimeError)


def test_total_execution_timeout_error_is_runtime_error() -> None:
    """TotalExecutionTimeoutError 应继承自 RuntimeError。"""
    assert issubclass(TotalExecutionTimeoutError, RuntimeError)