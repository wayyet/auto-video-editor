"""会话存储 ↔ PostgreSQL 仓储的接线单测(阶段 D)。

不打真库:用**假仓储**注入 ``SessionRepository`` 的位置,验证:

- ``ChatSession.save_state_atomic`` 只写库(不再写 JSON 文件),写不进去时
  **抛异常**(fail-closed —— 绝不静默吞、也绝不回退去写文件);
- ``ChatSession.load_from_state`` 从库里恢复,恢复链(补 tool 结果 / 截断坏行)
  行为不变;``session_id`` 对不上 ⇒ 当作"不存在"(404 语义);
- ``SessionStore`` 的 create / get / save_session_state;
- 限流规则:``GET /api/sessions`` 走独立桶,``POST /api/sessions`` 不受影响,
  ``GET /api/sessions/{id}`` 仍然走通用桶。

约定
----
假仓储抛的异常**类名**跟 ``db.session_repository`` 一致
(``SessionNotFound`` / ``SessionAlreadyExists``)—— ``agent_fastapi`` 按类名
判断,是为了不在 import 期就依赖 psycopg(OpenStoryline 独立 venv 可能没装)。
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OPENSTORYLINE_DIR = REPO_ROOT / "openstoryline"

# ``open_storyline.nodes.node_manager`` 用的是 ``src.open_storyline...`` 这种写法
# ⇒ 需要把 ``openstoryline/`` 放进 sys.path(生产环境靠 cwd=openstoryline/ 达成);
# ``open_storyline`` 包本身在 ``openstoryline/src`` 下。
# 一律放**末尾**:仓库根的 ``config.py`` 必须优先于 ``openstoryline/config.py``。
for _p in (str(OPENSTORYLINE_DIR / "src"), str(OPENSTORYLINE_DIR)):
    if _p not in sys.path:
        sys.path.append(_p)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _chdir_openstoryline(monkeypatch):
    """prompt 模板走相对路径(``prompts/tasks/...``)⇒ cwd 必须是 ``openstoryline/``。"""
    monkeypatch.chdir(OPENSTORYLINE_DIR)


@pytest.fixture()
def cfg(tmp_path):
    """一份 Settings:所有目录都落在 ``tmp_path``。"""
    from open_storyline.config import MCodeConfig, ProjectConfig, load_settings

    base = load_settings(OPENSTORYLINE_DIR / "config.toml")
    project = ProjectConfig(
        media_dir=tmp_path / "media",
        bgm_dir=tmp_path / "bgm",
        outputs_dir=tmp_path / "outputs",
    )
    return base.model_copy(update={"project": project, "mcode": MCodeConfig(cwd="")})


# ---------------------------------------------------------------------------
# 假仓储
# ---------------------------------------------------------------------------
class SessionNotFound(RuntimeError):
    """(类名与 ``db.session_repository.SessionNotFound`` 对齐)"""

    def __init__(self, session_id: str = "") -> None:
        super().__init__(f"session not found: {session_id}")
        self.session_id = session_id


class SessionAlreadyExists(RuntimeError):
    """(类名与 ``db.session_repository.SessionAlreadyExists`` 对齐)"""

    def __init__(self, session_id: str = "") -> None:
        super().__init__(f"session already exists: {session_id}")
        self.session_id = session_id


@dataclass
class FakeRecord:
    """一条会话记录(字段与 ``db.session_repository.SessionRecord`` 对齐)。"""

    session_id: str
    title: str = "新对话"
    title_source: str = "auto"
    state_json: Dict[str, Any] = field(default_factory=dict)
    message_count: int = 0
    last_message_preview: str = ""
    created_at: datetime = field(default_factory=lambda: datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc))
    deleted_at: Optional[datetime] = None
    version: int = 1


class FakeSessionRepository:
    """内存版 ``SessionRepository``(只实现 ``agent_fastapi`` 用到的那几个方法)。"""

    def __init__(self) -> None:
        self.rows: Dict[str, FakeRecord] = {}
        self.fail_read = False
        self.fail_write = False
        self.write_count = 0

    # -- 读 ---------------------------------------------------------------
    def get(self, session_id: str, *, include_deleted: bool = False) -> Optional[FakeRecord]:
        if self.fail_read:
            raise RuntimeError("db down")
        rec = self.rows.get(session_id)
        if rec is None:
            return None
        if rec.deleted_at is not None and not include_deleted:
            return None
        return rec

    def list(
        self,
        *,
        limit: int = 50,
        cursor: Optional[Tuple[datetime, str]] = None,
        title_prefix: Optional[str] = None,
        include_deleted: bool = False,
    ) -> List[FakeRecord]:
        if self.fail_read:
            raise RuntimeError("db down")
        rows = [r for r in self.rows.values() if include_deleted or r.deleted_at is None]
        rows.sort(key=lambda r: (r.updated_at, r.session_id), reverse=True)
        if title_prefix:
            rows = [r for r in rows if r.title.startswith(title_prefix)]
        if cursor is not None:
            rows = [r for r in rows if (r.updated_at, r.session_id) < cursor]
        return rows[:limit]

    # -- 写 ---------------------------------------------------------------
    def create(
        self,
        session_id: str,
        *,
        state_json: Optional[Dict[str, Any]] = None,
        title: Optional[str] = None,
        title_source: str = "auto",
        message_count: Optional[int] = None,
        last_message_preview: Optional[str] = None,
    ) -> FakeRecord:
        if self.fail_write:
            raise RuntimeError("db down")
        if session_id in self.rows:
            raise SessionAlreadyExists(session_id)
        rec = FakeRecord(
            session_id=session_id,
            title=title if title is not None else "新对话",
            title_source=title_source,
            state_json=dict(state_json or {}),
        )
        self.rows[session_id] = rec
        self.write_count += 1
        return rec

    def create_if_absent(
        self,
        session_id: str,
        *,
        state_json: Optional[Dict[str, Any]] = None,
        title: Optional[str] = None,
        title_source: str = "auto",
    ) -> Tuple[FakeRecord, bool]:
        if self.fail_write:
            raise RuntimeError("db down")
        existing = self.rows.get(session_id)
        if existing is not None:
            return existing, False
        return self.create(session_id, state_json=state_json, title=title, title_source=title_source), True

    def update_state(
        self,
        session_id: str,
        state_json: Dict[str, Any],
        *,
        expected_version: Optional[int] = None,
        message_count: Optional[int] = None,
        last_message_preview: Optional[str] = None,
    ) -> FakeRecord:
        if self.fail_write:
            raise RuntimeError("db down")
        rec = self.rows.get(session_id)
        if rec is None or rec.deleted_at is not None:
            raise SessionNotFound(session_id)
        rec.state_json = dict(state_json or {})
        rec.updated_at = datetime(2026, 10, 29, 12, 30, tzinfo=timezone.utc)
        rec.version += 1
        self.write_count += 1
        return rec

    def rename(self, session_id: str, title: str, *, title_source: str = "manual") -> FakeRecord:
        if self.fail_write:
            raise RuntimeError("db down")
        rec = self.rows.get(session_id)
        if rec is None or rec.deleted_at is not None:
            raise SessionNotFound(session_id)
        rec.title = title
        rec.title_source = title_source
        rec.updated_at = datetime(2026, 10, 29, 12, 30, tzinfo=timezone.utc)
        return rec

    def soft_delete(self, session_id: str) -> bool:
        if self.fail_write:
            raise RuntimeError("db down")
        rec = self.rows.get(session_id)
        if rec is None or rec.deleted_at is not None:
            return False
        rec.deleted_at = datetime(2026, 10, 29, 12, 40, tzinfo=timezone.utc)
        return True

    def restore(self, session_id: str) -> bool:
        rec = self.rows.get(session_id)
        if rec is None or rec.deleted_at is None:
            return False
        rec.deleted_at = None
        return True


@pytest.fixture()
def repo() -> FakeSessionRepository:
    return FakeSessionRepository()


def _hex_id(seed: str) -> str:
    """造一个 32 位小写 hex 的 **UUID4** 会话 ID。

    必须是真的 UUID4:``_is_valid_session_id_hex`` 用 ``uuid.UUID(name).hex == name
    and version == 4`` 校验,不是随便 32 个 hex 字符就行。
    """
    raw = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:32]
    return f"{raw[:12]}4{raw[13:16]}8{raw[17:]}"


# ---------------------------------------------------------------------------
# ChatSession.save_state_atomic
# ---------------------------------------------------------------------------
def test_save_state_atomic_writes_to_repository(cfg, repo):
    """状态写进仓储;旧的 ``session_state.json`` **不再**产生。"""
    from openstoryline.agent_fastapi import ChatSession

    sid = _hex_id("a")
    sess = ChatSession(sid, cfg, repo=repo)
    sess.history.append({"role": "user", "message": "你好"})

    sess.save_state_atomic()

    rec = repo.rows[sid]
    assert rec is not None, "会话没写进仓储"
    assert rec.state_json["session_id"] == sid
    assert rec.state_json["history"][-1]["message"] == "你好"
    assert not os.path.exists(sess.state_file_path()), "不该再落 JSON 文件"


def test_save_state_atomic_raises_on_db_failure(cfg, repo):
    """写库失败 ⇒ 抛异常(fail-closed),且**不**回退去写 JSON 文件。"""
    from openstoryline.agent_fastapi import ChatSession, SessionStateUnavailableError

    sid = _hex_id("b")
    sess = ChatSession(sid, cfg, repo=repo)
    repo.fail_write = True

    with pytest.raises(SessionStateUnavailableError):
        sess.save_state_atomic()

    assert not os.path.exists(sess.state_file_path())


def test_save_state_atomic_creates_row_when_missing(cfg, repo):
    """行还不存在时 ⇒ 建一行(而不是报"不存在")。"""
    from openstoryline.agent_fastapi import ChatSession

    sid = _hex_id("c")
    sess = ChatSession(sid, cfg, repo=repo)
    sess.save_state_atomic()

    assert sid in repo.rows
    assert repo.rows[sid].version == 1


def test_save_state_atomic_does_not_resurrect_deleted_session(cfg, repo):
    """已软删除的会话不被写回(也不复活)。"""
    from openstoryline.agent_fastapi import ChatSession

    sid = _hex_id("d")
    sess = ChatSession(sid, cfg, repo=repo)
    sess.save_state_atomic()
    assert repo.soft_delete(sid) is True

    # 在行里打个标记:第二次保存**不该**覆盖它
    repo.rows[sid].state_json["marker"] = "before-delete"

    # 再存一次:行已被软删 ⇒ update_state 报 not found ⇒ create_if_absent 不覆盖
    sess.save_state_atomic()

    assert repo.rows[sid].deleted_at is not None
    assert repo.rows[sid].state_json.get("marker") == "before-delete"


# ---------------------------------------------------------------------------
# ChatSession.load_from_state
# ---------------------------------------------------------------------------
def test_load_from_state_restores_from_repository(cfg, repo):
    """从仓储里把会话恢复出来。"""
    from openstoryline.agent_fastapi import ChatSession

    sid = _hex_id("e")
    repo.rows[sid] = FakeRecord(
        session_id=sid,
        state_json={
            "version": 1,
            "session_id": sid,
            "lang": "zh",
            "history": [{"role": "user", "message": "在吗"}],
            "lc_messages_serialized": [],
        },
    )

    sess = ChatSession.load_from_state(sid, cfg, repo=repo)
    assert sess is not None
    assert sess.session_id == sid
    assert sess.history[-1]["message"] == "在吗"


def test_load_from_state_keeps_restore_chain(cfg, repo):
    """恢复链没变:AI 消息里没配对的 tool_call 会被补一条 cancelled 结果。"""
    from langchain_core.messages import ToolMessage

    from openstoryline.agent_fastapi import ChatSession

    sid = _hex_id("f")
    repo.rows[sid] = FakeRecord(
        session_id=sid,
        state_json={
            "version": 1,
            "session_id": sid,
            "lc_messages_serialized": [
                {
                    "type": "ai",
                    "content": "",
                    "additional_kwargs": {},
                    "tool_calls": [{"id": "call_1", "name": "noop", "args": {}}],
                },
            ],
        },
    )

    sess = ChatSession.load_from_state(sid, cfg, repo=repo)
    assert sess is not None
    tool_ids = [
        str(getattr(m, "tool_call_id", "") or "")
        for m in sess.lc_messages
        if isinstance(m, ToolMessage)
    ]
    assert "call_1" in tool_ids, "未闭合的 tool_call 应该被补上 cancelled 结果"


def test_load_from_state_session_id_mismatch_returns_none(cfg, repo):
    """存着的 session_id 对不上 ⇒ 当作"不存在"(404 语义,不是 503)。"""
    from openstoryline.agent_fastapi import ChatSession

    sid = _hex_id("g")
    repo.rows[sid] = FakeRecord(
        session_id=sid,
        state_json={"version": 1, "session_id": _hex_id("h"), "history": []},
    )

    assert ChatSession.load_from_state(sid, cfg, repo=repo) is None


def test_load_from_state_missing_session_returns_none(cfg, repo):
    from openstoryline.agent_fastapi import ChatSession

    assert ChatSession.load_from_state(_hex_id("i"), cfg, repo=repo) is None


def test_load_from_state_read_error_raises_unavailable(cfg, repo):
    """数据库读不到 ⇒ ``SessionStateUnavailableError``(⇒ 503)。"""
    from openstoryline.agent_fastapi import ChatSession, SessionStateUnavailableError

    repo.fail_read = True
    with pytest.raises(SessionStateUnavailableError):
        ChatSession.load_from_state(_hex_id("j"), cfg, repo=repo)


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------
async def test_store_create_inserts_repository_row(cfg, repo):
    """新建会话 ⇒ 内存里有、库里也有。"""
    from openstoryline.agent_fastapi import SessionStore

    store = SessionStore(cfg, repo=repo)
    sess = await store.create()

    assert sess.session_id in repo.rows
    assert repo.rows[sess.session_id].state_json["session_id"] == sess.session_id


async def test_store_get_loads_from_repository_on_cache_miss(cfg, repo):
    """缓存没命中 ⇒ 从库里读回来。"""
    from openstoryline.agent_fastapi import SessionStore

    sid = _hex_id("k")
    repo.rows[sid] = FakeRecord(
        session_id=sid,
        state_json={"version": 1, "session_id": sid, "history": [{"message": "冷会话"}]},
    )

    store = SessionStore(cfg, repo=repo)
    sess = await store.get(sid)
    assert sess is not None
    assert sess.history[-1]["message"] == "冷会话"


async def test_store_get_returns_none_for_unknown_session(cfg, repo):
    from openstoryline.agent_fastapi import SessionStore

    store = SessionStore(cfg, repo=repo)
    assert await store.get(_hex_id("l")) is None


async def test_store_save_session_state_raises_on_db_failure(cfg, repo):
    """``save_session_state`` **不再**吞异常(旧实现只 warning ⇒ 静默丢数据)。"""
    from openstoryline.agent_fastapi import ChatSession, SessionStateUnavailableError, SessionStore

    store = SessionStore(cfg, repo=repo)
    sess = ChatSession(_hex_id("m"), cfg, repo=repo)
    repo.fail_write = True

    with pytest.raises(SessionStateUnavailableError):
        await store.save_session_state(sess)


async def test_store_get_or_404_maps_db_error_to_503(cfg, repo):
    """数据库不可用 ⇒ ``get_or_404`` 抛 503(不是 404)。"""
    from fastapi import HTTPException

    from openstoryline.agent_fastapi import SessionStore

    store = SessionStore(cfg, repo=repo)
    repo.fail_read = True

    with pytest.raises(HTTPException) as exc:
        await store.get_or_404(_hex_id("n"))
    assert exc.value.status_code == 503


async def test_store_forget_drops_cached_session(cfg, repo):
    from openstoryline.agent_fastapi import SessionStore

    store = SessionStore(cfg, repo=repo)
    sess = await store.create()
    assert await store.get(sess.session_id) is not None

    await store.forget(sess.session_id)
    # 缓存没了 ⇒ 再 get 会重新从库里读(还在)
    assert await store.get(sess.session_id) is not None


# ---------------------------------------------------------------------------
# 限流规则
# ---------------------------------------------------------------------------
def test_rate_limit_rule_list_sessions_has_own_bucket():
    """``GET /api/sessions`` ⇒ 独立桶 ``list_sessions``。"""
    from openstoryline.agent_fastapi import _match_http_rule

    name, burst, rpm, cost = _match_http_rule("GET", "/api/sessions")
    assert name == "list_sessions"
    assert burst > 0 and rpm > 0 and cost == 1.0


def test_rate_limit_rule_create_session_unchanged():
    """``POST /api/sessions`` 仍然是 ``create_session``(没被 GET 规则抢走)。"""
    from openstoryline.agent_fastapi import _match_http_rule

    assert _match_http_rule("POST", "/api/sessions")[0] == "create_session"


def test_rate_limit_rule_session_by_id_stays_general():
    """``GET /api/sessions/{id}`` 仍走通用桶(行为不变)。"""
    from openstoryline.agent_fastapi import _match_http_rule

    assert _match_http_rule("GET", "/api/sessions/abc")[0] == "api_general"
