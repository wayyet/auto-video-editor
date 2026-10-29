"""``tests/integration/`` 的共享 fixture。

重点:隔离测试库
----------------
PostgreSQL 集成测试**不能**直接打生产库 ``video_workflow``。

原因:这些用例会真的建会话 / 改标题 / 软删除 / 写 checkpoint。
打在生产库上 = 往用户的库里灌垃圾数据(标题形如「分页xxx」「独特前缀ABC」),
而且测试之间的数据会互相干扰(比如"标题前缀过滤"会翻到别的用例留下的行)。

约定:
- 每个测试文件/会话用**带唯一后缀**的独立库 ⇒ 用例之间彻底隔离;
- 库名带每次运行的随机后缀 ⇒ 并发跑多个 pytest 会话不会互相 TRUNCATE;
- 会话结束时自动删库 ⇒ 生产库一个字节都不动。

库连不上时 **fail**(不静默 skip)—— 避免"假绿"(方案 §7.2)。
"""

from __future__ import annotations

import os
import socket
import uuid
from typing import Callable
from urllib.parse import urlparse, urlunparse

import pytest


def default_postgres_uri() -> str:
    """默认 URI = ``config.POSTGRES_URI``(库名 ``video_workflow``)。"""
    try:
        from config import POSTGRES_URI

        return POSTGRES_URI
    except Exception:  # pragma: no cover - config 不可导入时的兜底
        return "postgresql://postgres:devpass@localhost:5432/video_workflow"


def base_postgres_uri() -> str:
    """"用户显式指定的目标库" —— 只用来取 host/port/口令,不用它的库名。"""
    return (
        os.environ.get("TEST_POSTGRES_URI")
        or os.environ.get("POSTGRES_URI")
        or default_postgres_uri()
    )


def uri_is_explicit() -> bool:
    """用户是否显式给了 URI(显式 ⇒ 连不上必须 fail,不许 skip)。"""
    return bool(os.environ.get("TEST_POSTGRES_URI") or os.environ.get("POSTGRES_URI"))


def postgres_reachable(uri: str, timeout: float = 1.5) -> bool:
    """TCP 探活(只解析 host:port,不建真连接)。"""
    try:
        parsed = urlparse(uri)
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def dbname_from_uri(uri: str) -> str:
    """取 URI 里的库名。"""
    parsed = urlparse(uri)
    return (parsed.path or "").lstrip("/")


def uri_with_dbname(uri: str, dbname: str) -> str:
    """换掉 URI 里的库名(其余部分原样保留)。"""
    parsed = urlparse(uri)
    return urlunparse(parsed._replace(path=f"/{dbname}"))


def admin_uri_for(uri: str) -> str:
    """从目标 URI 推出管理 URI(把库名换成 ``postgres``)。"""
    return uri_with_dbname(uri, "postgres")


def create_test_db(uri: str) -> bool:
    """建库 + 跑应用层迁移(幂等)。成功返回 True。"""
    import psycopg

    admin = admin_uri_for(uri)
    name = dbname_from_uri(uri)
    if not name:
        return False
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                if cur.fetchone() is None:
                    cur.execute(f'CREATE DATABASE "{name}"')
    except Exception:
        return False
    try:
        from db.migrations import apply_migrations_to_db

        apply_migrations_to_db(uri=uri)
    except Exception:
        return False
    return True


def drop_test_db(uri: str) -> None:
    """删掉测试库(先踢掉残留连接)。失败不影响测试结论。"""
    import psycopg

    admin = admin_uri_for(uri)
    name = dbname_from_uri(uri)
    if not name:
        return
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    except Exception:
        pass


class PostgresTestDbFactory:
    """隔离测试库的工厂。

    - ``create()``  → 建一个带唯一后缀的库,返回 URI;
    - ``ensure(uri)`` → 确保某个已指定的库存在且表已建(不删);
    - ``drop(uri)``  → 删库。
    """

    def __init__(self) -> None:
        self.created: list[str] = []

    def create(self, prefix: str = "video_workflow_test") -> str:
        """建一个**唯一命名**的隔离库,返回 URI。"""
        base = base_postgres_uri()
        if not postgres_reachable(base):
            pytest.fail(
                f"Postgres 不可达: {base} —— PostgreSQL 集成测试不允许静默 skip"
                "(方案 §7.2);请先启动 Postgres 容器。"
            )
        name = f"{prefix}_{uuid.uuid4().hex[:8]}"
        uri = uri_with_dbname(base, name)
        if not create_test_db(uri):
            pytest.fail(f"无法创建隔离测试库 {name}(权限不足?)")
        self.created.append(uri)
        return uri

    def ensure(self, uri: str) -> bool:
        """确保指定库存在且应用层迁移已跑过。"""
        return create_test_db(uri)

    def drop(self, uri: str) -> None:
        """删库。"""
        drop_test_db(uri)

    def cleanup(self) -> None:
        """删掉本工厂建过的所有库。"""
        for uri in self.created:
            drop_test_db(uri)
        self.created.clear()


@pytest.fixture(scope="session")
def postgres_test_db() -> PostgresTestDbFactory:
    """隔离测试库工厂(会话级)。"""
    factory = PostgresTestDbFactory()
    yield factory
    factory.cleanup()


@pytest.fixture(scope="session")
def isolated_postgres_db(postgres_test_db: PostgresTestDbFactory) -> Callable[[], str]:
    """工厂:建一个**隔离**的测试库,返回它的 URI;会话结束时自动删掉。

    用法::

        def test_x(isolated_postgres_db):
            uri = isolated_postgres_db()
            repo = SessionRepository(uri=uri)

    连不上 Postgres ⇒ 直接 **fail**(不静默 skip,避免"假绿")。
    """
    return postgres_test_db.create
