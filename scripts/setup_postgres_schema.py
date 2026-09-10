"""首次或幂等创建 LangGraph Postgres checkpointer 所需的表。"""

from __future__ import annotations

import asyncio
import os
import selectors
import sys
from urllib.parse import urlparse

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

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


async def setup_schema(uri: str) -> str:
    """建表并返回当前数据库名。"""
    async with AsyncPostgresSaver.from_conn_string(uri) as checkpointer:
        await checkpointer.setup()
        return urlparse(uri).path.lstrip("/") or "postgres"


def _run(coro):
    """用 SelectorEventLoop 跑协程；psycopg 异步在 Windows 上需要这个 loop。"""
    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def main() -> None:
    """命令行入口。"""
    uri = _postgres_uri()
    database = _run(setup_schema(uri))
    print(f"✓ checkpoint tables created in database: {database}")


if __name__ == "__main__":
    main()
