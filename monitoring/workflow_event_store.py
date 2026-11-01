"""``workflow_run_events`` 事件存储(PostgreSQL-only,设计《工作流流程图与实时节点追踪》§6.2-D)。

分工与设计要点
--------------
- 与 ``workflow_runs`` 台账分工:台账保留线程**最新状态**(单行覆盖式);
  本表保存**逐节点事件序列**(append-only),供 SSE 断线补发与整场回放。
- 游标 = 全局 ``BIGSERIAL id``:``WHERE thread_id = $1 AND id > $2`` 一条索引
  即可完成 Last-Event-ID 补发;恢复运行同 ``thread_id`` 续写,游标天然不回退。
- **先落库,后推送**(设计 §6.2-E 实现要点 2):``append_event`` 由 runner 在
  通知任何进程内广播器**之前**调用。
- URI 走统一 DSN 解析层 ``db.postgres_config.resolve_postgres_target``(与
  checkpointer / agent_gate worker 同一套优先级,361cc13 收敛语义)。
- 连接复用 ``db.connection``(client_encoding=UTF-8;本机库 SQL_ASCII,
  不设的话中文查询串直接报错)。
- **降级语义沿用 ``monitoring/runs_table.py`` 风格**:存储不可用时调用返回
  ``None`` / ``[]`` / ``{"available": False}``,同时打 ERROR 日志并累加
  ``degraded_count()`` —— 但**语义与台账相反**:事件丢失意味着实时历史不可用,
  上层(/events)必须显式报错,不得静默把已开始的任务显示为成功(设计 §6.2-D)。

JSONB 写入:payload 由本模块 ``json.dumps(..., ensure_ascii=False)`` 后以
``%s::jsonb`` 写入,不依赖 psycopg 的 ``Json`` 包装与进程级
``set_json_dumps`` 全局设置(自包含,谁先 import 都安全)。
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "EVENTS_TABLE",
    "append_event",
    "fetch_events_after",
    "latest_snapshot",
    "degraded_count",
]

EVENTS_TABLE = "workflow_run_events"

#: 单次补发的默认上限(SSE 重连补发按此分页;游标向前推进即可拿完)
DEFAULT_FETCH_LIMIT = 500
#: latest_snapshot 扫描 attempt 汇总的最大事件数
_SNAPSHOT_SCAN_LIMIT = 2000

# ---------------------------------------------------------------------------
# 降级计数(runs_table 风格:ERROR 日志 + 计数,可观测)
# ---------------------------------------------------------------------------
_degraded_count = 0
_degraded_lock = threading.Lock()


def degraded_count() -> int:
    """本进程累计的事件存储降级次数;> 0 说明事件没有真正落库,应当告警。"""
    with _degraded_lock:
        return _degraded_count


def _record_degraded(operation: str, exc: BaseException) -> None:
    global _degraded_count
    with _degraded_lock:
        _degraded_count += 1
        total = _degraded_count
    logger.error(
        "[workflow_event_store] %s failed (%s: %s) — 已降级(累计 %s 次);"
        "事件未落库,实时历史/断线补发不可用,需排查 Postgres",
        operation, type(exc).__name__, exc, total,
    )


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def _target_uri() -> str:
    from db.postgres_config import resolve_postgres_target

    return resolve_postgres_target().uri


@contextmanager
def _conn() -> Iterator[Any]:
    """一次性同步连接(与 runs_table 同款; psycopg 是同步驱动)。"""
    from db.connection import connection as _db_connection

    with _db_connection(_target_uri()) as conn:
        yield conn


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def append_event(event: dict[str, Any]) -> Optional[int]:
    """追加一条事件并返回分配的全局 ``id``(SSE 游标);失败降级返回 ``None``。

    调用方(runner)约定:**先**调用本函数,**再**通知进程内广播器 ——
    保证补发序与推送序一致(设计 §6.2-E)。

    Returns:
        新事件的全局 id;存储不可用/写入失败时 ``None``(已记 ERROR + 计数)。
    """
    from monitoring.workflow_events import validate_event

    try:
        ev = validate_event(event)
        payload_json = json.dumps(ev["payload"], ensure_ascii=False)
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {EVENTS_TABLE} "
                    "(thread_id, run_id, event_type, node_id, attempt, occurred_at, payload) "
                    "VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s::jsonb) "
                    "RETURNING id",
                    (
                        ev["thread_id"], ev["run_id"], ev["event_type"], ev["node_id"],
                        ev["attempt"], ev["occurred_at"], payload_json,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row[0])
    except Exception as e:  # noqa: BLE001 — 降级语义见模块 docstring
        _record_degraded(
            f"append_event({event.get('event_type')!r}, thread={event.get('thread_id')!r})", e
        )
        return None


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def _row_to_event(row: Any, cols: list[str]) -> dict[str, Any]:
    data = dict(zip(cols, row))
    occurred = data.get("occurred_at")
    if isinstance(occurred, datetime):
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        data["occurred_at"] = (
            occurred.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
    if isinstance(data.get("payload"), (str, bytes)):
        data["payload"] = json.loads(data["payload"])
    data["id"] = int(data["id"]) if data.get("id") is not None else None
    return data


_FETCH_COLUMNS = "id, thread_id, run_id, event_type, node_id, attempt, occurred_at, payload"


def fetch_events_after(
    thread_id: str,
    last_id: int = 0,
    limit: int = DEFAULT_FETCH_LIMIT,
) -> list[dict[str, Any]]:
    """按游标补发:``thread_id`` 下 ``id > last_id`` 的事件,``id`` 升序。

    SSE 断线重连用(``Last-Event-ID`` = 最后收到的 ``id``);游标向前分页即可
    取完全部历史。存储不可用时返回 ``[]``(记 ERROR + 降级计数)—— 上层必须
    把"空结果"与"确实没有更多事件"区分开(结合 :func:`latest_snapshot`)。
    """
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_FETCH_COLUMNS} FROM {EVENTS_TABLE} "
                    "WHERE thread_id = %s AND id > %s "
                    "ORDER BY id ASC LIMIT %s",
                    (thread_id, int(last_id), int(limit)),
                )
                cols = [d.name for d in cur.description]
                rows = cur.fetchall()
        return [_row_to_event(r, cols) for r in rows]
    except Exception as e:  # noqa: BLE001
        _record_degraded(f"fetch_events_after({thread_id!r}, after={last_id})", e)
        return []


def latest_snapshot(thread_id: str) -> dict[str, Any]:
    """线程的事件摘要快照(前端冷启动/恢复用的兜底视图)。

    Returns:
        dict::

            {
              "thread_id": ...,
              "available": True/False,   # False = 存储不可用(降级)
              "event_count": int,
              "first_id": int | None,
              "last_id": int | None,     # 可作为 SSE 起始游标
              "last_event": dict | None,
              "node_attempts": {node_id: 最大 attempt},   # 只统计 node.* 事件
            }
    """
    empty = {
        "thread_id": thread_id,
        "available": False,
        "event_count": 0,
        "first_id": None,
        "last_id": None,
        "last_event": None,
        "node_attempts": {},
    }
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*), MIN(id), MAX(id) FROM {EVENTS_TABLE} "
                    "WHERE thread_id = %s",
                    (thread_id,),
                )
                total, first_id, last_id = cur.fetchone()
                node_attempts: dict[str, int] = {}
                if total:
                    cur.execute(
                        f"SELECT node_id, MAX(attempt) FROM {EVENTS_TABLE} "
                        "WHERE thread_id = %s AND node_id IS NOT NULL "
                        "GROUP BY node_id",
                        (thread_id,),
                    )
                    node_attempts = {str(r[0]): int(r[1]) for r in cur.fetchall()}
                    cur.execute(
                        f"SELECT {_FETCH_COLUMNS} FROM {EVENTS_TABLE} "
                        "WHERE thread_id = %s ORDER BY id DESC LIMIT 1",
                        (thread_id,),
                    )
                    cols = [d.name for d in cur.description]
                    row = cur.fetchone()
                    last_event = _row_to_event(row, cols) if row else None
                else:
                    last_event = None
        return {
            "thread_id": thread_id,
            "available": True,
            "event_count": int(total or 0),
            "first_id": int(first_id) if first_id is not None else None,
            "last_id": int(last_id) if last_id is not None else None,
            "last_event": last_event,
            "node_attempts": node_attempts,
        }
    except Exception as e:  # noqa: BLE001
        _record_degraded(f"latest_snapshot({thread_id!r})", e)
        return empty
