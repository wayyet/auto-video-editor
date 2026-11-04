"""应用层数据库访问层(PostgreSQL 统一改造方案 阶段 B;2026-11-04 单流水线收敛)。

职责划分(方案 §2.1「分工边界」)
--------------------------------
- ``db.migrations`` — **应用层** SQL 迁移器,只管
  ``app_schema_migrations`` / ``workflow_runs`` / ``workflow_run_events``
  (``chat_sessions`` / ``workflow_chat_links`` 为 legacy schema:
  已发布迁移定义保留、存量表与数据不动,应用层已停止读写 ——
  单流水线架构收敛 阶段4)。
- LangGraph 自己的 4 张 checkpoint 表由 ``AsyncPostgresSaver.setup()`` 管,
  两边互不干涉(方案 §2.1)。

编码注意(本机 Postgres 库编码 = ``SQL_ASCII``)
----------------------------------------------
- 读 TEXT 列拿到的是 ``bytes`` ⇒ 调用方必须自己 ``decode("utf-8")``(方案 §3.2 A5)。
- 写 JSONB **必须** ``ensure_ascii=False`` ⇒ 否则 PG 抛 ``UntranslatableCharacter``。
  见 ``db.connection.configure_json_dumps``。
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

__all__ = [
    "MIGRATIONS",
    "Migration",
    "applied_versions",
    "apply_migrations",
    "apply_migrations_to_db",
    "connect",
    "connection",
]
