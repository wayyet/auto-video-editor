r"""一次性工具:旧 SQLite checkpoint 的弃用清单 + 归档(PostgreSQL 统一改造 §5.3)。

⚠️ **一次性迁移工具**,不是运行时组件。运行时代码已经不引用任何 SQLite
(方案 §4.1);这个脚本只负责把历史遗留的 ``checkpoints/*.sqlite`` 登记归档,
让"挂起任务查不到"这件事变成可见的、可重跑的清单,而不是静默丢数据。

背景
----
方案决策 **D2**:旧 SQLite checkpoint **不迁移**到 PostgreSQL,直接弃用并归档。
理由:checkpoint 是工作流中间态,挂起任务重跑即可;旧文件多为测试产物。

危险操作
--------
``--apply`` 会**移动**(不是删除)``checkpoints/`` 下的 ``.sqlite`` / ``-shm`` / ``-wal``,
移动到 ``logs/retired_checkpoints/<日期>/``(``logs/`` 已 gitignore)⇒ 保留可回滚窗口。
``checkpoints/`` 目录本身保留(清空)。

用法
----
```powershell
# 1) 只看清单(不动文件)
.\.venv\Scripts\python.exe scripts\retire_sqlite_checkpoints.py

# 2) 归档 + 把"只存在于旧 SQLite"的挂起任务标成 failed(可重跑)
.\.venv\Scripts\python.exe scripts\retire_sqlite_checkpoints.py --apply
```

编码注意(本机 Postgres 库编码 = ``SQL_ASCII``)
----------------------------------------------
- 读 ``TEXT`` 列拿到的是 ``bytes`` ⇒ 本脚本统一用 ``db.connection.connect``
  (``client_encoding=utf-8``)并对仍为 ``bytes`` 的值做解码兜底。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 默认 stdout 是 GBK;强制 UTF-8 以免中文乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

DEFAULT_CHECKPOINT_DIR = ROOT / "checkpoints"
DEFAULT_ARCHIVE_ROOT = ROOT / "logs" / "retired_checkpoints"

#: 需要一起归档的 SQLite 侧文件(WAL / SHM)
_SIDECAR_SUFFIXES = ("-wal", "-shm")


def _decode(value: Any) -> str:
    """``SQL_ASCII`` 库下 TEXT 列可能返回 ``bytes``;统一解码成 ``str``。"""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def inspect_sqlite(path: Path) -> dict[str, Any]:
    """读一个 ``.sqlite`` 文件:大小、thread_id 列表、checkpoint 条数。

    打不开 / 没有 ``checkpoints`` 表时返回 ``{"error": ...}``,不抛异常 ——
    清单工具必须能处理损坏文件,而不是自己先崩掉。
    """
    info: dict[str, Any] = {
        "file": path.name,
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "thread_ids": [],
        "checkpoint_count": 0,
        "error": None,
    }
    try:
        # 只读打开:绝不修改历史文件
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='table' AND name='checkpoints'"
            )
            if cur.fetchone() is None:
                info["error"] = "no_checkpoints_table"
                return info
            cur.execute("SELECT thread_id, COUNT(*) FROM checkpoints GROUP BY thread_id")
            for raw_tid, count in cur.fetchall():
                info["thread_ids"].append(
                    {"thread_id": _decode(raw_tid), "checkpoint_count": int(count)}
                )
            info["checkpoint_count"] = sum(t["checkpoint_count"] for t in info["thread_ids"])
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — 损坏文件也要进清单
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def collect(checkpoint_dir: Path) -> list[dict[str, Any]]:
    """扫描目录下所有 ``.sqlite`` 主文件(跳过 ``-wal`` / ``-shm`` 侧文件)。"""
    if not checkpoint_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(checkpoint_dir.glob("*.sqlite")):
        out.append(inspect_sqlite(path))
    return out


def collect_sidecars(checkpoint_dir: Path) -> list[Path]:
    """收集 ``-wal`` / ``-shm`` 侧文件。"""
    if not checkpoint_dir.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(checkpoint_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name.endswith(".sqlite"):
            continue
        if any(path.name.endswith(sfx) for sfx in _SIDECAR_SUFFIXES):
            out.append(path)
    return out


def mark_rerunnable(thread_ids: set[str], uri: str | None) -> list[dict[str, Any]]:
    """把"checkpoint 只存在于旧 SQLite"的挂起任务标成 ``failed``(可重跑)。

    方案 §5.3 第 3 步。``workflow_runs.status`` 有 CHECK 约束
    (``running`` / ``suspended`` / ``completed`` / ``failed``),所以落到 ``failed``。

    Args:
        thread_ids: 只存在于旧 SQLite 的 thread_id 集合。
        uri: Postgres 连接串;不给则用 ``db.migrations.postgres_uri()``。

    Returns:
        被改动的行(每行含 ``thread_id`` / 旧 status / 新 status)。
    """
    if not thread_ids:
        return []
    from db.connection import connect
    from db.migrations import postgres_uri

    target = uri or postgres_uri()
    changed: list[dict[str, Any]] = []
    with connect(target) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT thread_id, status FROM workflow_runs"
                " WHERE status IN ('running', 'suspended')"
            )
            rows = cur.fetchall()
            for raw_tid, raw_status in rows:
                tid = _decode(raw_tid)
                if tid not in thread_ids:
                    continue
                cur.execute(
                    "UPDATE workflow_runs SET status = 'failed' WHERE thread_id = %s",
                    (raw_tid,),
                )
                changed.append(
                    {"thread_id": tid, "from": _decode(raw_status), "to": "failed"}
                )
        conn.commit()
    return changed


def _postgres_thread_ids(uri: str | None) -> tuple[set[str], str | None]:
    """查 Postgres ``checkpoints`` 表里已有的 thread_id 集合。

    Returns:
        ``(集合, 错误信息)``。查不到时返回 ``(空集合, 错误串)`` —— 调用方据此
        **跳过**标记动作。宁可不动 ``workflow_runs``,也不要把"Postgres 侧其实
        还有 checkpoint"的挂起任务误标成 ``failed``。
    """
    try:
        from db.connection import connect
        from db.migrations import postgres_uri

        target = uri or postgres_uri()
        found: set[str] = set()
        with connect(target) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
                for (raw,) in cur.fetchall():
                    found.add(_decode(raw))
        return found, None
    except Exception as e:  # noqa: BLE001
        return set(), f"{type(e).__name__}: {e}"


def archive(checkpoint_dir: Path, archive_dir: Path, names: set[str]) -> list[str]:
    """把清单里的文件**移动**到归档目录(不是删除)。

    Returns:
        实际移动的文件的相对路径列表。
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for path in sorted(checkpoint_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name not in names:
            continue
        target = archive_dir / path.name
        shutil.move(str(path), str(target))
        moved.append(path.name)
    return moved


def main() -> int:
    p = argparse.ArgumentParser(
        prog="retire_sqlite_checkpoints",
        description="生成旧 SQLite checkpoint 弃用清单并归档(方案 §5.3)",
    )
    p.add_argument("--checkpoint-dir", default=str(DEFAULT_CHECKPOINT_DIR),
                   help="SQLite checkpoint 目录(默认 <repo>/checkpoints)")
    p.add_argument("--archive-dir", default="",
                   help="归档目录(默认 logs/retired_checkpoints/<日期>)")
    p.add_argument("--apply", action="store_true",
                   help="真正执行归档 + 标记重跑(不给则只打印清单)")
    p.add_argument("--uri", default=None, help="Postgres 连接串(默认 db.migrations.postgres_uri())")
    p.add_argument("--no-mark", action="store_true",
                   help="不修改 workflow_runs(只归档)")
    args = p.parse_args()

    checkpoint_dir = Path(args.checkpoint_dir).resolve()
    date = dt.date.today().isoformat()
    archive_dir = (
        Path(args.archive_dir).resolve()
        if args.archive_dir
        else DEFAULT_ARCHIVE_ROOT / date
    )

    entries = collect(checkpoint_dir)
    sidecars = collect_sidecars(checkpoint_dir)
    total_size = sum(e["size_bytes"] for e in entries)

    print(f"SQLite checkpoint 目录: {checkpoint_dir}")
    print(f"  主文件 {len(entries)} 个,侧文件 {len(sidecars)} 个,合计 {total_size / 1024 / 1024:.2f} MiB")
    print()
    print(f"{'文件':<34} {'大小':>10}  {'ckpt':>6}  thread_id")
    print("-" * 100)
    for e in entries:
        tids = ", ".join(
            f"{t['thread_id']}({t['checkpoint_count']})" for t in e["thread_ids"]
        ) or "-"
        print(f"{e['file']:<34} {e['size_bytes']:>10}  {e['checkpoint_count']:>6}  {tids}")
        if e["error"]:
            print(f"    ⚠️ 读取失败: {e['error']}")
    for sc in sidecars:
        print(f"{sc.name:<34} {sc.stat().st_size:>10}  (侧文件)")

    # 旧 SQLite 里的 thread_id 全集。
    sqlite_tids: set[str] = set()
    for e in entries:
        for t in e["thread_ids"]:
            sqlite_tids.add(t["thread_id"])

    # "只存在于旧 SQLite"的 thread_id —— 它们的 Postgres 侧**没有** checkpoint。
    # 必须两边都核对:同一个 thread_id 可能 SQLite / Postgres 都有(阶段 A/B 搬库
    # 之后又在旧路径上跑过一次),那种情况**不能**标成 failed。
    pg_tids, pg_error = _postgres_thread_ids(args.uri)
    sqlite_only = sqlite_tids - pg_tids

    manifest = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "checkpoint_dir": str(checkpoint_dir),
        "archive_dir": str(archive_dir),
        "files": entries,
        "sidecars": [p.name for p in sidecars],
        "sqlite_thread_ids": sorted(sqlite_tids),
        "sqlite_only_thread_ids": sorted(sqlite_only),
        "postgres_thread_ids_count": len(pg_tids),
        "postgres_lookup_error": pg_error,
    }
    manifest_path = archive_dir / "retirement_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print()
    print(f"清单已写: {manifest_path}")

    if not args.apply:
        print()
        print("dry-run:未移动任何文件。加 --apply 执行归档。")
        return 0

    names = {e["file"] for e in entries} | {p.name for p in sidecars}
    moved = archive(checkpoint_dir, archive_dir, names)
    print(f"已归档 {len(moved)} 个文件到 {archive_dir}")

    if args.no_mark:
        print("--no-mark:未修改 workflow_runs")
    elif pg_error:
        print(
            f"⚠️ 读 Postgres checkpoints 表失败({pg_error}) —— 跳过 workflow_runs 标记"
            "(避免把 Postgres 侧仍有 checkpoint 的挂起任务误标成 failed)",
            file=sys.stderr,
        )
    else:
        try:
            changed = mark_rerunnable(sqlite_only, args.uri)
            print(f"workflow_runs 标记为需重跑(failed):{len(changed)} 行")
            for c in changed:
                print(f"  {c['thread_id']}: {c['from']} -> {c['to']}")
        except Exception as e:  # noqa: BLE001 — 标记失败不影响归档结果
            print(f"⚠️ 标记 workflow_runs 失败: {type(e).__name__}: {e}", file=sys.stderr)

    left = [p.name for p in checkpoint_dir.iterdir() if p.is_file()]
    print(f"{checkpoint_dir} 剩余文件: {left if left else '（已清空,目录保留）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
