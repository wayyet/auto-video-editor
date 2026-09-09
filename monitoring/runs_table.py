"""workflow_runs 轻量台账表(Week 5 新增,对照第 5 周计划 §4.7)。

目的:在 Postgres 同一库里建独立表,跟踪每个挂起的 thread_id 与当前关卡,
便于:
- 运维查"哪些线程在挂起 / 等多久了 / 哪个关卡"
- 破坏性变更应急:State schema 升级时按 ``workflow_runs.status`` 过滤
  只对 running / suspended 做迁移,跳过 completed / failed
- 多日挂起的可视化

表结构:
    thread_id          TEXT PRIMARY KEY
    video_name         TEXT NOT NULL       -- 从 state.session_id / draft_path 解析
    current_checkpoint TEXT                -- '①' / '②' / '③' / NULL
    status             TEXT NOT NULL       -- 'running' / 'suspended' / 'completed' / 'failed'
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP

调用约定:由各关卡节点(node_06 / node_12 / node_checkpoint3_layout_review)
+ graph 边界节点调用 ``upsert_run`` / ``mark_resumed`` 维护。
``build_graph()`` 不主动写,避免与 interrupt 重放语义冲突。

``psycopg`` 是同步连接,与 ``AsyncPostgresSaver`` 的异步池不共享连接 —
``runs_table`` 用独立 ``psycopg.connect`` 直连,简单可靠。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg

from config import POSTGRES_URI


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    thread_id TEXT PRIMARY KEY,
    video_name TEXT NOT NULL,
    current_checkpoint TEXT,
    status TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# status 合法取值(便于上层校验)
STATUS_RUNNING = "running"
STATUS_SUSPENDED = "suspended"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

VALID_STATUSES = frozenset({STATUS_RUNNING, STATUS_SUSPENDED, STATUS_COMPLETED, STATUS_FAILED})


@contextmanager
def _conn() -> Iterator[psycopg.Connection]:
    """从 ``POSTGRES_URI`` 拿一次性同步连接。

    失败时让 ``psycopg.OperationalError`` 透传给上层,便于调用方决定
    是否记录 warning / 跳过。
    """
    uri = os.environ.get("POSTGRES_URI", POSTGRES_URI)
    c = psycopg.connect(uri)
    try:
        yield c
    finally:
        c.close()


def init_schema() -> None:
    """建表(幂等)。Week 5 计划 §4.7 要求主入口在启动时调用一次。

    失败不抛,改写 warning — Postgres 不可达时 runs_table 调用全部降级为
    no-op,不影响图运行。
    """
    try:
        with _conn() as c:
            with c.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
            c.commit()
    except Exception as e:  # noqa: BLE001
        import logging
        logging.warning(
            "[runs_table] init_schema failed (%s: %s) — workflow_runs 台账暂不可用",
            type(e).__name__,
            e,
        )


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
        True 写成功,False 失败(降级为 warning,不影响主流程)。
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
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (thread_id, video_name, current_checkpoint, status),
                )
            c.commit()
        return True
    except Exception as e:  # noqa: BLE001
        import logging
        logging.warning(
            "[runs_table] upsert_run(%s, %s) failed (%s: %s)",
            thread_id,
            status,
            type(e).__name__,
            e,
        )
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
        数据库不可达时返回 ``[]``。
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
        import logging
        logging.warning(
            "[runs_table] list_suspended failed (%s: %s) — returning []",
            type(e).__name__,
            e,
        )
        return []
