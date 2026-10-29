"""会话 API(PostgreSQL)集成测试(阶段 D 验收用例 3~7)。

打**真库**(本机 Postgres),验证:

- **用例 3**:新建会话 ⇒ 另一条连接 / 另一个 app 实例能读到完整状态;
- **用例 4**:聊完一轮后,会话列表里仍然能看到它;
- **用例 5**:第二个客户端(换个"浏览器")访问同一个服务,看到同一份列表;
- **用例 6**:改名 ⇒ 换一个新的 ``SessionStore`` 仍在;标题 / ``updated_at`` / 排序都保住;
- **用例 7**:软删除 ⇒ 默认列表里没有它;``repo.restore()`` 还能救回来。

另外断言:

- ``save_session_state`` 在数据库故障时**抛异常**(不再静默吞);
- ``GET /api/sessions`` **绝不**返回 ``state_json``;
- 列表响应的形状与契约完全一致(key 与类型);
- 数据库不可用时列表接口返回 **503**(fail-closed)。

约定(对齐 ``tests/integration/test_postgres_checkpointer.py``)
--------------------------------------------------------
- ``TEST_POSTGRES_URI`` / ``POSTGRES_URI`` 环境变量优先,否则用 ``config.POSTGRES_URI``;
- 显式给了 URI 却连不上 ⇒ **fail**(不静默 skip —— 杜绝"假绿");
- Windows 上默认事件循环是 ProactorEventLoop,psycopg 异步不可用 ⇒ 显式 SelectorEventLoop。
"""

from __future__ import annotations

import asyncio
import os
import selectors
import socket
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from db.session_repository import SessionRepository


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OPENSTORYLINE_DIR = REPO_ROOT / "openstoryline"

# ``open_storyline.nodes.node_manager`` 用的是 ``src.open_storyline...`` 这种写法
# ⇒ 需要把 ``openstoryline/`` 放进 sys.path(生产环境靠 cwd=openstoryline/ 达成);
# ``open_storyline`` 包本身在 ``openstoryline/src`` 下。
# 一律放**末尾**:仓库根的 ``config.py`` 必须优先于 ``openstoryline/config.py``。
for _p in (str(OPENSTORYLINE_DIR / "src"), str(OPENSTORYLINE_DIR)):
    if _p not in sys.path:
        sys.path.append(_p)


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

#: 隔离测试库的建/删逻辑统一放在 ``tests/integration/conftest.py``
#: (``postgres_test_db`` 工厂)—— 这些用例会建会话、改标题、软删除;
#: 直接打在生产库 ``video_workflow`` 上会留下垃圾会话(标题形如「分页xxx」),
#: 也会碰到用户自己的会话 ⇒ 必须用独立库。


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
            "跳过会话 API 集成测试;显式设置 TEST_POSTGRES_URI 可让连不上直接 fail"
        ),
    ),
]


@pytest.fixture(scope="session")
def _sessions_test_db(postgres_test_db) -> str:
    """建一个**独立的测试库**(带唯一后缀),跑完删掉。

    这些用例会建会话 / 改标题 / 软删除;打在生产库 ``video_workflow`` 上会
    留下垃圾会话,也会碰到用户自己的会话 ⇒ 必须用独立库。

    连不上 ⇒ skip(明确原因);显式指定了 URI 却连不上 ⇒ fail(不静默 skip)。
    """
    if not _reachable(TEST_URI):
        if _URI_IS_EXPLICIT:
            pytest.fail(
                f"显式指定了 Postgres URI,但目标不可达: {TEST_URI} — "
                "Postgres 集成测试不允许静默 skip"
            )
        pytest.skip(f"Postgres not reachable at {TEST_URI}")

    target = os.environ.get("TEST_POSTGRES_URI")
    if target:
        # 调用方指定了具体库 ⇒ 就用它(不再另建库),但仍要确保表在。
        if not postgres_test_db.ensure(target):
            pytest.fail(f"无法在 {target} 上准备 chat_sessions 表")
        yield target
        return

    uri = postgres_test_db.create(prefix="video_workflow_sessions_test")
    try:
        yield uri
    finally:
        postgres_test_db.drop(uri)


@pytest.fixture()
def postgres_uri(_sessions_test_db: str) -> str:
    """本文件所有用例用的 Postgres URI(= 独立测试库)。"""
    return _sessions_test_db


def _run_async(coro):
    """在 SelectorEventLoop 上跑协程。

    Windows 默认事件循环是 ProactorEventLoop,psycopg 异步不可用
    (``Psycopg cannot use the 'ProactorEventLoop' to run in async mode``),
    必须显式换 SelectorEventLoop —— 与 ``test_postgres_checkpointer.py`` 同一套写法。
    """
    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _chdir_openstoryline(monkeypatch):
    """prompt 模板走相对路径(``prompts/tasks/...``)⇒ cwd 必须是 ``openstoryline/``。"""
    monkeypatch.chdir(OPENSTORYLINE_DIR)


@pytest.fixture(autouse=True)
def _relax_list_sessions_rate_limit(monkeypatch):
    """把 ``GET /api/sessions`` 的限流桶放宽。

    限流规则本身由单测覆盖(``tests/unit/test_session_store_repository.py``);
    这里只是别让一串集成用例把 ``list_sessions`` 桶(默认 burst=10)打爆。
    """
    import openstoryline.agent_fastapi as web

    monkeypatch.setattr(web, "HTTP_LIST_SESSIONS_BURST", 100000)
    monkeypatch.setattr(web, "HTTP_LIST_SESSIONS_RPM", 100000)


@pytest.fixture()
def cfg(tmp_path):
    """一份 Settings:所有目录都落在 ``tmp_path``(别污染真实工程目录)。"""
    from open_storyline.config import MCodeConfig, ProjectConfig, load_settings

    base = load_settings(OPENSTORYLINE_DIR / "config.toml")
    project = ProjectConfig(
        media_dir=tmp_path / "media",
        bgm_dir=tmp_path / "bgm",
        outputs_dir=tmp_path / "outputs",
    )
    return base.model_copy(update={"project": project, "mcode": MCodeConfig(cwd="")})


@pytest.fixture()
def api(postgres_uri, cfg):
    """``(app, repo)`` —— 仓储指向测试库的 app。"""
    from openstoryline.agent_fastapi import app, SessionStore

    repo = SessionRepository(uri=postgres_uri)
    app.state.session_repo = repo
    app.state.sessions = SessionStore(cfg, repo=repo)
    return app, repo


@pytest.fixture()
def client(api):
    """指向测试库的 HTTP 客户端。

    ``TestClient`` 进出会跑 lifespan(lifespan 会重建 ``app.state``)⇒
    出来之后要把 state 覆盖回测试库。
    """
    from fastapi.testclient import TestClient

    from openstoryline.agent_fastapi import SessionStore

    app, repo = api
    with TestClient(app) as c:
        app.state.session_repo = repo
        app.state.sessions = SessionStore(app.state.cfg, repo=repo)
        yield c


class _BrokenRepo:
    """一个"数据库挂了"的仓储(所有读写都抛异常)。"""

    def get(self, *args: Any, **kwargs: Any):
        raise RuntimeError("db down")

    def list(self, *args: Any, **kwargs: Any):
        raise RuntimeError("db down")

    def create(self, *args: Any, **kwargs: Any):
        raise RuntimeError("db down")

    def create_if_absent(self, *args: Any, **kwargs: Any):
        raise RuntimeError("db down")

    def update_state(self, *args: Any, **kwargs: Any):
        raise RuntimeError("db down")

    def rename(self, *args: Any, **kwargs: Any):
        raise RuntimeError("db down")

    def soft_delete(self, *args: Any, **kwargs: Any):
        raise RuntimeError("db down")


# ---------------------------------------------------------------------------
# 用例 3:新建会话 ⇒ 另一条连接能读到完整状态
# ---------------------------------------------------------------------------
def test_new_session_visible_from_another_connection(api, postgres_uri):
    """用例 3:另一个应用实例(另一条连接)能读到完整状态。"""
    app, _repo = api
    store = app.state.sessions

    sess = _run_async(store.create())
    sess.history.append({"role": "user", "message": "跨实例可见"})
    _run_async(store.save_session_state(sess))

    other = SessionRepository(uri=postgres_uri)
    record = other.get(sess.session_id)
    assert record is not None
    assert record.state_json["session_id"] == sess.session_id
    assert record.state_json["history"][-1]["message"] == "跨实例可见"


# ---------------------------------------------------------------------------
# 用例 4:聊完一轮后,会话列表里仍然能看到它
# ---------------------------------------------------------------------------
def test_list_shows_session_after_conversation_round(client):
    """用例 4:一轮对话之后,列表里仍然能看到这条会话。"""
    created = client.post("/api/sessions")
    assert created.status_code == 200
    sid = created.json()["session_id"]

    # 模拟"聊了一轮":往会话里塞一条消息并存库
    store = client.app.state.sessions
    sess = store._sessions[sid]
    sess.history.append({"role": "user", "message": "第一轮提问"})
    _run_async(store.save_session_state(sess))

    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(i["session_id"] == sid for i in items), "聊完一轮后列表里找不到它"


# ---------------------------------------------------------------------------
# 用例 5:第二个客户端(换个"浏览器")看到同一份列表
# ---------------------------------------------------------------------------
def test_second_client_sees_the_same_list(api, client):
    """用例 5:另一个客户端访问同一个服务,看到同一份列表。"""
    from fastapi.testclient import TestClient

    from openstoryline.agent_fastapi import SessionStore

    app, repo = api

    sid = client.post("/api/sessions").json()["session_id"]

    # 第二个"浏览器":另一个客户端连同一个 app(同一个 service)
    with TestClient(app) as c2:
        app.state.session_repo = repo
        app.state.sessions = SessionStore(app.state.cfg, repo=repo)
        resp = c2.get("/api/sessions")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(i["session_id"] == sid for i in items), "第二个客户端没看到这条会话"


# ---------------------------------------------------------------------------
# 用例 6:改名 ⇒ 换一个新的 SessionStore 仍在;updated_at / 排序保住
# ---------------------------------------------------------------------------
def test_rename_survives_fresh_session_store(api, postgres_uri, cfg):
    """用例 6:改名后换一个新的 ``SessionStore``(= 重启服务),标题 / updated_at 都在。"""
    from openstoryline.agent_fastapi import SessionStore

    app, repo = api
    store = app.state.sessions

    sess = _run_async(store.create())
    sid = sess.session_id
    before = repo.get(sid)
    assert before is not None

    repo.rename(sid, "改过的标题", title_source="manual")
    renamed = repo.get(sid)
    assert renamed is not None
    assert renamed.title == "改过的标题"
    assert renamed.title_source == "manual"
    assert renamed.updated_at >= before.updated_at

    # 换一个新的 SessionStore(≈ 重启服务)
    fresh = SessionStore(cfg, repo=SessionRepository(uri=postgres_uri))
    restored = _run_async(fresh.get(sid))
    assert restored is not None

    after = SessionRepository(uri=postgres_uri).get(sid)
    assert after is not None
    assert after.title == "改过的标题"
    assert after.title_source == "manual"
    # 读一次**不该**把 updated_at 刷新掉(否则列表排序会被"只是打开一下"打乱)
    assert after.updated_at == renamed.updated_at


def test_rename_via_api_and_list_shows_new_title(client):
    """改名后,列表里显示的就是新标题。"""
    sid = client.post("/api/sessions").json()["session_id"]

    resp = client.patch(f"/api/sessions/{sid}", json={"title": "  新标题  "})
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "session_id": sid,
        "title": "新标题",
        "title_source": "manual",
    }

    items = client.get("/api/sessions").json()["items"]
    hit = next(i for i in items if i["session_id"] == sid)
    assert hit["title"] == "新标题"
    assert hit["title_source"] == "manual"


def test_patch_title_source_auto_is_accepted(client):
    """前端**自动推导**的标题回写用 ``title_source="auto"``(方案 §4.6)。

    默认(不传)按 ``"manual"`` 处理 —— 手动标题优先是安全默认。
    """
    sid = client.post("/api/sessions").json()["session_id"]

    resp = client.patch(
        f"/api/sessions/{sid}", json={"title": "自动标题", "title_source": "auto"}
    )
    assert resp.status_code == 200
    assert resp.json()["title_source"] == "auto"

    items = client.get("/api/sessions").json()["items"]
    hit = next(i for i in items if i["session_id"] == sid)
    assert hit["title_source"] == "auto"


def test_patch_title_source_invalid_is_422(client):
    """``title_source`` 只接受 ``auto`` / ``manual``,其它值 422。"""
    sid = client.post("/api/sessions").json()["session_id"]
    resp = client.patch(
        f"/api/sessions/{sid}", json={"title": "x", "title_source": "whatever"}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 用例 7:软删除 ⇒ 默认列表里没有;还能 restore
# ---------------------------------------------------------------------------
def test_soft_delete_hides_from_list_and_can_be_restored(client, postgres_uri):
    """用例 7:软删除后默认列表里没有它,但 ``repo.restore()`` 能救回来。"""
    sid = client.post("/api/sessions").json()["session_id"]

    resp = client.delete(f"/api/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "session_id": sid}

    items = client.get("/api/sessions").json()["items"]
    assert all(i["session_id"] != sid for i in items), "软删除后还在默认列表里"

    repo = SessionRepository(uri=postgres_uri)
    assert repo.get(sid) is None
    assert repo.restore(sid) is True
    assert repo.get(sid) is not None

    items = client.get("/api/sessions").json()["items"]
    assert any(i["session_id"] == sid for i in items), "恢复后应该重新出现在列表里"


def test_soft_delete_twice_returns_404(client):
    sid = client.post("/api/sessions").json()["session_id"]
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert client.delete(f"/api/sessions/{sid}").status_code == 404


def test_delete_unknown_session_returns_404(client):
    assert client.delete(f"/api/sessions/{uuid.uuid4().hex}").status_code == 404


# ---------------------------------------------------------------------------
# 契约:响应形状
# ---------------------------------------------------------------------------
def test_list_response_shape_exact(client):
    """列表响应的 key 与类型必须与契约完全一致。"""
    client.post("/api/sessions")

    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    body = resp.json()

    assert set(body.keys()) == {"items", "next_cursor"}
    assert isinstance(body["next_cursor"], (str, type(None)))

    assert body["items"], "刚建了会话,列表不该是空的"
    for item in body["items"]:
        assert set(item.keys()) == {
            "session_id",
            "title",
            "title_source",
            "created_at",
            "updated_at",
            "message_count",
            "last_message_preview",
        }
        assert isinstance(item["session_id"], str)
        assert isinstance(item["title"], str)
        assert isinstance(item["title_source"], str)
        assert isinstance(item["created_at"], str)
        assert isinstance(item["updated_at"], str)
        assert isinstance(item["message_count"], int)
        assert isinstance(item["last_message_preview"], str)
        assert item["created_at"].endswith("+00:00")
        assert item["updated_at"].endswith("+00:00")


def test_list_never_returns_state_json(client):
    """``state_json`` **绝不**出现在响应里。"""
    sid = client.post("/api/sessions").json()["session_id"]

    raw = client.get("/api/sessions").text
    assert "state_json" not in raw
    assert "lc_messages_serialized" not in raw

    item = next(i for i in client.get("/api/sessions").json()["items"] if i["session_id"] == sid)
    assert "state_json" not in item


def test_list_limit_is_clamped(client):
    """``limit`` 被夹到 1..200。"""
    assert client.get("/api/sessions?limit=0").status_code == 200
    assert client.get("/api/sessions?limit=100000").status_code == 200
    assert client.get("/api/sessions?limit=abc").status_code == 422


def test_list_cursor_pagination(client):
    """游标分页:第一页 + 下一页正好覆盖全部,不重不漏。"""
    created = []
    for _ in range(3):
        created.append(client.post("/api/sessions").json()["session_id"])

    first = client.get("/api/sessions?limit=2").json()
    assert len(first["items"]) == 2
    assert first["next_cursor"], "还有下一页 ⇒ next_cursor 不能是 null"

    second = client.get(
        "/api/sessions",
        params={"limit": 2, "cursor": first["next_cursor"]},
    ).json()

    seen = [i["session_id"] for i in first["items"]] + [i["session_id"] for i in second["items"]]
    assert len(seen) == len(set(seen)), "游标分页出现重复"
    for sid in created:
        assert sid in seen, "游标分页漏了会话"


def test_list_invalid_cursor_returns_422(client):
    assert client.get("/api/sessions?cursor=not-a-cursor").status_code == 422


def test_list_title_prefix_filter(client):
    sid = client.post("/api/sessions").json()["session_id"]
    client.patch(f"/api/sessions/{sid}", json={"title": "独特前缀ABC"})

    items = client.get("/api/sessions", params={"title_prefix": "独特前缀"}).json()["items"]
    assert any(i["session_id"] == sid for i in items)

    items = client.get("/api/sessions", params={"title_prefix": "独特前缀XYZ"}).json()["items"]
    assert all(i["session_id"] != sid for i in items)


def test_list_include_deleted_flag(client, postgres_uri):
    sid = client.post("/api/sessions").json()["session_id"]
    client.delete(f"/api/sessions/{sid}")

    default_items = client.get("/api/sessions").json()["items"]
    assert all(i["session_id"] != sid for i in default_items)

    with_deleted = client.get("/api/sessions", params={"include_deleted": "true"}).json()["items"]
    assert any(i["session_id"] == sid for i in with_deleted)


# ---------------------------------------------------------------------------
# 契约:错误码
# ---------------------------------------------------------------------------
def test_patch_rejects_bad_titles(client):
    sid = client.post("/api/sessions").json()["session_id"]
    url = f"/api/sessions/{sid}"

    assert client.patch(url, json={"title": ""}).status_code == 422
    assert client.patch(url, json={"title": "   "}).status_code == 422
    assert client.patch(url, json={"title": 123}).status_code == 422
    assert client.patch(url, json={"title": "x" * 201}).status_code == 422
    assert client.patch(url, json={}).status_code == 200  # title 可选


def test_patch_unknown_session_returns_404(client):
    resp = client.patch(f"/api/sessions/{uuid.uuid4().hex}", json={"title": "x"})
    assert resp.status_code == 404


def test_list_returns_503_when_db_down(client, api):
    """数据库不可用 ⇒ 503(fail-closed),不是 500 / 空列表。"""
    app, repo = api
    app.state.session_repo = _BrokenRepo()
    try:
        resp = client.get("/api/sessions")
        assert resp.status_code == 503
    finally:
        app.state.session_repo = repo


def test_save_session_state_raises_on_db_failure(cfg):
    """``save_session_state`` 在数据库故障时**抛异常**(不再静默吞)。"""
    from openstoryline.agent_fastapi import (
        ChatSession,
        SessionStateUnavailableError,
        SessionStore,
    )

    store = SessionStore(cfg, repo=_BrokenRepo())
    sess = ChatSession(uuid.uuid4().hex, cfg, repo=_BrokenRepo())

    with pytest.raises(SessionStateUnavailableError):
        _run_async(store.save_session_state(sess))


def test_create_session_returns_503_when_db_down(client, api):
    """数据库不可用时建会话 ⇒ 503(应用级异常处理器兜住)。"""
    app, repo = api
    app.state.sessions._repo = _BrokenRepo()  # type: ignore[attr-defined]
    try:
        resp = client.post("/api/sessions")
        assert resp.status_code == 503
    finally:
        app.state.sessions._repo = repo  # type: ignore[attr-defined]


def test_cursor_tolerates_unencoded_plus(client):
    """客户端没给 "+00:00" 做 URL 编码时(``+`` 变空格),游标也能解析。"""
    client.post("/api/sessions")
    client.post("/api/sessions")
    first = client.get("/api/sessions?limit=1").json()
    cursor = first["next_cursor"]
    assert cursor is not None
    assert "+" in cursor

    # 模拟"没编码"的客户端:直接拼进 query string
    raw = f"/api/sessions?limit=1&cursor={cursor}"
    resp = client.get(raw)
    assert resp.status_code == 200
    assert "items" in resp.json()
