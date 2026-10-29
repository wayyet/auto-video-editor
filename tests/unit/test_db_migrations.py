"""``db.migrations`` 的单元测试(方案 §7.1 / 阶段 B 门禁)。

不连库,只测纯逻辑:
- ``Migration`` 的自校验;
- 迁移清单本身的内容(版本连续、命名、语句非空);
- 关键 DDL 片段在场(``chat_sessions`` 的 CHECK / 索引,``workflow_runs`` 的
  CHECK / TIMESTAMPTZ 转换 / 索引);
- 迁移语句**不含** saver 的 4 张表(方案 §2.1「分工边界」)。

真实库的幂等验证在 ``tests/integration/test_session_repository_postgres.py``。
"""

from __future__ import annotations

import pytest

from db.migrations import (
    MIGRATIONS,
    MIGRATIONS_TABLE_NAME,
    Migration,
    applied_versions,
)


def test_migration_rejects_bad_version() -> None:
    with pytest.raises(ValueError):
        Migration(version=0, name="x", statements=("SELECT 1;",))


def test_migration_rejects_blank_name() -> None:
    with pytest.raises(ValueError):
        Migration(version=1, name="  ", statements=("SELECT 1;",))


def test_migration_rejects_empty_statements() -> None:
    with pytest.raises(ValueError):
        Migration(version=1, name="x", statements=())


def test_migration_list_versions_are_unique_and_contiguous() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions == list(range(1, len(versions) + 1))


def test_migration_list_all_statements_non_empty() -> None:
    for mig in MIGRATIONS:
        assert mig.statements, mig
        for stmt in mig.statements:
            assert stmt.strip(), mig


def _all_sql() -> str:
    """所有迁移语句拼起来(空白归一化,便于断言)。"""
    joined = "\n".join(stmt for mig in MIGRATIONS for stmt in mig.statements)
    return " ".join(joined.split())


def test_chat_sessions_ddl_present() -> None:
    sql = _all_sql()
    assert "CREATE TABLE IF NOT EXISTS chat_sessions" in sql
    # 方案 §2.1 的字段与约束
    assert "session_id" in sql
    assert "^[0-9a-f]{32}$" in sql  # session_id CHECK
    assert "title_source IN ('auto','manual')" in sql
    assert "state_json" in sql and "JSONB" in sql
    assert "message_count INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0)" in sql
    assert "version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1)" in sql
    assert "idx_chat_sessions_active_updated" in sql
    assert "WHERE deleted_at IS NULL" in sql


def test_workflow_runs_ddl_present() -> None:
    sql = _all_sql()
    assert "CREATE TABLE IF NOT EXISTS workflow_runs" in sql
    # 方案 §2.1:thread_id CHECK 挡掉 \x<hex>,status CHECK 限定取值
    assert "^[A-Za-z0-9_.:-]+$" in sql
    assert "'running','suspended','completed','failed'" in sql
    # TIMESTAMPTZ 走"加列 → 回填 → 换列"
    assert "updated_at_tz" in sql
    assert "AT TIME ZONE 'UTC'" in sql
    assert "idx_workflow_runs_status_updated" in sql


def test_migrations_do_not_touch_langgraph_saver_tables() -> None:
    """分工边界:应用迁移**不碰** saver 的 4 张表(方案 §2.1)。"""
    sql = _all_sql()
    for table in (
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" not in sql
        assert f"ALTER TABLE {table}" not in sql
        assert f"DROP TABLE {table}" not in sql


def test_migrations_table_sql_is_idempotent() -> None:
    from db.migrations import MIGRATIONS_TABLE_SQL

    assert "CREATE TABLE IF NOT EXISTS app_schema_migrations" in MIGRATIONS_TABLE_SQL
    assert MIGRATIONS_TABLE_NAME == "app_schema_migrations"


def test_postgres_uri_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from db.migrations import postgres_uri

    monkeypatch.setenv("POSTGRES_URI", "postgresql://x:y@localhost:5432/zzz")
    assert postgres_uri() == "postgresql://x:y@localhost:5432/zzz"


def test_postgres_uri_falls_back_to_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from db.migrations import postgres_uri

    monkeypatch.delenv("POSTGRES_URI", raising=False)
    uri = postgres_uri()
    assert uri.startswith("postgresql://")
    assert "video_workflow" in uri


def test_applied_versions_on_empty_db() -> None:
    """台账表不存在时返回空集(而不是抛异常)。"""

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, _sql, _params=None):
            self._empty = True

        def fetchone(self):
            return None

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    assert applied_versions(_FakeConn()) == set()  # type: ignore[arg-type]
