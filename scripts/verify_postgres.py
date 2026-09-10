"""连通性自检：版本、当前数据库及 LangGraph checkpoint 表。"""

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
EXPECTED_DATABASE = "langgraph"

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


async def verify(uri: str) -> None:
    """检查数据库和 checkpoint 表；失败时抛出异常。"""
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

            if database != EXPECTED_DATABASE:
                safe_uri = urlparse(uri)
                safe_uri = safe_uri._replace(netloc=f"{safe_uri.username}:***@{safe_uri.hostname}:{safe_uri.port}" if safe_uri.username else safe_uri.netloc)
                raise RuntimeError(
                    f"当前数据库是 {database!r}，预期使用 {EXPECTED_DATABASE!r}；"
                    f"实际 URI: {safe_uri.geturl()}"
                )

            await cursor.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' ORDER BY tablename;"
            )
            tables = {record[0].decode("utf-8", errors="replace") if isinstance(record[0], (bytes, bytearray)) else record[0] for record in await cursor.fetchall()}
            print(f"tables  : {sorted(tables)}")

            missing = EXPECTED_TABLES - tables
            if missing:
                raise RuntimeError(f"缺少 checkpoint 表：{sorted(missing)}；请先运行 setup_postgres_schema.py")


async def run() -> None:
    """建立异步校验任务。"""
    uri = _postgres_uri()
    await verify(uri)


def _run(coro):
    """用 SelectorEventLoop 跑协程；psycopg 异步在 Windows 上需要这个 loop。"""
    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def main() -> None:
    """命令行入口。"""
    try:
        _run(run())
    except SystemExit:
        raise
    except Exception as exc:
        print(f"✗ 验证失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    else:
        print("✓ 全部 checkpoint 表存在")


if __name__ == "__main__":
    main()
