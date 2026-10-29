"""workflow_runs 轻量台账表(PostgreSQL 统一改造方案 §4.4)。

目的:在 Postgres 同一库里建独立表,跟踪每个挂起的 thread_id 与当前关卡,
便于:
- 运维查"哪些线程在挂起 / 等多久了 / 哪个关卡"
- 破坏性变更应急:State schema 升级时按 ``workflow_runs.status`` 过滤
  只对 running / suspended 做迁移,跳过 completed / failed
- 多日挂起的可视化

表结构(建表 DDL 已迁到 ``db/migrations.py``,这里是唯一事实来源):
    thread_id          TEXT PRIMARY KEY
    video_name         TEXT NOT NULL       -- 从 state.session_id / draft_path 解析
    current_checkpoint TEXT                -- '①' / '②' / '③' / NULL
    status             TEXT NOT NULL       -- 'running' / 'suspended' / 'completed' / 'failed'
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()

调用约定:由各关卡节点(node_06 / node_12 / node_checkpoint3_layout_review)
+ graph 边界节点调用 ``upsert_run`` / ``mark_resumed`` 维护。
``build_graph()`` 不主动写,避免与 interrupt 重放语义冲突。

``psycopg`` 是同步连接,与 ``AsyncPostgresSaver`` 的异步池不共享连接 —
``runs_table`` 用独立 ``psycopg.connect`` 直连,简单可靠。

降级语义(方案 D5 / §4.4)
------------------------
运行台账**允许降级**:数据库不可达时调用返回降级值(``False`` / ``[]``),
但**必须**同时打 ERROR 级日志并累加告警计数 —— 静默降级会让人误以为"已保存"。
(检查点与会话状态不走这套降级,那是 fail-closed。)
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg

from config import POSTGRES_URI

logger = logging.getLogger(__name__)


# status 合法取值(便于上层校验)
STATUS_RUNNING = "running"
STATUS_SUSPENDED = "suspended"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

VALID_STATUSES = frozenset({STATUS_RUNNING, STATUS_SUSPENDED, STATUS_COMPLETED, STATUS_FAILED})

# 降级操作计数(方案 §4.4:台账降级必须可观测)。
# 进程内计数 —— 供监控脚本 / 告警采集读取;每次降级 +1。
_degraded_count = 0
_degraded_lock = threading.Lock()


def degraded_count() -> int:
    """返回本进程累计的台账降级次数。

    大于 0 说明 Postgres 不可达,台账写入没有真正落库 —— 应当告警。
    """
    with _degraded_lock:
        return _degraded_count


def _record_degraded(operation: str, exc: BaseException) -> None:
    """记录一次降级:ERROR 级日志 + 计数 +1。"""
    global _degraded_count
    with _degraded_lock:
        _degraded_count += 1
        total = _degraded_count
    logger.error(
        "[runs_table] %s failed (%s: %s) — 已降级(累计降级 %s 次);台账未落库,需排查 Postgres",
        operation,
        type(exc).__name__,
        exc,
        total,
    )


@contextmanager
def _conn() -> Iterator[psycopg.Connection]:
    """从 ``POSTGRES_URI`` 拿一次性同步连接。

    失败时让 ``psycopg.OperationalError`` 透传给上层,便于调用方决定
    是否记录 warning / 跳过。

    走 ``db.connection`` —— 把 ``client_encoding`` 设成 UTF-8:本机库是
    ``SQL_ASCII``,不设的话 psycopg 会按 ASCII 编码查询串,中文直接报错。
    """
    from db.connection import connection as _db_connection

    uri = os.environ.get("POSTGRES_URI", POSTGRES_URI)
    with _db_connection(uri) as conn:
        yield conn


def init_schema() -> None:
    """应用层迁移(幂等)。

    建表 DDL 已从本模块移走 —— 统一由 ``db/migrations.py`` 管理(方案 §4.4),
    这里只做一次调用,避免"两处建表"漂移。

    失败不抛,降级为 ERROR 日志 —— Postgres 不可达时 runs_table 调用全部降级为
    no-op,不影响图运行;但会累加 ``degraded_count()`` 供告警采集。
    """
    try:
        from db.migrations import apply_migrations

        with _conn() as c:
            apply_migrations(c)
    except Exception as e:  # noqa: BLE001
        _record_degraded("init_schema", e)


def upsert_run(
    thread_id: str,
    video_name: str,
    current_checkpoint: str | None,
    status: str,
) -> bool:
    """插入或更新一行 ``workflow_runs``。

    Args:
        thread_id: LangGraph thread_id。
        video_name: 视频名(从 state.session_id 或 draft_path 解析)。
        current_checkpoint: '①' / '②' / '③' / None(运行中)。
        status: 'running' / 'suspended' / 'completed' / 'failed'。

    Returns:
        True 写成功,False 失败(降级 + ERROR 日志 + 计数,不影响主流程)。
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status {status!r}; must be one of {sorted(VALID_STATUSES)}"
        )
    try:
        with _conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO workflow_runs(thread_id, video_name, current_checkpoint, status)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (thread_id) DO UPDATE
                    SET current_checkpoint = EXCLUDED.current_checkpoint,
                        status = EXCLUDED.status,
                        updated_at = now()
                    """,
                    (thread_id, video_name, current_checkpoint, status),
                )
            c.commit()
        return True
    except Exception as e:  # noqa: BLE001
        _record_degraded(f"upsert_run({thread_id}, {status})", e)
        return False


def mark_resumed(thread_id: str) -> bool:
    """resume 后:把 status 置回 running(关卡由调用方决定是否再 upsert_run)。"""
    return upsert_run(thread_id, video_name="", current_checkpoint=None, status=STATUS_RUNNING)


def mark_completed(thread_id: str) -> bool:
    """流程结束后置 completed。"""
    return upsert_run(thread_id, video_name="", current_checkpoint=None, status=STATUS_COMPLETED)


def mark_failed(thread_id: str, *, current_checkpoint: str | None = None) -> bool:
    """流程异常时置 failed。"""
    return upsert_run(
        thread_id, video_name="", current_checkpoint=current_checkpoint, status=STATUS_FAILED
    )


def list_suspended() -> list[dict]:
    """查所有 suspended 线程 — 运维 SQL 查询入口。

    Returns:
        list of dicts: ``[{thread_id, video_name, current_checkpoint, updated_at}, ...]``。
        数据库不可达时返回 ``[]``(并记一次降级)。
    """
    try:
        with _conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    """
                    SELECT thread_id, video_name, current_checkpoint, updated_at
                    FROM workflow_runs
                    WHERE status = %s
                    ORDER BY updated_at DESC
                    """,
                    (STATUS_SUSPENDED,),
                )
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        _record_degraded("list_suspended", e)
        return []
