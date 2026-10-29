"""``chat_sessions`` 仓储的 PostgreSQL 集成测试(方案 §7.2 验收用例 3 / 6 / 7)。

打真实库,验证:
- 创建 / 读取 / 更新 / 软删除 / 恢复;
- 另一条连接能读到完整状态(跨实例可见);
- 乐观锁冲突不静默覆盖;
- 游标分页 + 标题前缀过滤;
- 中文状态(``state_json`` / ``title``)在 SQL_ASCII 库上能正确往返;
- 迁移脚本幂等(重复执行不报错、不重复建表)。

测试库用独立变量 ``TEST_POSTGRES_URI``;显式指定却连不上 ⇒ 直接 fail,
不静默 skip(方案 §7.2,杜绝"假绿")。
"""

from __future__ import annotations

import os
import socket
import uuid

import pytest

from db.migrations import MIGRATIONS_TABLE_NAME, apply_migrations
from db.session_repository import (
    SessionAlreadyExists,
    SessionNotFound,
    SessionRepository,
    SessionVersionConflict,
)


def _default_uri() -> str:
    try:
        from config import POSTGRES_URI

        return POSTGRES_URI
    except Exception:  # pragma: no cover
        return "postgresql://postgres:devpass@localhost:5432/video_workflow"


TEST_URI = (
    os.environ.get("TEST_POSTGRES_URI")
    or os.environ.get("POSTGRES_URI")
    or _default_uri()
)
_URI_IS_EXPLICIT = bool(
    os.environ.get("TEST_POSTGRES_URI") or os.environ.get("POSTGRES_URI")
)


def _reachable(uri: str, timeout: float = 1.5) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        (not _URI_IS_EXPLICIT) and not _reachable(TEST_URI),
        reason=(
            f"Postgres not reachable at {TEST_URI}(本机未启或 URI 不通)— "
            "跳过 chat_sessions 集成测试;显式设置 TEST_POSTGRES_URI 可让连不上直接 fail"
        ),
    ),
]


@pytest.fixture()
def repo() -> SessionRepository:
    """指向真实测试库的仓储。"""
    return SessionRepository(uri=TEST_URI)


@pytest.fixture()
def session_id() -> str:
    return uuid.uuid4().hex


def test_migrations_are_idempotent(repo: SessionRepository) -> None:
    """重复执行迁移不报错(阶段 B 门禁:幂等)。"""
    from db.connection import connection as _db_connection

    with _db_connection(TEST_URI) as conn:
        first = apply_migrations(conn)
        second = apply_migrations(conn)
    # 第一次可能执行了迁移,也可能数据库早已是最新;第二次一定无事可做。
    assert set(first) <= {1, 2}
    assert second == []


def test_migration_table_exists(repo: SessionRepository) -> None:
    from db.connection import connection as _db_connection

    with _db_connection(TEST_URI) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = %s",
                (MIGRATIONS_TABLE_NAME,),
            )
            assert cur.fetchone() is not None


def test_create_and_get(repo: SessionRepository, session_id: str) -> None:
    state = {"version": 1, "history": [{"role": "user", "message": "你好,世界"}]}
    created = repo.create(session_id, state_json=state)

    assert created.session_id == session_id
    assert created.title == "新对话"
    assert created.title_source == "auto"
    assert created.message_count == 1
    assert created.version == 1

    fetched = repo.get(session_id)
    assert fetched is not None
    assert fetched.state_json == state
    assert fetched.last_message_preview == "你好,世界"


def test_create_rejects_duplicate(repo: SessionRepository, session_id: str) -> None:
    repo.create(session_id, state_json={"history": []})
    with pytest.raises(SessionAlreadyExists):
        repo.create(session_id, state_json={"history": []})


def test_another_connection_sees_the_state(repo: SessionRepository, session_id: str) -> None:
    """另一条连接(≈另一个应用实例)能读到完整状态(方案 §7.2 用例 3)。"""
    repo.create(session_id, state_json={"history": [{"message": "跨实例可见"}]})

    other = SessionRepository(uri=TEST_URI)
    record = other.get(session_id)
    assert record is not None
    assert record.state_json["history"][0]["message"] == "跨实例可见"


def test_chinese_state_round_trips(repo: SessionRepository, session_id: str) -> None:
    """SQL_ASCII 库上中文必须能原样往返(方案 §3.2 A5)。"""
    state = {
        "history": [{"role": "assistant", "message": "这是中文内容,含标点:，。!"}],
        "custom_llm_config": {"model": "深度求索"},
    }
    repo.create(session_id, state_json=state, title="中文标题")

    record = repo.get(session_id)
    assert record is not None
    assert record.title == "中文标题"
    assert record.state_json["history"][0]["message"] == "这是中文内容,含标点:，。!"
    assert record.state_json["custom_llm_config"]["model"] == "深度求索"


def test_update_state_bumps_version(repo: SessionRepository, session_id: str) -> None:
    repo.create(session_id, state_json={"history": [{"message": "v1"}]})
    updated = repo.update_state(session_id, {"history": [{"message": "v2"}]}, expected_version=1)
    assert updated.version == 2
    assert updated.state_json["history"][0]["message"] == "v2"


def test_version_conflict_does_not_overwrite(repo: SessionRepository, session_id: str) -> None:
    """版本冲突 ⇒ 抛冲突 + **不覆盖**已有内容(方案 §6 / §7.2 用例 9)。"""
    repo.create(session_id, state_json={"history": [{"message": "原始"}]})
    repo.update_state(session_id, {"history": [{"message": "别人写的"}]}, expected_version=1)

    with pytest.raises(SessionVersionConflict):
        repo.update_state(session_id, {"history": [{"message": "我要覆盖"}]}, expected_version=1)

    current = repo.get(session_id)
    assert current is not None
    assert current.state_json["history"][0]["message"] == "别人写的"


def test_manual_title_is_not_overwritten_by_auto_title(
    repo: SessionRepository, session_id: str
) -> None:
    repo.create(session_id, state_json={"history": []})
    repo.rename(session_id, "我手动改的")

    result = repo.apply_auto_title(session_id, "自动生成的")
    assert result is None

    record = repo.get(session_id)
    assert record is not None
    assert record.title == "我手动改的"
    assert record.title_source == "manual"


def test_soft_delete_hides_session_then_restore(
    repo: SessionRepository, session_id: str
) -> None:
    repo.create(session_id, state_json={"history": []})
    assert repo.soft_delete(session_id) is True
    assert repo.get(session_id) is None
    assert repo.get(session_id, include_deleted=True) is not None

    assert repo.restore(session_id) is True
    assert repo.get(session_id) is not None


def test_soft_delete_is_idempotent(repo: SessionRepository, session_id: str) -> None:
    repo.create(session_id, state_json={"history": []})
    assert repo.soft_delete(session_id) is True
    assert repo.soft_delete(session_id) is False


def test_list_uses_cursor_pagination(repo: SessionRepository) -> None:
    """游标分页:第一页 + 下一页正好覆盖全部,不重不漏。"""
    prefix = f"分页{uuid.uuid4().hex[:8]}"
    ids = [uuid.uuid4().hex for _ in range(3)]
    for sid in ids:
        repo.create(sid, state_json={"history": []}, title=f"{prefix}-{sid[:4]}")

    first_page = repo.list(limit=2, title_prefix=prefix)
    assert len(first_page) == 2

    last = first_page[-1]
    second_page = repo.list(
        limit=10,
        cursor=(last.updated_at, last.session_id),
        title_prefix=prefix,
    )
    assert len(second_page) == 1

    seen = [s.session_id for s in first_page] + [s.session_id for s in second_page]
    assert len(seen) == len(set(seen))  # 不重复
    assert set(seen) == set(ids)  # 不遗漏


def test_list_title_prefix_filter(repo: SessionRepository) -> None:
    sid = uuid.uuid4().hex
    repo.create(sid, state_json={"history": []}, title="独特前缀ABC")

    hits = repo.list(limit=50, title_prefix="独特前缀")
    assert any(s.session_id == sid for s in hits)

    misses = repo.list(limit=50, title_prefix="独特前缀XYZ")
    assert not any(s.session_id == sid for s in misses)


def test_get_or_raise_raises(repo: SessionRepository) -> None:
    with pytest.raises(SessionNotFound):
        repo.get_or_raise(uuid.uuid4().hex)


def test_workflow_runs_check_constraint_rejects_hex_thread_id(
    repo: SessionRepository,
) -> None:
    """thread_id 的 CHECK 必须挡掉 ``\\x<hex>`` 字面量(方案 §3.2 A2)。"""
    import psycopg

    from db.connection import connection as _db_connection

    with pytest.raises(psycopg.errors.CheckViolation):
        with _db_connection(TEST_URI) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO workflow_runs(thread_id, video_name, status) "
                    "VALUES (%s, %s, %s)",
                    ("\\x766964656f2d776972696e672d303031", "x", "running"),
                )
            conn.commit()


def test_workflow_runs_accepts_normal_thread_id(repo: SessionRepository) -> None:
    """正常 thread_id 必须能写入(别把 CHECK 写太严)。"""
    from db.connection import connection as _db_connection

    with _db_connection(TEST_URI) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workflow_runs(thread_id, video_name, status) "
                "VALUES (%s, %s, %s) ON CONFLICT (thread_id) DO UPDATE "
                "SET status = EXCLUDED.status",
                ("video-wiring-001", "x", "running"),
            )
        conn.commit()
