"""按目标 URI 对齐本地 Postgres 容器的角色与数据库。

背景
----
``start_production.ps1 -PostgresUri`` 支持**任意** URI。当目标 URI 指向
本地托管容器(``localhost:5432``)但用了别的角色 / 库名时,容器里默认只有
管理员身份(``postgres``)和默认库(``langgraph``),直接连会失败。

本脚本用**管理员凭据**做增量对齐(只 CREATE / GRANT,不 DROP / 不删库),
然后用目标 URI 回验可连。

用法::

    python scripts/align_postgres.py --target-uri <uri> --admin-uri <uri>

流程
----
1. 用 ``--admin-uri`` 连到维护库 ``postgres``
2. 目标角色不存在 → ``CREATE ROLE "<user>" LOGIN PASSWORD '<pw>'``
   目标角色存在但密码不同且 user == 管理员 user
                  → ``ALTER ROLE "<user>" WITH PASSWORD '<pw>'``
                    (打印醒目告警:默认 URI 将不再可用)
3. 目标库不存在 → ``CREATE DATABASE "<db>" OWNER "<user>"``
4. ``GRANT ALL ON DATABASE "<db>" TO "<user>"``
   ``GRANT ALL ON SCHEMA public TO "<user>"``
   (PG16 的 ``public`` schema 默认对非 owner 不可写,必须显式授权)
5. 用 ``--target-uri`` 回验可连;失败 → 非零退出 + 可操作报错
6. 全程不打印密码
"""

from __future__ import annotations

import argparse
import asyncio
import selectors
import sys
from urllib.parse import urlparse

import psycopg
from psycopg import sql

# Windows 默认 stdout 是 GBK，遇到中文 / 符号会报错；强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


class AlignError(RuntimeError):
    """对齐失败(可操作的错误信息)。"""


def _mask_uri(uri: str) -> str:
    """把 URI 里的密码替换成 ``***``,用于任何输出。"""
    try:
        parsed = urlparse(uri)
    except ValueError:
        return "<无法解析的 URI>"
    if not parsed.password:
        return uri
    netloc = parsed.netloc
    if parsed.password in netloc:
        netloc = netloc.replace(parsed.password, "***")
    return parsed._replace(netloc=netloc).geturl()


def _split_uri(uri: str) -> dict:
    """从 URI 里取出 user / password / host / port / dbname。"""
    parsed = urlparse(uri)
    if not parsed.hostname:
        raise AlignError(f"URI 缺少 host:{_mask_uri(uri)}")
    dbname = (parsed.path or "").lstrip("/")
    return {
        "user": parsed.username or "",
        "password": parsed.password or "",
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "dbname": dbname,
    }


def _admin_uri_with_db(uri: str, dbname: str) -> str:
    """把 URI 的库名换掉,其余部分保持不变。"""
    parsed = urlparse(uri)
    return parsed._replace(path=f"/{dbname}").geturl()


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
async def _role_exists(cur, role: str) -> bool:
    await cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    return (await cur.fetchone()) is not None


async def _ensure_role(cur, *, user: str, password: str, admin_user: str) -> str:
    """确保目标角色存在且能登录。返回 'created' / 'altered' / 'ok'。"""
    if not user:
        raise AlignError("目标 URI 没有用户名,无法对齐角色")

    if not await _role_exists(cur, user):
        await cur.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(user),
                sql.Literal(password),
            )
        )
        print(f"  ✓ 已创建角色 {user!r}")
        return "created"

    # 角色已存在:只有"目标角色 == 管理员角色"时才允许改密码
    # (改别的角色的密码需要 superuser 权限,且会破坏别人正在用的凭据)
    if user != admin_user:
        print(f"  · 角色 {user!r} 已存在(非管理员角色,不改动其密码)")
        return "ok"

    await cur.execute(
        sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
            sql.Identifier(user),
            sql.Literal(password),
        )
    )
    print(f"  ✓ 已更新管理员角色 {user!r} 的密码")
    print(
        "  ! 警告:管理员角色密码已改,默认 URI "
        "postgresql://postgres:devpass@localhost:5432/video_workflow "
        "将不再可用。",
        file=sys.stderr,
    )
    return "altered"


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
async def _database_exists(cur, dbname: str) -> bool:
    await cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    return (await cur.fetchone()) is not None


async def _ensure_database(cur, *, dbname: str, owner: str) -> str:
    """确保目标库存在。返回 'created' / 'ok'。"""
    if not dbname:
        raise AlignError("目标 URI 没有库名,无法对齐数据库")
    if await _database_exists(cur, dbname):
        print(f"  · 数据库 {dbname!r} 已存在")
        return "ok"
    await cur.execute(
        sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(dbname),
            sql.Identifier(owner),
        )
    )
    print(f"  ✓ 已创建数据库 {dbname!r}(owner={owner!r})")
    return "created"


async def _grant(cur, *, dbname: str, user: str) -> None:
    """库级授权(在维护库执行)。"""
    await cur.execute(
        sql.SQL("GRANT ALL ON DATABASE {} TO {}").format(
            sql.Identifier(dbname),
            sql.Identifier(user),
        )
    )
    print(f"  ✓ 已授权 {user!r} 对数据库 {dbname!r}")


async def _grant_public_schema(admin_uri: str, *, dbname: str, user: str) -> None:
    """在**目标库**里给 public schema 授权(用管理员身份连目标库)。

    PG16 起 public schema 默认对非 owner 不可写,必须显式授权。
    两个要点:
    1. 必须在**目标库**里做 —— 在维护库(postgres)里授权会留下一条跨库依赖,
       导致之后 ``DROP ROLE`` 被拒。
    2. 用**管理员**凭据连目标库 —— 目标角色的密码此刻可能还没对上
       (已存在的非管理员角色我们不会改它的密码)。
    """
    target_db_uri = _admin_uri_with_db(admin_uri, dbname)
    async with await psycopg.AsyncConnection.connect(target_db_uri) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(
                    sql.Identifier(user)
                )
            )
    print(f"  ✓ 已授权 {user!r} 对目标库 {dbname!r} 的 public schema")


# ---------------------------------------------------------------------------
# 回验
# ---------------------------------------------------------------------------
async def _verify_target(target_uri: str) -> str:
    """用目标 URI 连一次,返回当前库名。"""
    async with await psycopg.AsyncConnection.connect(target_uri) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT current_database();")
            row = await cur.fetchone()
            if row is None:
                raise AlignError("目标 URI 连通但没有返回 current_database()")
            dbname = row[0]
            if isinstance(dbname, (bytes, bytearray)):
                dbname = dbname.decode("utf-8", errors="replace")
            return dbname


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def align(*, target_uri: str, admin_uri: str) -> int:
    """执行对齐。返回 0 表示成功。"""
    target = _split_uri(target_uri)
    admin = _split_uri(admin_uri)
    admin_user = admin["user"] or "postgres"

    # 维护库:用管理员身份连 postgres 库
    maintenance_uri = _admin_uri_with_db(admin_uri, "postgres")

    print(f"==> 目标 URI : {_mask_uri(target_uri)}")
    print(f"==> 管理员 URI: {_mask_uri(admin_uri)}")

    # 用管理员连接进维护库(autocommit:CREATE DATABASE 不能在事务块里跑)
    async with await psycopg.AsyncConnection.connect(
        maintenance_uri, autocommit=True
    ) as conn:
        async with conn.cursor() as cur:
            print("==> 对齐角色")
            await _ensure_role(
                cur,
                user=target["user"],
                password=target["password"],
                admin_user=admin_user,
            )
            print("==> 对齐数据库")
            await _ensure_database(
                cur, dbname=target["dbname"], owner=target["user"]
            )
            print("==> 授权")
            await _grant(cur, dbname=target["dbname"], user=target["user"])

    # public schema 授权必须在目标库里做(在维护库里做会留下跨库依赖)
    await _grant_public_schema(
        admin_uri, dbname=target["dbname"], user=target["user"]
    )

    # 回验:必须能用目标 URI 连上
    print("==> 回验目标 URI")
    actual = await _verify_target(target_uri)
    if actual != target["dbname"]:
        raise AlignError(
            f"回验失败:目标 URI 连到的是 {actual!r},预期 {target['dbname']!r}"
        )
    print(f"  ✓ 目标 URI 可连,current_database() = {actual!r}")
    return 0


def _run(coro):
    """用 SelectorEventLoop 跑协程;psycopg 异步在 Windows 上需要这个 loop。"""
    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="align_postgres",
        description="按目标 URI 对齐 Postgres 角色与数据库(只增量,不删除)",
    )
    parser.add_argument("--target-uri", required=True, help="目标连接串(角色/库按此对齐)")
    parser.add_argument("--admin-uri", required=True, help="管理员连接串(本地容器用)")
    args = parser.parse_args()

    try:
        rc = _run(align(target_uri=args.target_uri, admin_uri=args.admin_uri))
    except AlignError as exc:
        print(f"✗ 对齐失败:{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — 顶层兜底,任何异常都要给可操作报错
        print(f"✗ 对齐失败(异常):{exc}", file=sys.stderr)
        return 1
    print("✓ 角色/数据库对齐完成")
    return rc


if __name__ == "__main__":
    sys.exit(main())
