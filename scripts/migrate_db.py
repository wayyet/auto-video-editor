"""应用层数据库迁移入口(PostgreSQL 统一改造方案 §9 阶段 B)。

对 ``POSTGRES_URI`` 指向的库执行应用层迁移(``app_schema_migrations`` /
``chat_sessions`` / ``workflow_runs``)。

LangGraph 自己的 4 张 checkpoint 表由 ``scripts/setup_postgres_schema.py``
(``AsyncPostgresSaver.setup()``)负责,本脚本**不碰**。

用法::

    # 用 POSTGRES_URI(默认取 config.POSTGRES_URI)
    .\\.venv\\Scripts\\python.exe scripts\\migrate_db.py

    # 指定连接串
    .\\.venv\\Scripts\\python.exe scripts\\migrate_db.py --uri postgresql://postgres:devpass@localhost:5432/video_workflow

    # 只看状态,不执行
    .\\.venv\\Scripts\\python.exe scripts\\migrate_db.py --status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让 ``import db.migrations`` 能解析到项目根
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.connection import connection as _db_connection  # noqa: E402
from db.migrations import (  # noqa: E402
    MIGRATIONS_TABLE_NAME,
    apply_migrations,
    postgres_uri,
)

# Windows 默认 stdout 是 GBK，遇到中文 / 符号会报错；强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


def _print_status(uri: str) -> None:
    """打印迁移台账状态(不执行任何迁移)。"""
    with _db_connection(uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = %s",
                (MIGRATIONS_TABLE_NAME,),
            )
            if cur.fetchone() is None:
                print(f"✗ 表 {MIGRATIONS_TABLE_NAME} 不存在 —— 迁移尚未执行过")
                return
            cur.execute(
                f"SELECT version, name, applied_at FROM {MIGRATIONS_TABLE_NAME} "
                "ORDER BY version"
            )
            rows = cur.fetchall()

    print(f"已应用 {len(rows)} 条迁移:")
    for version, name, applied_at in rows:
        print(f"  {version:>3}  {name}  @ {applied_at}")


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="migrate_db",
        description="执行 auto-video-editor 的应用层 PostgreSQL 迁移(幂等)",
    )
    parser.add_argument("--uri", help="目标连接串;不给则用 POSTGRES_URI / config 默认值")
    parser.add_argument(
        "--status",
        action="store_true",
        help="只打印已应用的迁移版本,不执行迁移",
    )
    args = parser.parse_args()

    uri = args.uri or postgres_uri()

    try:
        if args.status:
            _print_status(uri)
            return 0

        with _db_connection(uri) as conn:
            applied = apply_migrations(conn)

        if applied:
            print(f"✓ 已应用迁移版本: {applied}")
        else:
            print("✓ 数据库已是最新,无需迁移")
        return 0
    except Exception as exc:  # noqa: BLE001 — 顶层兜底,给可操作报错
        print(f"✗ 迁移失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
