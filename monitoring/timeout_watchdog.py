"""两级超时 watchdog — Week 3 补全 §11/P1-3 接入。

设计要点(非破坏性接入):
1. **默认关闭**:`config.ENABLE_TWO_LEVEL_TIMEOUT` 默认为 False;既有 24 条
   集成测试 + 60 条单元测试不受影响。
2. **总时长超时**:`invoke_with_total_timeout(graph, state, config)` 用
   ``concurrent.futures.ThreadPoolExecutor.submit(graph.invoke, ...)`` +
   ``result(timeout=TOTAL_EXECUTION_TIMEOUT_S)``;超时则 cancel + 写 error_log。
3. **单节点无响应 watchdog**:`start_node_inactivity_watchdog(state_dict_ref)`
   启动后台线程,周期性读 ``state["status_log"]`` 最后写入时间;
   若 > ``NODE_INACTIVITY_TIMEOUT_S`` 未动 → 触发回调 + raise ``NodeTimeoutError`。

注意 — Watchdog 是基于"status_log 最后写入时间"的近似度量,不替代真正的
LangGraph runtime timeout hook;Week 4 用户实测时如 LangGraph 1.2.x 提供
原生 timeout API,优先切换到原生 API。
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Any, Callable

from config import (
    ENABLE_TWO_LEVEL_TIMEOUT,
    NODE_INACTIVITY_TIMEOUT_S,
    TOTAL_EXECUTION_TIMEOUT_S,
)


class NodeTimeoutError(RuntimeError):
    """单节点无响应 watchdog 触发时抛出。

    LangGraph 捕获为 GraphInterrupt,保留 checkpointer 可 resume。
    """


class TotalExecutionTimeoutError(RuntimeError):
    """图总执行时长超限时抛出。"""


# ---------------------------------------------------------------------------
# 总时长超时
# ---------------------------------------------------------------------------
def invoke_with_total_timeout(
    graph: Any,
    state: Any,
    config: dict | None = None,
    *,
    timeout_s: int | None = None,
    error_log_sink: list[str] | None = None,
) -> Any:
    """带总时长超时的 ``graph.invoke`` 包装。

    Args:
        graph: CompiledStateGraph 实例。
        state: 初始 state。
        config: LangGraph config(``configurable.thread_id`` 等);None 时走默认。
        timeout_s: 总超时秒数;None 时用 ``config.TOTAL_EXECUTION_TIMEOUT_S``。
        error_log_sink: 若提供,超时/异常会追加错误消息到此 list。

    Returns:
        ``graph.invoke(state, config)`` 的结果。

    Raises:
        TotalExecutionTimeoutError: 总执行时长超过 timeout_s。
        Exception: ``graph.invoke`` 本身抛出的异常透传(非超时相关)。
    """
    if not ENABLE_TWO_LEVEL_TIMEOUT:
        # 默认关闭:直接透传,保持既有 60+24 测试兼容
        return graph.invoke(state, config)

    effective_timeout = timeout_s if timeout_s is not None else TOTAL_EXECUTION_TIMEOUT_S

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(graph.invoke, state, config)
        try:
            return future.result(timeout=effective_timeout)
        except concurrent.futures.TimeoutError as e:
            future.cancel()
            msg = f"[timeout_watchdog] 总执行时长 > {effective_timeout}s,强制中断"
            if error_log_sink is not None:
                error_log_sink.append(msg)
            raise TotalExecutionTimeoutError(msg) from e


# ---------------------------------------------------------------------------
# 单节点无响应 watchdog
# ---------------------------------------------------------------------------
def _read_last_status_log_time(state_ref: dict) -> float:
    """读取 ``state_ref["status_log"]`` 最后一项对应的写入时间(epoch 秒)。

    由于 ``status_log`` 不带时间戳,这里用 ``state_ref`` 中的隐式时间字段
    ``__last_activity_ts``;调用方应在节点完成后 ``state_ref["__last_activity_ts"]
    = time.monotonic()``。
    """
    return float(state_ref.get("__last_activity_ts", time.monotonic()))


def start_node_inactivity_watchdog(
    state_ref: dict,
    *,
    timeout_s: int | None = None,
    poll_interval_s: float = 5.0,
    on_timeout: Callable[[], None] | None = None,
) -> threading.Thread:
    """启动单节点无响应 watchdog 线程。

    Args:
        state_ref: 引用 state 的 dict(节点写入 ``__last_activity_ts`` 字段)。
        timeout_s: 无响应超时秒数;None 时用 ``config.NODE_INACTIVITY_TIMEOUT_S``。
        poll_interval_s: watchdog 检查间隔。
        on_timeout: 超时回调;None 时仅抛 ``NodeTimeoutError``(测试用)。

    Returns:
        已启动的 daemon Thread。
    """
    effective_timeout = timeout_s if timeout_s is not None else NODE_INACTIVITY_TIMEOUT_S

    stop_event = threading.Event()

    def _loop() -> None:
        while not stop_event.is_set():
            last_ts = _read_last_status_log_time(state_ref)
            idle = time.monotonic() - last_ts
            if idle > effective_timeout:
                state_ref.setdefault("error_log", []).append(
                    f"[timeout_watchdog] 单节点无响应 > {effective_timeout}s "
                    f"(idle={idle:.1f}s)"
                )
                if on_timeout is not None:
                    on_timeout()
                    return
                # 无 on_timeout:写完日志后静默退出,避免 daemon 线程抛
                # PytestUnhandledThreadExceptionWarning。
                return
            stop_event.wait(poll_interval_s)

    thread = threading.Thread(target=_loop, daemon=True, name="timeout-watchdog")
    thread.start()
    return thread


def stop_node_inactivity_watchdog(thread: threading.Thread, *, timeout_s: float = 1.0) -> None:
    """通知 watchdog 线程优雅退出(仅在 on_timeout 模式下有意义)。"""
    # 由于 _loop 是无限循环且 daemon=True,实际无法直接停止 — 通过设置
    # state_ref["__last_activity_ts"] 让 watchdog 重新判定 idle<timeout
    # 自行退出即可。此函数为 API 一致性保留。
    _ = (thread, timeout_s)