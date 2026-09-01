"""编排进程心跳写入器(Week 3, 对照附件 6.1 节)。

背景线程持续把 epoch 时间戳写入 ``HEARTBEAT_FILE``,外部
``heartbeat_monitor.ps1`` 每 2 分钟轮询,超阈值则告警。

Week 3 集成点: ``graph.build_graph()`` 默认在图编译时启动;
Week 4 在 main 入口调用可由 build_graph 的 ``start_heartbeat`` 参数关闭。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

# 默认落到 C:\ProgramData\VideoWorkflow\heartbeat.txt — 监控脚本读取同一路径。
HEARTBEAT_FILE: Path = Path(r"C:\ProgramData\VideoWorkflow\heartbeat.txt")
HEARTBEAT_INTERVAL_SECONDS: int = 10

_thread: threading.Thread | None = None
_lock = threading.Lock()


def start_heartbeat(
    heartbeat_file: Path | None = None,
    interval: int = HEARTBEAT_INTERVAL_SECONDS,
) -> threading.Thread:
    """启动后台心跳线程,返回 Thread 句柄(daemon=True, 主进程退出时自动终止)。

    Args:
        heartbeat_file: 心跳时间戳落盘路径,None 时用全局默认值。
        interval: 写入间隔秒数。

    Returns:
        已启动的 Thread 对象(daemon=True)。
    """
    target = heartbeat_file or HEARTBEAT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)

    def _loop() -> None:
        while True:
            try:
                target.write_text(str(time.time()), encoding="utf-8")
            except Exception:  # noqa: BLE001 — 心跳写入失败不应拖垮编排进程
                pass
            time.sleep(interval)

    with _lock:
        global _thread
        if _thread is not None and _thread.is_alive():
            return _thread
        _thread = threading.Thread(target=_loop, daemon=True, name="heartbeat-writer")
        _thread.start()
    return _thread


def stop_heartbeat() -> None:
    """停止心跳线程(主要用于单测)。目前 _loop 是无限循环,无法优雅终止;daemon=True 保证进程退出时清理。"""
    # 占位:Week 4 可引入 threading.Event 让 _loop 可优雅停止。当前 daemon 模式已足够。
    pass