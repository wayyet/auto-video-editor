"""``monitoring.workflow_chat_link_store`` 单元测试(执行设计 §10.4 任务 1.5/1.6)。

覆盖:
- ID / 状态校验(纯逻辑,不连库);
- 旧 checkpoint 兼容:``WorkflowState.chat_session_id`` 为可选键,缺失时
  ``.get()`` 返回 ``None``(任务 1.6);
- Store 行为(upsert 覆盖、四态流转、无记录查询、非法 ID 拒绝、FK 降级、
  存储不可用上抛)—— 打**隔离测试库**,复用
  ``tests/integration/conftest.py`` 的建库工具(每用例独立库,不碰生产库,
  方案 §7.2"测试不允许静默 skip":库连不上直接 fail)。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from monitoring.workflow_chat_link_store import (
    VALID_LINK_STATUSES,
    ChatLinkStoreUnavailable,
    ChatLinkValidationError,
    WorkflowChatLinkStore,
    degraded_count,
    validate_chat_session_id,
    validate_thread_id,
)

_CSID = "0123456789abcdef0123456789abcdef"
_CSID2 = "fedcba9876543210fedcba9876543210"


# ---------------------------------------------------------------------------
# 1. ID / 状态校验(任务 1.4;API 与 Store 共用)
# ---------------------------------------------------------------------------
def test_validate_chat_session_id_accepts_32hex_and_normalizes() -> None:
    assert validate_chat_session_id(_CSID) == _CSID
    # 去空白 + 转小写(uuid4().hex 生成端恒为小写,这里防御大写输入)
    assert validate_chat_session_id(f"  {_CSID.upper()} ") == _CSID


@pytest.mark.parametrize(
    "raw",
    ["", None, "   ", "short", "0" * 31, "0" * 33, "z" * 32, "会话id".ljust(32, "x")],
)
def test_validate_chat_session_id_rejects(raw: Any) -> None:
    with pytest.raises(ChatLinkValidationError):
        validate_chat_session_id(raw)


def test_validate_thread_id_accepts_workflow_runs_shape() -> None:
    assert validate_thread_id("video-web-001") == "video-web-001"
    assert validate_thread_id("a:b.c_d-e") == "a:b.c_d-e"


@pytest.mark.parametrize("raw", ["", None, "带 空格", "路径/注入", "x" * 129])
def test_validate_thread_id_rejects(raw: Any) -> None:
    with pytest.raises(ChatLinkValidationError):
        validate_thread_id(raw)


def test_valid_link_statuses_are_the_four_contract_states() -> None:
    assert VALID_LINK_STATUSES == frozenset(
        {"running", "waiting_human", "completed", "failed"}
    )


# ---------------------------------------------------------------------------
# 2. 旧 checkpoint 兼容(任务 1.6):可选键缺失 → .get() 返回 None
# ---------------------------------------------------------------------------
def test_workflow_state_has_optional_chat_session_id() -> None:
    from state import WorkflowState

    assert "chat_session_id" in WorkflowState.__annotations__


def test_old_checkpoint_state_without_field_reads_none() -> None:
    # 旧 checkpoint / 旧 initial_state 不含 chat_session_id ——
    # 统一 .get() 访问约定下读到 None,不抛 KeyError(执行设计 §12 第 10 条)。
    old_state: dict = {"session_id": "video-001", "video_input_path": "inputs/x.mp4"}
    assert old_state.get("chat_session_id") is None


# ---------------------------------------------------------------------------
# 3. Store 行为(隔离测试库)
# ---------------------------------------------------------------------------
@pytest.fixture
def link_db_uri() -> str:
    """独立隔离库(唯一命名,用例结束删除);复用集成侧建库工具。"""
    from tests.integration.conftest import (
        base_postgres_uri,
        create_test_db,
        drop_test_db,
        postgres_reachable,
        uri_with_dbname,
    )

    base = base_postgres_uri()
    if not postgres_reachable(base):
        pytest.fail(
            f"Postgres 不可达: {base} —— 关联存储测试不允许静默 skip;"
            "请先启动 Postgres 容器。"
        )
    name = f"video_workflow_linktest_{uuid.uuid4().hex[:8]}"
    uri = uri_with_dbname(base, name)
    if not create_test_db(uri):
        pytest.fail(f"无法创建隔离测试库 {name}(权限不足?)")
    yield uri
    drop_test_db(uri)


def _seed_chat_session(uri: str, session_id: str) -> None:
    """插入 ``chat_sessions`` 行(关联表 FK 的目标;默认值覆盖其余列)。"""
    import psycopg

    with psycopg.connect(uri, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_sessions (session_id) VALUES (%s) "
                "ON CONFLICT (session_id) DO NOTHING",
                (session_id,),
            )
        conn.commit()


def _link_count(uri: str) -> int:
    import psycopg

    with psycopg.connect(uri, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM workflow_chat_links")
            return int(cur.fetchone()[0])


def test_upsert_running_creates_then_overwrites(link_db_uri: str) -> None:
    _seed_chat_session(link_db_uri, _CSID)
    store = WorkflowChatLinkStore(uri=link_db_uri)

    assert store.upsert_running(_CSID, "video-001", "run-aaa") is True
    link = store.get_by_chat_session(_CSID)
    assert link == {
        "chat_session_id": _CSID,
        "thread_id": "video-001",
        "run_id": "run-aaa",
        "status": "running",
        "updated_at": link["updated_at"],
    }
    assert link["updated_at"].endswith("Z")

    # 同会话再次启动:覆盖为最近一次(每会话单行,§4.3)
    assert store.upsert_running(_CSID, "video-002", "run-bbb") is True
    link2 = store.get_by_chat_session(_CSID)
    assert link2["thread_id"] == "video-002"
    assert link2["run_id"] == "run-bbb"
    assert link2["status"] == "running"
    assert _link_count(link_db_uri) == 1


def test_status_four_state_transitions(link_db_uri: str) -> None:
    _seed_chat_session(link_db_uri, _CSID)
    store = WorkflowChatLinkStore(uri=link_db_uri)
    store.upsert_running(_CSID, "video-001", "run-aaa")

    # running → waiting_human(interrupt)→ running(resume)→ completed
    assert store.update_status(_CSID, "waiting_human", run_id="run-aaa") is True
    assert store.get_by_chat_session(_CSID)["status"] == "waiting_human"
    assert store.update_status(_CSID, "running", run_id="run-bbb") is True
    assert store.get_by_chat_session(_CSID)["status"] == "running"
    assert store.get_by_chat_session(_CSID)["run_id"] == "run-bbb"
    assert store.update_status(_CSID, "completed") is True
    assert store.get_by_chat_session(_CSID)["status"] == "completed"

    # 新工作流启动覆盖 → running → failed
    store.upsert_running(_CSID, "video-002", "run-ccc")
    assert store.get_by_chat_session(_CSID)["thread_id"] == "video-002"
    assert store.update_status(_CSID, "failed") is True
    assert store.get_by_chat_session(_CSID)["status"] == "failed"


def test_get_no_record_returns_none(link_db_uri: str) -> None:
    # 有会话无关联;以及合法格式但库里根本没有该会话(读不受 FK 限制)
    _seed_chat_session(link_db_uri, _CSID)
    store = WorkflowChatLinkStore(uri=link_db_uri)
    assert store.get_by_chat_session(_CSID) is None
    assert store.get_by_chat_session(_CSID2) is None


def test_invalid_ids_rejected_without_db_write(link_db_uri: str) -> None:
    store = WorkflowChatLinkStore(uri=link_db_uri)
    with pytest.raises(ChatLinkValidationError):
        store.upsert_running("not-hex", "video-001")
    with pytest.raises(ChatLinkValidationError):
        store.upsert_running(_CSID, "坏 thread_id")
    with pytest.raises(ChatLinkValidationError):
        store.update_status("not-hex", "running")
    with pytest.raises(ChatLinkValidationError):
        store.update_status(_CSID, "bogus_status")
    with pytest.raises(ChatLinkValidationError):
        store.get_by_chat_session("nope")
    assert _link_count(link_db_uri) == 0, "非法输入不得产生任何写入"


def test_fk_unknown_chat_session_degrades(link_db_uri: str) -> None:
    """合法格式但 chat_sessions 无此会话 → FK 拒绝 → 降级 False + 计数。"""
    before = degraded_count()
    store = WorkflowChatLinkStore(uri=link_db_uri)
    assert store.upsert_running(_CSID, "video-001") is False
    assert degraded_count() > before
    assert _link_count(link_db_uri) == 0


def test_update_status_missing_row_without_thread_id_is_honest_false(
    link_db_uri: str,
) -> None:
    """无关联行且未提供 thread_id → 如实返回 False,不冒充成功。"""
    _seed_chat_session(link_db_uri, _CSID)
    store = WorkflowChatLinkStore(uri=link_db_uri)
    assert store.update_status(_CSID, "completed") is False
    assert store.get_by_chat_session(_CSID) is None


def test_get_on_unreachable_store_raises_unavailable(monkeypatch) -> None:
    """查询失败必须上抛(§5.1:503,不得冒充"无关联")。"""
    monkeypatch.setenv("POSTGRES_CONNECT_TIMEOUT", "1")
    store = WorkflowChatLinkStore(
        uri="postgresql://postgres:devpass@127.0.0.1:5991/no_such_db"
    )
    with pytest.raises(ChatLinkStoreUnavailable) as excinfo:
        store.get_by_chat_session(_CSID)
    assert not isinstance(excinfo.value, ChatLinkValidationError)
