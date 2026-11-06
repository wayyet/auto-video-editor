"""读当前 Thread 的 LangGraph checkpoint(gates 展示 / 恢复门禁共用取数层)。

2026-11-06 阶段 2 起 ``api_console`` 的 gates 端点需要读 interrupt payload;
阶段 3 的恢复前文件安全门禁(``console_service.resume_run``)也需要同一份
checkpoint state(draft_path 等)。本模块把这条取数链收敛到一处:

- 独立线程 + ``asyncio.run(loop_factory=SelectorEventLoop)``—— Windows uvicorn
  主循环是 Proactor,async psycopg 不兼容(与 resume_workflow.py 修复同款纪律);
- 只读 ``aget_state``,不写库、不跑图、不触发恢复。

另含恢复前门禁 :func:`resume_guard`(阶段 3):剪映必须已关闭 + 草稿文件
存在/JSON 可解析/mtime 合理,并返回审计 SHA-256。
"""

from __future__ import annotations

import asyncio
import selectors
import sys
from pathlib import Path
from typing import Any

from web.human_gate_payload import build_gate_views


def fetch_snapshot(thread_id: str):
    """读 thread 的 state snapshot(独立事件循环;进程内不做任何缓存)。"""
    from graph import build_graph, get_checkpointer, normalize_thread_id

    async def _inner():
        tid = normalize_thread_id(thread_id)
        async with get_checkpointer() as saver:
            g = build_graph(
                checkpointer=saver,
                thread_id=tid,
                start_heartbeat_thread=False,
                run_preflight=False,
            )
            return await g.aget_state({"configurable": {"thread_id": tid}})

    if sys.platform == "win32":
        return asyncio.run(
            _inner(),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(_inner())


def fetch_gate_data(thread_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """返回(挂起关卡展示载荷列表, state values)。"""
    from scripts.resume_workflow import describe_interrupts

    snap = fetch_snapshot(thread_id)
    interrupts = describe_interrupts(snap)
    values = dict(snap.values or {})
    return build_gate_views(interrupts, values), values


def fetch_state_values(thread_id: str) -> dict[str, Any]:
    """只取 state values(恢复门禁用,不构造展示载荷)。"""
    return dict(fetch_snapshot(thread_id).values or {})


def resume_guard(
    thread_id: str,
    *,
    require_jianying_closed: bool | None = None,
    proc_checker: Any = None,
) -> dict[str, Any]:
    """恢复前文件安全门禁(计划 §8 阶段 3)。

    - 剪映必须已关闭(:class:`config.JY_RESUME_REQUIRE_CLOSED`,默认开);
    - ``draft_path`` 存在 / JSON 可解析 / mtime 合理 / 目录未被占用
      (:func:`draft_ops.draft_lock.validate_draft_for_resume`);
    - 通过时返回对人工修改后草稿的 SHA-256(写任务 manifest 审计)。

    Returns:
        ``{"ok": bool, "problems": [...], "hashes": {...}}``。
    """
    from draft_ops.draft_lock import assert_resume_ready

    values = fetch_state_values(thread_id)
    result = assert_resume_ready(
        values.get("draft_path"),
        proc_checker=proc_checker,
        require_jianying_closed=require_jianying_closed,
    )
    return {
        "ok": result["ok"],
        "problems": result["problems"],
        "hashes": result["hashes"],
    }


__all__ = [
    "fetch_gate_data",
    "fetch_snapshot",
    "fetch_state_values",
    "resume_guard",
]
