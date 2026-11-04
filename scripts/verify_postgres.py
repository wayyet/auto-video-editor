"""连通性自检：版本、当前数据库及 LangGraph checkpoint 表。

期望库名不再硬编码 —— 按连接串 URI 的 dbname 动态比对
(URI 未指定库名时才回落 ``video_workflow``),以支持任意 ``-PostgresUri``。

用法::

    # 严格模式(手工核验):缺 checkpoint 表 → 失败
    python scripts/verify_postgres.py

    # 宽松模式(preflight Step 5 调用):缺 checkpoint 表 → 仅告警
    python scripts/verify_postgres.py --allow-missing-tables
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg

# 让脚本不论从哪个目录运行,都能 import 仓库根下的 ``scripts._pg_common``。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._pg_common import mask_uri, require_postgres_uri, run_async  # noqa: E402


EXPECTED_TABLES = {
    "checkpoints",
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoint_migrations",
}
# 仅当 URI 里没有 dbname 时才回落到这个库名
DEFAULT_DATABASE = "video_workflow"

# Windows 默认 stdout 是 GBK，遇到中文 / 符号会报错；强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


def _expected_database(uri: str) -> str:
    """按 URI 的 dbname 推出期望库名；URI 没写库名时回落 ``video_workflow``。"""
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
                    f"实际 URI: {mask_uri(uri)}"
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
    uri = require_postgres_uri()
    await verify(uri, allow_missing_tables=allow_missing_tables)


def main() -> None:
    """命令行入口。"""
    allow_missing_tables = "--allow-missing-tables" in sys.argv[1:]
    try:
        run_async(run(allow_missing_tables=allow_missing_tables))
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
