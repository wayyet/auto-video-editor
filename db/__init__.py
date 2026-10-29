"""应用层数据库访问层(PostgreSQL 统一改造方案 阶段 B)。

职责划分(方案 §2.1「分工边界」)
--------------------------------
- ``db.migrations`` — **应用层** SQL 迁移器,只管
  ``app_schema_migrations`` / ``chat_sessions`` / ``workflow_runs``。
- ``db.session_repository`` — ``chat_sessions`` 的仓储(CRUD / 乐观锁 / 游标分页)。
- LangGraph 自己的 4 张 checkpoint 表由 ``AsyncPostgresSaver.setup()`` 管,
  两边互不干涉(方案 §2.1)。

编码注意(本机 Postgres 库编码 = ``SQL_ASCII``)
----------------------------------------------
- 读 TEXT 列拿到的是 ``bytes`` ⇒ 调用方必须自己 ``decode("utf-8")``(方案 §3.2 A5)。
- 写 JSONB **必须** ``ensure_ascii=False`` ⇒ 否则 PG 抛 ``UntranslatableCharacter``。
  见 ``db.session_repository.jsonb``。
"""

from __future__ import annotations

from db.connection import connect, connection
from db.migrations import (
    MIGRATIONS,
    Migration,
    apply_migrations,
    apply_migrations_to_db,
    applied_versions,
)
from db.session_repository import (
    SessionAlreadyExists,
    SessionNotFound,
    SessionRecord,
    SessionRepository,
    SessionSummary,
    SessionVersionConflict,
    jsonb,
    summarize_state,
    text_value,
)

__all__ = [
    "MIGRATIONS",
    "Migration",
    "SessionAlreadyExists",
    "SessionNotFound",
    "SessionRecord",
    "SessionRepository",
    "SessionSummary",
    "SessionVersionConflict",
    "applied_versions",
    "apply_migrations",
    "apply_migrations_to_db",
    "connect",
    "connection",
    "jsonb",
    "summarize_state",
    "text_value",
]
