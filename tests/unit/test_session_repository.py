"""``db.session_repository`` 的单元测试(方案 §7.1)。

用假连接驱动仓储的分支逻辑,不依赖真实 Postgres:
- 创建 / 读取 / 更新 / 软删除 / 恢复
- 游标分页 + 标题前缀过滤
- ``title_source`` 手动优先(自动标题不覆盖手动标题)
- ``version`` 乐观锁冲突(影响行数为 0 ⇒ 抛冲突,**不覆盖**)

真实库的端到端行为在 ``tests/integration/test_session_repository_postgres.py``。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

import pytest

from db.session_repository import (
    SessionAlreadyExists,
    SessionNotFound,
    SessionRepository,
    SessionVersionConflict,
    jsonb,
    summarize_state,
    text_value,
)


# ---------------------------------------------------------------------------
# 假连接
# ---------------------------------------------------------------------------
@dataclass
class FakeResponse:
    """一次 execute 的预设结果。"""

    rows: list[tuple] = field(default_factory=list)
    rowcount: int = 0


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn
        self._rows: list[tuple] = []
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        self._conn.executed.append((sql, tuple(params or ())))
        response = self._conn._next()
        self._rows = response.rows
        self.rowcount = response.rowcount

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class FakeConnection:
    """按调用顺序回放预设结果的假连接。"""

    def __init__(self, responses: Sequence[FakeResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.executed: list[tuple[str, tuple]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def _next(self) -> FakeResponse:
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse()


def make_repo(responses: Sequence[FakeResponse] | None = None) -> tuple[SessionRepository, FakeConnection]:
    """构造一个用假连接驱动的仓储,返回 ``(repo, conn)``。"""
    conn = FakeConnection(responses)

    @contextmanager
    def factory() -> Iterator[FakeConnection]:
        yield conn

    return SessionRepository(conn_factory=factory), conn


# 一条完整会话行的 10 列(session_id..version)。
def record_row(
    session_id: str = "a" * 32,
    title: str = "新对话",
    title_source: str = "auto",
    state_json: dict | None = None,
    message_count: int = 0,
    preview: str = "",
    version: int = 1,
) -> tuple:
    return (
        session_id,
        title,
        title_source,
        state_json if state_json is not None else {},
        message_count,
        preview,
        "2026-10-29 00:00:00+00:00",
        "2026-10-29 00:00:00+00:00",
        None,
        version,
    )


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------
def test_text_value_decodes_bytes() -> None:
    """SQL_ASCII 库下 TEXT 列是 bytes,必须能解码成 str。"""
    assert text_value(b"\xe6\x96\xb0\xe5\xaf\xb9\xe8\xaf\x9d") == "新对话"
    assert text_value("abc") == "abc"
    assert text_value(None) == ""


def test_summarize_state_counts_and_previews() -> None:
    state = {"history": [{"role": "user", "message": "你好"}, {"role": "assistant", "message": "在的"}]}
    count, preview = summarize_state(state)
    assert count == 2
    assert preview == "在的"


def test_summarize_state_handles_empty_and_bad_shape() -> None:
    assert summarize_state({}) == (0, "")
    assert summarize_state({"history": []}) == (0, "")
    assert summarize_state({"history": "not-a-list"}) == (0, "")
    assert summarize_state(None) == (0, "")


def test_summarize_state_truncates_long_preview() -> None:
    state = {"history": [{"message": "x" * 500}]}
    count, preview = summarize_state(state)
    assert count == 1
    assert len(preview) <= 200


def test_jsonb_uses_ensure_ascii_false() -> None:
    """JSONB 必须 ensure_ascii=False,否则 SQL_ASCII 库报 UntranslatableCharacter。"""
    dumped = jsonb({"k": "中文值"}).dumps({"k": "中文值"})
    assert "\\u" not in dumped
    assert "中文值" in dumped


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------
def test_create_inserts_and_returns_record() -> None:
    repo, conn = make_repo([FakeResponse(rows=[record_row(title="我的会话")])])
    record = repo.create("a" * 32, state_json={"history": [{"message": "hi"}]})

    sql, params = conn.executed[0]
    assert "INSERT INTO chat_sessions" in sql
    assert "ON CONFLICT (session_id) DO NOTHING" in sql
    assert params[0] == "a" * 32
    assert params[1] == "新对话"  # 默认标题
    assert params[2] == "auto"  # 默认 title_source
    assert record.session_id == "a" * 32
    assert record.title == "我的会话"
    assert conn.commits == 1


def test_create_raises_when_session_id_exists() -> None:
    """ON CONFLICT DO NOTHING ⇒ RETURNING 无行 ⇒ 抛 SessionAlreadyExists。"""
    repo, _ = make_repo([FakeResponse(rows=[])])
    with pytest.raises(SessionAlreadyExists):
        repo.create("b" * 32)


def test_create_rejects_invalid_title_source() -> None:
    repo, _ = make_repo([])
    with pytest.raises(ValueError):
        repo.create("c" * 32, title_source="weird")


def test_create_if_absent_is_idempotent() -> None:
    """重复导入不覆盖已有会话(方案 §5.4)。"""
    repo, _ = make_repo(
        [
            FakeResponse(rows=[]),  # INSERT ... ON CONFLICT DO NOTHING → 无行
            FakeResponse(rows=[record_row(title="旧的")]),  # 回读已有行
        ]
    )
    record, created = repo.create_if_absent("d" * 32, state_json={"history": []})
    assert created is False
    assert record.title == "旧的"


def test_create_if_absent_reports_created() -> None:
    repo, _ = make_repo([FakeResponse(rows=[record_row(session_id="e" * 32)])])
    record, created = repo.create_if_absent("e" * 32)
    assert created is True
    assert record.session_id == "e" * 32


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def test_get_returns_none_when_missing() -> None:
    repo, _ = make_repo([FakeResponse(rows=[])])
    assert repo.get("f" * 32) is None


def test_get_or_raise_raises_session_not_found() -> None:
    repo, _ = make_repo([FakeResponse(rows=[])])
    with pytest.raises(SessionNotFound):
        repo.get_or_raise("f" * 32)


def test_get_excludes_deleted_by_default() -> None:
    repo, conn = make_repo([FakeResponse(rows=[record_row()])])
    repo.get("a" * 32)
    sql, _ = conn.executed[0]
    assert "deleted_at IS NULL" in sql


def test_get_can_include_deleted() -> None:
    repo, conn = make_repo([FakeResponse(rows=[record_row()])])
    repo.get("a" * 32, include_deleted=True)
    sql, _ = conn.executed[0]
    assert "deleted_at IS NULL" not in sql


# ---------------------------------------------------------------------------
# 更新 / 乐观锁
# ---------------------------------------------------------------------------
def test_update_state_bumps_version() -> None:
    repo, conn = make_repo([FakeResponse(rows=[record_row(version=2)])])
    record = repo.update_state("a" * 32, {"history": [{"message": "新"}]}, expected_version=1)

    sql, params = conn.executed[0]
    assert "version = version + 1" in sql
    assert "AND version = %s" in sql
    assert params[-1] == 1  # expected_version
    assert record.version == 2


def test_update_state_raises_conflict_and_rolls_back() -> None:
    """影响行数为 0 且行存在 ⇒ 版本冲突,**不能覆盖**。"""
    repo, conn = make_repo(
        [
            FakeResponse(rows=[]),  # UPDATE ... RETURNING → 0 行
            FakeResponse(rows=[(5,)]),  # 复查:行存在,当前版本 5
        ]
    )
    with pytest.raises(SessionVersionConflict) as exc:
        repo.update_state("a" * 32, {"history": []}, expected_version=1)
    assert exc.value.session_id == "a" * 32
    assert exc.value.expected_version == 1
    assert conn.rollbacks == 1
    assert conn.commits == 0


def test_update_state_raises_not_found_when_row_missing() -> None:
    repo, conn = make_repo(
        [
            FakeResponse(rows=[]),  # UPDATE → 0 行
            FakeResponse(rows=[]),  # 复查:行不存在
        ]
    )
    with pytest.raises(SessionNotFound):
        repo.update_state("a" * 32, {"history": []}, expected_version=1)
    assert conn.rollbacks == 1


def test_update_state_without_expected_version_locks_row_first() -> None:
    """不给 expected_version ⇒ 先 SELECT ... FOR UPDATE 取当前版本再更新。"""
    repo, conn = make_repo(
        [
            FakeResponse(rows=[(3,)]),  # SELECT version ... FOR UPDATE
            FakeResponse(rows=[record_row(version=4)]),  # UPDATE ... RETURNING
        ]
    )
    record = repo.update_state("a" * 32, {"history": []})
    assert "FOR UPDATE" in conn.executed[0][0]
    assert conn.executed[1][1][-1] == 3
    assert record.version == 4


# ---------------------------------------------------------------------------
# 标题
# ---------------------------------------------------------------------------
def test_rename_defaults_to_manual_source() -> None:
    repo, conn = make_repo([FakeResponse(rows=[record_row(title="手动标题", title_source="manual")])])
    record = repo.rename("a" * 32, "手动标题")

    sql, params = conn.executed[0]
    assert "title_source = %s" in sql
    assert params[1] == "manual"
    assert record.title_source == "manual"


def test_apply_auto_title_does_not_overwrite_manual_title() -> None:
    """手动标题不被自动标题覆盖 ⇒ WHERE 带 title_source = 'auto'。"""
    repo, conn = make_repo([FakeResponse(rows=[])])
    result = repo.apply_auto_title("a" * 32, "自动标题")

    sql, _ = conn.executed[0]
    assert "title_source = 'auto'" in sql
    assert result is None  # 手动标题的行不会被更新


def test_apply_auto_title_updates_auto_title() -> None:
    repo, _ = make_repo([FakeResponse(rows=[record_row(title="自动标题")])])
    result = repo.apply_auto_title("a" * 32, "自动标题")
    assert result is not None
    assert result.title == "自动标题"


def test_rename_raises_when_session_missing() -> None:
    repo, _ = make_repo([FakeResponse(rows=[])])
    with pytest.raises(SessionNotFound):
        repo.rename("a" * 32, "x")


# ---------------------------------------------------------------------------
# 软删除 / 恢复
# ---------------------------------------------------------------------------
def test_soft_delete_returns_true_when_row_updated() -> None:
    repo, conn = make_repo([FakeResponse(rowcount=1)])
    assert repo.soft_delete("a" * 32) is True
    sql, _ = conn.executed[0]
    assert "deleted_at = now()" in sql
    assert "deleted_at IS NULL" in sql


def test_soft_delete_returns_false_when_already_deleted() -> None:
    repo, _ = make_repo([FakeResponse(rowcount=0)])
    assert repo.soft_delete("a" * 32) is False


def test_restore_clears_deleted_at() -> None:
    repo, conn = make_repo([FakeResponse(rowcount=1)])
    assert repo.restore("a" * 32) is True
    sql, _ = conn.executed[0]
    assert "deleted_at = NULL" in sql
    assert "deleted_at IS NOT NULL" in sql


# ---------------------------------------------------------------------------
# 分页
# ---------------------------------------------------------------------------
def test_list_orders_by_updated_at_desc() -> None:
    repo, conn = make_repo([FakeResponse(rows=[])])
    repo.list()
    sql, params = conn.executed[0]
    assert "ORDER BY updated_at DESC, session_id DESC" in sql
    assert "LIMIT %s" in sql
    assert params[-1] == 50
    assert "deleted_at IS NULL" in sql


def test_list_with_cursor_uses_row_value_comparison() -> None:
    """游标分页用 (updated_at, session_id) 行值比较,不用 offset。"""
    repo, conn = make_repo([FakeResponse(rows=[])])
    repo.list(limit=10, cursor=("2026-10-29 00:00:00+00:00", "a" * 32))

    sql, params = conn.executed[0]
    assert "(updated_at, session_id) < (%s, %s)" in sql
    assert "OFFSET" not in sql.upper()
    assert params[0] == "2026-10-29 00:00:00+00:00"
    assert params[1] == "a" * 32
    assert params[-1] == 10


def test_list_title_prefix_is_escaped() -> None:
    """标题里的 % / _ 不能当通配符 —— 必须转义 + ESCAPE。"""
    repo, conn = make_repo([FakeResponse(rows=[])])
    repo.list(title_prefix="100%_x")

    sql, params = conn.executed[0]
    assert "title LIKE %s" in sql
    assert "ESCAPE" in sql
    assert params[0] == "100\\%\\_x%"


def test_list_non_positive_limit_returns_empty_without_query() -> None:
    repo, conn = make_repo([])
    assert repo.list(limit=0) == []
    assert conn.executed == []


def test_list_maps_summary_rows_without_state_json() -> None:
    """列表响应不含 state_json(方案 §4.5)。"""
    repo, _ = make_repo(
        [
            FakeResponse(
                rows=[
                    (
                        "a" * 32,
                        "标题",
                        "auto",
                        3,
                        "末条",
                        "2026-10-29 00:00:00+00:00",
                        "2026-10-29 00:00:00+00:00",
                    )
                ]
            )
        ]
    )
    summaries = repo.list()
    assert len(summaries) == 1
    assert summaries[0].message_count == 3
    assert summaries[0].last_message_preview == "末条"
    assert not hasattr(summaries[0], "state_json")
