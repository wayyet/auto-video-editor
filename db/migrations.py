"""应用层 SQL 迁移器(PostgreSQL 统一改造方案 §2.1 / §9 阶段 B)。

范围
----
本模块**只**管应用层的表:

- ``app_schema_migrations`` — 迁移版本台账(本模块自建,不算一条迁移)。
- ``chat_sessions``         — 聊天会话(阶段 B 新建)。
- ``workflow_runs``         — 运行台账(阶段 B 升级:CHECK / 索引 / TIMESTAMPTZ)。

LangGraph 自己的 4 张 checkpoint 表(``checkpoints`` / ``checkpoint_blobs`` /
``checkpoint_writes`` / ``checkpoint_migrations``)由
``AsyncPostgresSaver.setup()`` 建,本模块**不碰**(方案 §2.1「分工边界」)。

设计要点
--------
- **幂等**:每条语句自身幂等(``IF NOT EXISTS`` / ``DO $$ ... $$`` 守卫),
  且 ``app_schema_migrations`` 记录已应用版本 ⇒ 重复执行安全(阶段 B 门禁)。
- **原子**:所有迁移在**同一个事务**里跑;任一条失败 ⇒ 整体回滚,不留半截状态。
- **串行**:用 ``pg_advisory_xact_lock`` 抢占,多实例同时启动时只有一个跑迁移。
- **fail-closed**:连接失败 / 迁移失败一律抛异常,绝不静默降级。

编码注意(本机 Postgres 库编码 = ``SQL_ASCII``)
----------------------------------------------
``client_encoding`` 跟随库编码 = ``SQL_ASCII``,因此:
- 读 TEXT 列拿到的是 ``bytes``,调用方必须自己 ``decode("utf-8")``(方案 §3.2 A5)。
  ⇒ 迁移 003 把**库级默认**改成 UTF8,新连接直接给 ``str``;但代码里仍要能处理
  两种形态(旧连接的 bytes / 新连接的 str)。
- 写 JSONB **必须**用 ``ensure_ascii=False`` 序列化:默认的 ``ensure_ascii=True``
  会把中文变成 ``\\uXXXX``,PG 无法把 unicode 转义翻译成 SQL_ASCII ⇒
  ``UntranslatableCharacter``。⇒ 写 JSONB 的进程必须先调
  ``db.connection.configure_json_dumps()``(全局设置,LangGraph saver 也吃这一套)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence

import psycopg

from db.connection import connection as _db_connection

__all__ = [
    "MIGRATIONS",
    "MIGRATIONS_TABLE_NAME",
    "Migration",
    "apply_migrations",
    "apply_migrations_to_db",
    "applied_versions",
    "postgres_uri",
]


# ---------------------------------------------------------------------------
# 迁移版本台账
# ---------------------------------------------------------------------------
MIGRATIONS_TABLE_NAME = "app_schema_migrations"

MIGRATIONS_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE_NAME} (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# 应用私有 advisory lock key(避免多实例同时跑迁移)。
# 约定:换 key 之前先确认没有别的组件占用。
_ADVISORY_LOCK_KEY = 8_210_445_177_001


@dataclass(frozen=True)
class Migration:
    """一条版本化迁移。

    Attributes:
        version: 单调递增的整数版本号,从 1 开始。
        name: 人类可读的名字(写进 ``app_schema_migrations.name``)。
        statements: 该迁移要执行的 SQL 语句,按顺序执行。
    """

    version: int
    name: str
    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError(f"migration version 必须 >= 1,got {self.version!r}")
        if not self.name.strip():
            raise ValueError(f"migration {self.version} 缺少 name")
        if not self.statements:
            raise ValueError(f"migration {self.version} ({self.name}) 没有任何语句")


# ---------------------------------------------------------------------------
# 001 — chat_sessions
# ---------------------------------------------------------------------------
_CHAT_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id     TEXT PRIMARY KEY
                   CHECK (session_id ~ '^[0-9a-f]{32}$'),
    title          TEXT NOT NULL DEFAULT '新对话',
    title_source   TEXT NOT NULL DEFAULT 'auto'
                   CHECK (title_source IN ('auto','manual')),
    state_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    message_count  INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    last_message_preview TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ NULL,
    version        BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1)
);
"""

_CHAT_SESSIONS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_chat_sessions_active_updated
    ON chat_sessions (updated_at DESC)
    WHERE deleted_at IS NULL;
"""

# ---------------------------------------------------------------------------
# 002 — workflow_runs 升级
# ---------------------------------------------------------------------------
# 新建时直接建成目标结构(thread_id / status CHECK + TIMESTAMPTZ)。
# 已有旧表时 CREATE TABLE IF NOT EXISTS 是空操作,后面的 DO 块负责补齐。
_WORKFLOW_RUNS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    thread_id         TEXT PRIMARY KEY
                      CONSTRAINT workflow_runs_thread_id_check
                      CHECK (thread_id ~ '^[A-Za-z0-9_.:-]+$'),
    video_name        TEXT NOT NULL,
    current_checkpoint TEXT,
    status            TEXT NOT NULL
                      CONSTRAINT workflow_runs_status_check
                      CHECK (status IN ('running','suspended','completed','failed')),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# thread_id CHECK:禁止 bytes 造成的 ``\x<hex>`` 字面量(方案 §3.2 A2)。
# 先 NOT VALID 加约束(不扫全表),再 VALIDATE —— 存量违规 ⇒ 迁移失败(fail-closed)。
_WORKFLOW_RUNS_THREAD_ID_CONSTRAINT_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'workflow_runs_thread_id_check'
          AND conrelid = 'public.workflow_runs'::regclass
    ) THEN
        ALTER TABLE workflow_runs
            ADD CONSTRAINT workflow_runs_thread_id_check
            CHECK (thread_id ~ '^[A-Za-z0-9_.:-]+$') NOT VALID;
    END IF;
END
$$;
"""

_WORKFLOW_RUNS_VALIDATE_THREAD_ID_SQL = (
    "ALTER TABLE workflow_runs VALIDATE CONSTRAINT workflow_runs_thread_id_check;"
)

_WORKFLOW_RUNS_STATUS_CONSTRAINT_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'workflow_runs_status_check'
          AND conrelid = 'public.workflow_runs'::regclass
    ) THEN
        ALTER TABLE workflow_runs
            ADD CONSTRAINT workflow_runs_status_check
            CHECK (status IN ('running','suspended','completed','failed')) NOT VALID;
    END IF;
END
$$;
"""

_WORKFLOW_RUNS_VALIDATE_STATUS_SQL = (
    "ALTER TABLE workflow_runs VALIDATE CONSTRAINT workflow_runs_status_check;"
)

# updated_at: TIMESTAMP → TIMESTAMPTZ,走"加列 → 回填 → 换列"(方案 §2.1)。
# 存量行的时区语义按 **UTC** 解释(方案 §11 U1 的建议默认)。
# 只在列确实是 ``timestamp without time zone`` 时才动 —— 已经是 timestamptz 就跳过,
# 否则 ``AT TIME ZONE 'UTC'`` 会把已带时区的值再偏一次。
_WORKFLOW_RUNS_TZ_CONVERSION_SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'workflow_runs'
          AND column_name = 'updated_at'
          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE workflow_runs ADD COLUMN updated_at_tz TIMESTAMPTZ;
        UPDATE workflow_runs SET updated_at_tz = updated_at AT TIME ZONE 'UTC';
        ALTER TABLE workflow_runs DROP COLUMN updated_at;
        ALTER TABLE workflow_runs RENAME COLUMN updated_at_tz TO updated_at;
    END IF;
END
$$;
"""

_WORKFLOW_RUNS_TZ_FINALIZE_SQL = """
ALTER TABLE workflow_runs ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE workflow_runs ALTER COLUMN updated_at SET NOT NULL;
"""

_WORKFLOW_RUNS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status_updated
    ON workflow_runs (status, updated_at DESC);
"""

# ---------------------------------------------------------------------------
# 003 — 库级 client_encoding
# ---------------------------------------------------------------------------
# 本机库编码是 SQL_ASCII(Windows 容器没设 LANG)。库级 ``client_encoding`` 默认
# 跟着库编码走 = SQL_ASCII ⇒ psycopg3 把 TEXT 列当 **bytes** 返回,调用方得自己
# decode(方案 §3.2 A5)。
#
# 这里把**库级默认**改成 UTF8:
# - 库本身的存储编码**不变**(仍是 SQL_ASCII,服务端不做任何字符集转换);
# - 只是让新连接默认用 UTF-8 ⇒ TEXT / JSONB 直接给 ``str``,少一个解码坑。
#
# ⚠️ 这条迁移**改不了已建立的连接**:已开的会话仍是旧值,重连才生效。
# ⚠️ 只影响"当前连接的库",所以用 ``current_database()`` 拼,不会误改别的库。
_CLIENT_ENCODING_UTF8_SQL = """
DO $$
BEGIN
    EXECUTE format(
        'ALTER DATABASE %I SET client_encoding TO %L',
        current_database(),
        'UTF8'
    );
END
$$;
"""


# ---------------------------------------------------------------------------
# 迁移清单
# ---------------------------------------------------------------------------
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="create_chat_sessions",
        statements=(
            _CHAT_SESSIONS_SQL,
            _CHAT_SESSIONS_INDEX_SQL,
        ),
    ),
    Migration(
        version=2,
        name="upgrade_workflow_runs",
        statements=(
            _WORKFLOW_RUNS_CREATE_SQL,
            _WORKFLOW_RUNS_THREAD_ID_CONSTRAINT_SQL,
            _WORKFLOW_RUNS_VALIDATE_THREAD_ID_SQL,
            _WORKFLOW_RUNS_STATUS_CONSTRAINT_SQL,
            _WORKFLOW_RUNS_VALIDATE_STATUS_SQL,
            _WORKFLOW_RUNS_TZ_CONVERSION_SQL,
            _WORKFLOW_RUNS_TZ_FINALIZE_SQL,
            _WORKFLOW_RUNS_INDEX_SQL,
        ),
    ),
    Migration(
        version=3,
        name="set_client_encoding_utf8",
        statements=(_CLIENT_ENCODING_UTF8_SQL,),
    ),
)


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------
def applied_versions(conn: psycopg.Connection) -> set[int]:
    """返回已应用的迁移版本号集合(台账表不存在时返回空集)。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s",
            (MIGRATIONS_TABLE_NAME,),
        )
        if cur.fetchone() is None:
            return set()
        cur.execute(f"SELECT version FROM {MIGRATIONS_TABLE_NAME}")
        return {int(row[0]) for row in cur.fetchall()}


def apply_migrations(
    conn: psycopg.Connection,
    *,
    migrations: Sequence[Migration] | None = None,
) -> list[int]:
    """应用尚未执行的迁移,返回本次实际应用的版本号列表。

    幂等:已记录在案的版本会被跳过;语句本身也可重复执行。
    原子:全部迁移在同一个事务里执行,失败整体回滚并抛异常。

    Args:
        conn: 已打开的 psycopg 连接(本函数负责 commit / rollback)。
        migrations: 迁移清单,默认用模块级 ``MIGRATIONS``。

    Raises:
        Exception: 任何语句失败都会回滚并向上抛出(fail-closed)。
    """
    todo = tuple(migrations if migrations is not None else MIGRATIONS)
    applied: list[int] = []
    try:
        with conn.cursor() as cur:
            # 事务级 advisory lock:多实例同时启动时只有一个真正跑迁移。
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
            cur.execute(MIGRATIONS_TABLE_SQL)
            cur.execute(f"SELECT version FROM {MIGRATIONS_TABLE_NAME}")
            done = {int(row[0]) for row in cur.fetchall()}

            for mig in sorted(todo, key=lambda m: m.version):
                if mig.version in done:
                    continue
                for stmt in mig.statements:
                    cur.execute(stmt)
                cur.execute(
                    f"INSERT INTO {MIGRATIONS_TABLE_NAME}(version, name) "
                    "VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                    (mig.version, mig.name),
                )
                applied.append(mig.version)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return applied


def postgres_uri() -> str:
    """当前生效的 Postgres 连接串。

    优先级:环境变量 ``POSTGRES_URI`` → ``config.POSTGRES_URI``。
    与 ``monitoring/runs_table.py`` 保持同一套约定。
    """
    env = os.environ.get("POSTGRES_URI", "").strip()
    if env:
        return env
    try:
        from config import POSTGRES_URI

        return POSTGRES_URI
    except Exception:  # pragma: no cover - config 不可导入时的兜底
        return "postgresql://postgres:devpass@localhost:5432/video_workflow"


def apply_migrations_to_db(
    *,
    uri: str | None = None,
    migrations: Sequence[Migration] | None = None,
) -> list[int]:
    """连库并应用迁移(便捷入口,给脚本 / 启动流程用)。"""
    target = uri or postgres_uri()
    with _db_connection(target) as conn:
        return apply_migrations(conn, migrations=migrations)
