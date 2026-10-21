"""连通性自检：版本、当前数据库及 LangGraph checkpoint 表。

期望库名不再硬编码 —— 按连接串 URI 的 dbname 动态比对
(URI 未指定库名时才回落 ``langgraph``),以支持任意 ``-PostgresUri``。

用法::

    # 严格模式(手工核验):缺 checkpoint 表 → 失败
    python scripts/verify_postgres.py

    # 宽松模式(preflight Step 5 调用):缺 checkpoint 表 → 仅告警
    python scripts/verify_postgres.py --allow-missing-tables
"""

from __future__ import annotations

import asyncio
import os
import selectors
import sys
from urllib.parse import urlparse

import psycopg


EXPECTED_TABLES = {
    "checkpoints",
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoint_migrations",
}
# 仅当 URI 里没有 dbname 时才回落到这个库名
DEFAULT_DATABASE = "langgraph"

# Windows 默认 stdout 是 GBK，遇到中文 / 符号会报错；强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


def _postgres_uri() -> str:
    """读取连接串；不把密码写入日志。"""
    uri = os.environ.get("POSTGRES_URI", "").strip()
    if not uri:
        print("✗ 未设置 POSTGRES_URI", file=sys.stderr)
        print("示例：$env:POSTGRES_URI='postgresql://postgres:devpass@localhost:5432/langgraph'", file=sys.stderr)
        raise SystemExit(2)
    return uri


def _mask_uri(uri: str) -> str:
    """把 URI 里的密码替换成 ``***``，用于任何输出。"""
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


def _expected_database(uri: str) -> str:
    """按 URI 的 dbname 推出期望库名；URI 没写库名时回落 ``langgraph``。"""
    try:
        parsed = urlparse(uri)
    except ValueError:
        return DEFAULT_DATABASE
    dbname = (parsed.path or "").lstrip("/")
    return dbname or DEFAULT_DATABASE


async def verify(uri: str, *, allow_missing_tables: bool = False) -> None:
    """检查数据库和 checkpoint 表；失败时抛出异常。

    Args:
        uri: 目标连接串。
        allow_missing_tables: 为 True 时，缺 checkpoint 表只告警不报错
            (preflight Step 5 在 Step 6 建表之前调用，全新库必然没有表)。
    """
    expected_database = _expected_database(uri)
    async with await psycopg.AsyncConnection.connect(uri) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT version(), current_database();")
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("Postgres 未返回版本和数据库信息")

            version, database = row
            if isinstance(version, (bytes, bytearray)):
                version = version.decode("utf-8", errors="replace")
            if isinstance(database, (bytes, bytearray)):
                database = database.decode("utf-8", errors="replace")
            print(f"version : {version}")
            print(f"db      : {database}")

            if database != expected_database:
                raise RuntimeError(
                    f"当前数据库是 {database!r}，预期使用 {expected_database!r}；"
                    f"实际 URI: {_mask_uri(uri)}"
                )

            await cursor.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' ORDER BY tablename;"
            )
            tables = {record[0].decode("utf-8", errors="replace") if isinstance(record[0], (bytes, bytearray)) else record[0] for record in await cursor.fetchall()}
            print(f"tables  : {sorted(tables)}")

            missing = EXPECTED_TABLES - tables
            if missing:
                if allow_missing_tables:
                    print(
                        f"! 缺少 checkpoint 表：{sorted(missing)}"
                        "（--allow-missing-tables:仅告警,由 Step 6 建表）",
                    )
                else:
                    raise RuntimeError(
                        f"缺少 checkpoint 表：{sorted(missing)}；请先运行 setup_postgres_schema.py"
                    )


async def run(*, allow_missing_tables: bool = False) -> None:
    """建立异步校验任务。"""
    uri = _postgres_uri()
    await verify(uri, allow_missing_tables=allow_missing_tables)


def _run(coro):
    """用 SelectorEventLoop 跑协程；psycopg 异步在 Windows 上需要这个 loop。"""
    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def main() -> None:
    """命令行入口。"""
    allow_missing_tables = "--allow-missing-tables" in sys.argv[1:]
    try:
        _run(run(allow_missing_tables=allow_missing_tables))
    except SystemExit:
        raise
    except Exception as exc:
        print(f"✗ 验证失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    else:
        if allow_missing_tables:
            print("✓ Postgres 可达(缺表仅告警)")
        else:
            print("✓ 全部 checkpoint 表存在")


if __name__ == "__main__":
    main()
