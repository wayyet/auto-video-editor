"""agent_gate 测试的共享 fixtures。

PostgreSQL 统一改造后的数据库约定(方案 §4.7)
--------------------------------------------
agent_gate 的 worker 现在**只**用 Postgres checkpointer(唯一持久化后端,
SQLite 分支已删除)。跨进程场景(崩溃后继续 / 等待人工放行后恢复)本来就需要
真持久化,所以测试必须有一个真的 Postgres。

为了不污染工作库里真实的 checkpoint,这里用**独立测试库**:

- ``AGENT_GATE_POSTGRES_URI`` —— agent_gate 专用(优先级高于仓库的
  ``POSTGRES_URI``),指向 ``video_workflow_agent_gate_test``;
  这样不会改动仓库全局的 ``POSTGRES_URI``,也不会影响 ``tests/`` 下的用例。
- 测试库不存在就建;连不上 ⇒ 整个 agent_gate 测试套件 **skip**(明确说明原因),
  而不是静默退化 —— 免得"假绿"。
- 每个用例之间 **TRUNCATE** 检查点表 ⇒ 用例之间完全隔离,
  不用改用例里的 thread_id。
"""

import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "fixtures"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# 子进程(worker / mcp 服务)也要能 import 到这两个目录
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(ROOT), str(ROOT / "fixtures")] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
)
os.environ.setdefault("AGENT_GATE_HEARTBEAT_S", "0.3")
os.environ.setdefault("AGENT_GATE_STALE_AFTER_S", "1.5")

# agent_gate 的 worker 默认走 Postgres(方案 §4.7);不再有 SQLite 兜底。
os.environ.setdefault("AGENT_GATE_CKPT_BACKEND", "postgres")

# ---- 独立测试库 ----------------------------------------------------------
# ⚠️ 库名带**每次运行**的唯一后缀:并发跑多个 pytest 会话时,它们不会互相
# TRUNCATE 对方的检查点表(之前用固定库名时,并发跑会偶发串数据)。
# 会话结束时把库删掉(见 ``_drop_test_db``)。
_SESSION_TAG = uuid.uuid4().hex[:8]
TEST_DB_NAME = f"video_workflow_agent_gate_{_SESSION_TAG}"
_ADMIN_URI = os.environ.get(
    "AGENT_GATE_TEST_ADMIN_URI",
    "postgresql://postgres:devpass@localhost:5432/postgres",
)
_TEST_URI = os.environ.get(
    "AGENT_GATE_TEST_POSTGRES_URI",
    f"postgresql://postgres:devpass@localhost:5432/{TEST_DB_NAME}",
)

import pytest  # noqa: E402


def _ensure_test_db() -> str | None:
    """确保测试库存在,返回测试库 URI;失败返回 ``None``。

    只做 ``CREATE DATABASE IF NOT EXISTS`` 语义(先查再建),不会删任何库。
    """
    try:
        import psycopg
    except ImportError:  # pragma: no cover - 依赖缺失
        return None
    try:
        with psycopg.connect(_ADMIN_URI, autocommit=True, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,))
                if cur.fetchone() is None:
                    cur.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    except Exception:  # noqa: BLE001 — 连不上就返回 None,让上层 skip
        return None
    return _TEST_URI


def _drop_test_db() -> None:
    """删掉本次会话建的测试库(只删自己那个带唯一后缀的)。"""
    try:
        import psycopg
    except ImportError:  # pragma: no cover - 依赖缺失
        return
    try:
        with psycopg.connect(_ADMIN_URI, autocommit=True, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = %s AND pid <> pg_backend_pid()",
                    (TEST_DB_NAME,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
    except Exception:  # noqa: BLE001 — 清理失败不影响测试结论
        pass


@pytest.fixture(scope="session", autouse=True)
def _agent_gate_postgres():
    """准备 agent_gate 的独立测试库。

    连不上 ⇒ 整个 agent_gate 套件 skip(明确原因,不静默退化)。
    """
    uri = _ensure_test_db()
    if uri is None:
        pytest.skip(
            f"Postgres 不可达(测试库 {TEST_DB_NAME})—— agent_gate 的 checkpointer "
            "已统一为 Postgres(方案 §4.7),没有本地文件兜底;请先启动 Postgres 容器。"
        )
    os.environ["AGENT_GATE_POSTGRES_URI"] = uri
    # 首次建表(checkpoints / checkpoint_blobs / checkpoint_writes)
    try:
        from agent_gate.runner import close_checkpointer, make_checkpointer

        saver = make_checkpointer("agent-gate-setup")
        close_checkpointer(saver)
    except Exception as e:  # noqa: BLE001 — 建表失败同样 skip
        pytest.skip(f"Postgres 测试库初始化失败: {type(e).__name__}: {e}")
    yield uri
    os.environ.pop("AGENT_GATE_POSTGRES_URI", None)
    _drop_test_db()


@pytest.fixture(autouse=True)
def _clean_checkpoints(_agent_gate_postgres):
    """每个用例前后清空测试库的检查点表 ⇒ 用例之间完全隔离。"""
    import psycopg

    def _truncate() -> None:
        with psycopg.connect(_agent_gate_postgres, autocommit=True, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    cur.execute(f"TRUNCATE TABLE {table}")

    _truncate()
    yield
    _truncate()


@pytest.fixture
def mock(tmp_path):
    """用法:srv = mock({...脚本...});测试结束自动关掉。"""
    from support import MockServer

    made = []

    def make(script: dict):
        srv = MockServer(tmp_path, script)
        made.append(srv)
        return srv

    yield make
    for m in made:
        m.close()
