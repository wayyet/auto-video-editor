"""首次或幂等创建 LangGraph Postgres checkpointer 所需的表。"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# 让脚本不论从哪个目录运行,都能 import 仓库根下的 ``scripts._pg_common``。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._pg_common import require_postgres_uri, run_async  # noqa: E402

# Windows 默认 stdout 是 GBK，遇到中文 / 符号会报错；强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


async def setup_schema(uri: str) -> str:
    """建表并返回当前数据库名。"""
    async with AsyncPostgresSaver.from_conn_string(uri) as checkpointer:
        await checkpointer.setup()
        return urlparse(uri).path.lstrip("/") or "postgres"


def main() -> None:
    """命令行入口。"""
    uri = require_postgres_uri()
    database = run_async(setup_schema(uri))
    print(f"✓ checkpoint tables created in database: {database}")


if __name__ == "__main__":
    main()
