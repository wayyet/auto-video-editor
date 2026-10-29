"""``chat_sessions`` 仓储(PostgreSQL 统一改造方案 §2.1 / §4.5 / §6)。

职责
----
``chat_sessions`` 是聊天会话的**唯一事实来源**。本仓储提供:

- 创建 / 读取 / 更新 / 软删除 / 恢复;
- **乐观锁**更新(``version`` 列):影响行数为 0 ⇒ 已被他人改过 ⇒ 抛冲突,
  **绝不直接覆盖**(方案 §6);
- **游标分页**列表(``limit + cursor``,按 ``(updated_at, session_id)``),
  不用 ``offset`` —— 会话频繁更新会让 offset 分页出现重复 / 遗漏(方案 §4.5);
- 标题 ``title_source`` 语义:手动标题不被自动标题覆盖。

本模块只做数据访问,**不含** HTTP 路由与前端逻辑(那是阶段 D 的事)。

编码注意(本机 Postgres 库编码 = ``SQL_ASCII``)
----------------------------------------------
- 读 TEXT 列拿到的是 ``bytes`` ⇒ 本模块统一用 ``_text()`` 解码(方案 §3.2 A5)。
- 写 JSONB **必须** ``ensure_ascii=False`` ⇒ 见 ``jsonb()``。
  默认的 ``ensure_ascii=True`` 会把中文写成 ``\\uXXXX``,PG 在 SQL_ASCII 库上
  无法翻译该转义 ⇒ ``UntranslatableCharacter``。
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, ContextManager, Iterator, Mapping, Sequence

import psycopg
from psycopg.types.json import Jsonb

from db.connection import connection as _db_connection

__all__ = [
    "SESSION_ID_RE",
    "SessionAlreadyExists",
    "SessionNotFound",
    "SessionRecord",
    "SessionRepository",
    "SessionSummary",
    "SessionVersionConflict",
    "jsonb",
    "summarize_state",
    "text_value",
]


# 会话 ID 形如 ``uuid.uuid4().hex`` —— 32 位小写十六进制。
# 与 ``chat_sessions.session_id`` 的 CHECK 约束保持一致(方案 §1.2)。
SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")

DEFAULT_TITLE = "新对话"

TITLE_SOURCE_AUTO = "auto"
TITLE_SOURCE_MANUAL = "manual"
VALID_TITLE_SOURCES = frozenset({TITLE_SOURCE_AUTO, TITLE_SOURCE_MANUAL})

_PREVIEW_MAX_CHARS = 200


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class SessionNotFound(RuntimeError):
    """会话不存在(或已软删除)。"""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session not found: {session_id}")
        self.session_id = session_id


class SessionAlreadyExists(RuntimeError):
    """会话 ID 已存在。"""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session already exists: {session_id}")
        self.session_id = session_id


class SessionVersionConflict(RuntimeError):
    """乐观锁冲突:``version`` 对不上,说明别人已经改过这条会话。

    方案 §6:影响行数为 0 时**不能直接覆盖**,必须重读后合并或返回 409。
    """

    def __init__(self, session_id: str, expected_version: int) -> None:
        super().__init__(
            f"session {session_id} version conflict (expected={expected_version})"
        )
        self.session_id = session_id
        self.expected_version = expected_version


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SessionRecord:
    """一条会话的完整记录(含 ``state_json``)。"""

    session_id: str
    title: str
    title_source: str
    state_json: dict
    message_count: int
    last_message_preview: str
    created_at: datetime | None
    updated_at: datetime | None
    deleted_at: datetime | None
    version: int


@dataclass(frozen=True)
class SessionSummary:
    """会话列表项(**不含** ``state_json``)。"""

    session_id: str
    title: str
    title_source: str
    message_count: int
    last_message_preview: str
    created_at: datetime | None
    updated_at: datetime | None


# ---------------------------------------------------------------------------
# 编解码辅助
# ---------------------------------------------------------------------------
def text_value(value: Any) -> str:
    """把 psycopg 返回的 TEXT 值转成 ``str``。

    本机 Postgres 库编码是 ``SQL_ASCII`` ⇒ psycopg3 把 TEXT 列当 **bytes**
    返回(方案 §3.2 A5)。这里统一解码,UTF-8 兜底。
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _int_value(value: Any, default: int = 0) -> int:
    """把 psycopg 返回的整数列转成 ``int``(SQL_ASCII 下 BIGINT 也可能是 bytes)。"""
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8", errors="replace")
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_value(value: Any) -> dict:
    """把 JSONB 列转成 ``dict``(兼容 bytes / str / 已是 dict 的情况)。"""
    if value is None:
        return {}
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value).decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw else {}
    elif isinstance(value, str):
        parsed = json.loads(value) if value else {}
    else:
        parsed = value
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _dumps_utf8(obj: Any) -> str:
    """JSONB 序列化:必须 ``ensure_ascii=False``。

    默认的 ``ensure_ascii=True`` 会把中文写成 ``\\uXXXX``,PG 在 SQL_ASCII 库上
    无法把 unicode 转义翻译成 SQL_ASCII ⇒ ``UntranslatableCharacter``。
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def jsonb(value: Any) -> Jsonb:
    """把 Python 对象包成能安全写进 JSONB 的参数。"""
    return Jsonb(value, dumps=_dumps_utf8)


def summarize_state(state: Mapping[str, Any] | None) -> tuple[int, str]:
    """从会话状态推导 ``(message_count, last_message_preview)``。

    约定:状态里的 ``history`` 是"给前端回放"的消息列表(见
    ``ChatSession.dump_state()``),末条即为最新消息预览。

    Args:
        state: 会话状态字典(``dump_state()`` 的产物)。

    Returns:
        ``(消息条数, 末条消息预览)``。状态缺失 / 形状不对时返回 ``(0, "")``。
    """
    if not isinstance(state, Mapping):
        return 0, ""
    history = state.get("history")
    if not isinstance(history, (list, tuple)):
        return 0, ""
    count = len(history)
    if count == 0:
        return 0, ""
    last = history[-1]
    preview = ""
    if isinstance(last, Mapping):
        for key in ("message", "content", "summary", "text"):
            raw = last.get(key)
            if isinstance(raw, str) and raw.strip():
                preview = raw.strip()
                break
            if raw is not None and not isinstance(raw, str):
                preview = json.dumps(raw, ensure_ascii=False)
                break
    preview = preview.replace("\n", " ").strip()
    if len(preview) > _PREVIEW_MAX_CHARS:
        preview = preview[:_PREVIEW_MAX_CHARS]
    return count, preview


def _escape_like(prefix: str) -> str:
    """转义 LIKE 模式里的 ``%`` / ``_`` / ``\\``(配合 ``ESCAPE '\\'``)。"""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------
ConnFactory = Callable[[], ContextManager[psycopg.Connection]]

_COLUMNS = (
    "session_id, title, title_source, state_json, message_count, "
    "last_message_preview, created_at, updated_at, deleted_at, version"
)

_SUMMARY_COLUMNS = (
    "session_id, title, title_source, message_count, last_message_preview, "
    "created_at, updated_at"
)


class SessionRepository:
    """``chat_sessions`` 的数据访问对象。

    Args:
        uri: Postgres 连接串;不给则用 ``db.migrations.postgres_uri()``。
        conn_factory: 连接工厂(返回 ``psycopg.Connection`` 的上下文管理器)。
            给定时 ``uri`` 被忽略 —— 单测用它注入假连接。
    """

    def __init__(
        self,
        *,
        uri: str | None = None,
        conn_factory: ConnFactory | None = None,
    ) -> None:
        self._uri = uri
        self._conn_factory = conn_factory

    # -- 连接 ---------------------------------------------------------------
    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        """取一条连接。

        默认每次操作开一条短连接(与 ``monitoring/runs_table.py`` 同一套约定:
        同步 psycopg 与 saver 的异步池不共享连接)。
        阶段 D 引入 lifespan 连接池后,这里换成池化连接即可,调用方无感。

        连接统一走 ``db.connection`` —— 把 ``client_encoding`` 设成 UTF-8,
        否则本机 SQL_ASCII 库会让带中文的 SQL 直接报 UnicodeEncodeError。
        """
        if self._conn_factory is not None:
            with self._conn_factory() as conn:
                yield conn
            return
        target = self._uri
        if not target:
            from db.migrations import postgres_uri

            target = postgres_uri()
        with _db_connection(target) as conn:
            yield conn

    # -- 读 -----------------------------------------------------------------
    @staticmethod
    def _to_record(row: Sequence[Any]) -> SessionRecord:
        return SessionRecord(
            session_id=text_value(row[0]),
            title=text_value(row[1]),
            title_source=text_value(row[2]),
            state_json=_json_value(row[3]),
            message_count=_int_value(row[4]),
            last_message_preview=text_value(row[5]),
            created_at=row[6],
            updated_at=row[7],
            deleted_at=row[8],
            version=_int_value(row[9], default=1),
        )

    @staticmethod
    def _to_summary(row: Sequence[Any]) -> SessionSummary:
        return SessionSummary(
            session_id=text_value(row[0]),
            title=text_value(row[1]),
            title_source=text_value(row[2]),
            message_count=_int_value(row[3]),
            last_message_preview=text_value(row[4]),
            created_at=row[5],
            updated_at=row[6],
        )

    def get(
        self,
        session_id: str,
        *,
        include_deleted: bool = False,
    ) -> SessionRecord | None:
        """读一条会话;不存在返回 ``None``。"""
        sql = f"SELECT {_COLUMNS} FROM chat_sessions WHERE session_id = %s"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (session_id,))
                row = cur.fetchone()
        return self._to_record(row) if row is not None else None

    def get_or_raise(
        self,
        session_id: str,
        *,
        include_deleted: bool = False,
    ) -> SessionRecord:
        """读一条会话;不存在抛 ``SessionNotFound``。"""
        record = self.get(session_id, include_deleted=include_deleted)
        if record is None:
            raise SessionNotFound(session_id)
        return record

    def list(
        self,
        *,
        limit: int = 50,
        cursor: tuple[datetime, str] | None = None,
        title_prefix: str | None = None,
        include_deleted: bool = False,
    ) -> list[SessionSummary]:
        """列出会话(游标分页,按 ``updated_at DESC, session_id DESC``)。

        Args:
            limit: 返回条数上限。
            cursor: 上一页末项的 ``(updated_at, session_id)``;给了就只返回更旧的。
            title_prefix: 标题前缀过滤(``LIKE 'prefix%'``)。
            include_deleted: 是否包含已软删除的会话。

        Returns:
            ``SessionSummary`` 列表(**不含** ``state_json``)。
        """
        if limit <= 0:
            return []
        sql = f"SELECT {_SUMMARY_COLUMNS} FROM chat_sessions WHERE 1=1"
        params: list[Any] = []
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        if cursor is not None:
            sql += " AND (updated_at, session_id) < (%s, %s)"
            params.extend([cursor[0], cursor[1]])
        if title_prefix:
            sql += " AND title LIKE %s ESCAPE '\\'"
            params.append(f"{_escape_like(title_prefix)}%")
        sql += " ORDER BY updated_at DESC, session_id DESC LIMIT %s"
        params.append(int(limit))

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
        return [self._to_summary(row) for row in rows]

    # -- 写 -----------------------------------------------------------------
    def create(
        self,
        session_id: str,
        *,
        state_json: Mapping[str, Any] | None = None,
        title: str | None = None,
        title_source: str = TITLE_SOURCE_AUTO,
        message_count: int | None = None,
        last_message_preview: str | None = None,
    ) -> SessionRecord:
        """新建一条会话。

        Raises:
            SessionAlreadyExists: 该 ``session_id`` 已存在。
        """
        state = dict(state_json or {})
        if message_count is None or last_message_preview is None:
            derived_count, derived_preview = summarize_state(state)
            if message_count is None:
                message_count = derived_count
            if last_message_preview is None:
                last_message_preview = derived_preview
        if title_source not in VALID_TITLE_SOURCES:
            raise ValueError(
                f"invalid title_source {title_source!r}; "
                f"must be one of {sorted(VALID_TITLE_SOURCES)}"
            )
        sql = (
            f"INSERT INTO chat_sessions"
            f" (session_id, title, title_source, state_json, message_count, last_message_preview)"
            f" VALUES (%s, %s, %s, %s, %s, %s)"
            f" ON CONFLICT (session_id) DO NOTHING"
            f" RETURNING {_COLUMNS}"
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        session_id,
                        title if title is not None else DEFAULT_TITLE,
                        title_source,
                        jsonb(state),
                        int(message_count),
                        last_message_preview,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        if row is None:
            raise SessionAlreadyExists(session_id)
        return self._to_record(row)

    def create_if_absent(
        self,
        session_id: str,
        *,
        state_json: Mapping[str, Any] | None = None,
        title: str | None = None,
        title_source: str = TITLE_SOURCE_AUTO,
    ) -> tuple[SessionRecord, bool]:
        """幂等创建(供旧 ``session_state.json`` 导入用,方案 §5.4)。

        Returns:
            ``(会话记录, 是否本次新建)``。已存在时**不覆盖**原有内容。
        """
        state = dict(state_json or {})
        count, preview = summarize_state(state)
        sql = (
            f"INSERT INTO chat_sessions"
            f" (session_id, title, title_source, state_json, message_count, last_message_preview)"
            f" VALUES (%s, %s, %s, %s, %s, %s)"
            f" ON CONFLICT (session_id) DO NOTHING"
            f" RETURNING {_COLUMNS}"
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        session_id,
                        title if title is not None else DEFAULT_TITLE,
                        title_source,
                        jsonb(state),
                        count,
                        preview,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        if row is not None:
            return self._to_record(row), True
        existing = self.get(session_id, include_deleted=True)
        if existing is None:  # pragma: no cover - 理论上不可达
            raise SessionNotFound(session_id)
        return existing, False

    def update_state(
        self,
        session_id: str,
        state_json: Mapping[str, Any],
        *,
        expected_version: int | None = None,
        message_count: int | None = None,
        last_message_preview: str | None = None,
    ) -> SessionRecord:
        """更新会话状态(乐观锁)。

        方案 §6:``UPDATE ... WHERE session_id = %s AND version = %s``,
        影响行数为 0 ⇒ 已被他人更新 ⇒ 抛 ``SessionVersionConflict``,
        **不能直接覆盖**。

        Args:
            session_id: 会话 ID。
            state_json: 新的完整状态。
            expected_version: 期望的当前版本号;不给则先 ``SELECT ... FOR UPDATE``
                取当前版本再更新(仍在同一事务里,不会被并发写穿透)。
            message_count: 覆盖消息条数;不给则按新状态推导。
            last_message_preview: 覆盖末条预览;不给则按新状态推导。

        Raises:
            SessionNotFound: 会话不存在 / 已软删除。
            SessionVersionConflict: 版本对不上。
        """
        state = dict(state_json or {})
        derived_count, derived_preview = summarize_state(state)
        if message_count is None:
            message_count = derived_count
        if last_message_preview is None:
            last_message_preview = derived_preview

        sql = (
            "UPDATE chat_sessions SET"
            " state_json = %s,"
            " message_count = %s,"
            " last_message_preview = %s,"
            " updated_at = now(),"
            " version = version + 1"
            " WHERE session_id = %s AND version = %s AND deleted_at IS NULL"
            f" RETURNING {_COLUMNS}"
        )
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    if expected_version is None:
                        cur.execute(
                            "SELECT version FROM chat_sessions"
                            " WHERE session_id = %s AND deleted_at IS NULL"
                            " FOR UPDATE",
                            (session_id,),
                        )
                        row = cur.fetchone()
                        if row is None:
                            raise SessionNotFound(session_id)
                        expected_version = _int_value(row[0], default=1)

                    cur.execute(
                        sql,
                        (
                            jsonb(state),
                            int(message_count),
                            last_message_preview,
                            session_id,
                            int(expected_version),
                        ),
                    )
                    row = cur.fetchone()
                    if row is None:
                        # 0 行受影响:要么行不存在,要么 version 对不上。
                        cur.execute(
                            "SELECT version FROM chat_sessions WHERE session_id = %s",
                            (session_id,),
                        )
                        current = cur.fetchone()
                        if current is None:
                            raise SessionNotFound(session_id)
                        raise SessionVersionConflict(
                            session_id, int(expected_version)
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._to_record(row)

    def rename(
        self,
        session_id: str,
        title: str,
        *,
        title_source: str = TITLE_SOURCE_MANUAL,
    ) -> SessionRecord:
        """改标题(默认按**手动**处理,后续自动标题不会覆盖它)。

        Raises:
            SessionNotFound: 会话不存在 / 已软删除。
        """
        if title_source not in VALID_TITLE_SOURCES:
            raise ValueError(
                f"invalid title_source {title_source!r}; "
                f"must be one of {sorted(VALID_TITLE_SOURCES)}"
            )
        sql = (
            "UPDATE chat_sessions SET title = %s, title_source = %s, updated_at = now()"
            " WHERE session_id = %s AND deleted_at IS NULL"
            f" RETURNING {_COLUMNS}"
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (title, title_source, session_id))
                row = cur.fetchone()
            conn.commit()
        if row is None:
            raise SessionNotFound(session_id)
        return self._to_record(row)

    def apply_auto_title(self, session_id: str, title: str) -> SessionRecord | None:
        """自动标题:只在 ``title_source = 'auto'`` 时生效。

        手动改过的标题不会被自动标题覆盖(方案 §2.1「title_source」语义)。
        返回被更新的记录;若标题是手动设置的,返回 ``None``。
        """
        sql = (
            "UPDATE chat_sessions SET title = %s, updated_at = now()"
            " WHERE session_id = %s AND title_source = 'auto' AND deleted_at IS NULL"
            f" RETURNING {_COLUMNS}"
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (title, session_id))
                row = cur.fetchone()
            conn.commit()
        return self._to_record(row) if row is not None else None

    def soft_delete(self, session_id: str) -> bool:
        """软删除(打 ``deleted_at``)。返回是否真的删掉了。"""
        sql = (
            "UPDATE chat_sessions SET deleted_at = now(), updated_at = now()"
            " WHERE session_id = %s AND deleted_at IS NULL"
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (session_id,))
                affected = cur.rowcount
            conn.commit()
        return affected > 0

    def restore(self, session_id: str) -> bool:
        """恢复软删除的会话。返回是否真的恢复了。"""
        sql = (
            "UPDATE chat_sessions SET deleted_at = NULL, updated_at = now()"
            " WHERE session_id = %s AND deleted_at IS NOT NULL"
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (session_id,))
                affected = cur.rowcount
            conn.commit()
        return affected > 0
