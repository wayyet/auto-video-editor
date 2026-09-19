"""剪映写入安全护栏(Week 5 新增;对齐计划 §1.4 / §2.1)。

模块组成:
- :func:`check_jianying_not_running` — 剪映进程检测(告警,**不**阻断)。
- :func:`snapshot_before_edit` — 写入前复制整目录快照到 ``<draft_dir>/.snapshots/<ts>/``。
- :func:`restore_from_snapshot` — verify 失败时整目录回退到最近快照。
- :func:`cleanup_old_snapshots` — GC 保留最近 N 个快照。

设计要点(对齐附件 §3.4 / §3.5):
- 剪映进程检测**只读、不阻断**:依据 jianying-draft-edit 黄金法则
  "强杀进程会丢失用户正在编辑的内容"。
- 快照机制与 node_07 既有 ``snapshot2`` 完全独立(后者在
  ``<drafts_root>/snapshots/snapshot2/``,本模块快照在
  ``<draft_dir>/.snapshots/``)。
- 进程检测用 stdlib ``subprocess`` 调系统命令(windows ``tasklist`` /
  linux ``pgrep``);不引入 ``psutil`` 等额外依赖(``requirements.txt`` 不变)。
- 全部 IO / 子进程操作支持函数注入,便于单测在 Linux 上跑(参见
  ``tests/unit/test_safe_write_guard.py``)。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# 剪映进程检测
# ---------------------------------------------------------------------------

# 剪映 Windows 客户端的可执行文件名(常见命名;5.9+ 验证)。
_JIANYING_PROC_NAMES_WINDOWS: tuple[str, ...] = (
    "JianyingPro.exe",
    "JianyingPro",
)
# 剪映 macOS / Linux 客户端名(用于跨平台 stdlib 检测;本仓库主要在 Windows 跑)。
_JIANYING_PROC_NAMES_POSIX: tuple[str, ...] = (
    "JianyingPro",
    "jianyingpro",
)


def _default_windows_proc_query() -> list[str]:
    """Windows:`tasklist /FI "IMAGENAME eq JianyingPro.exe"` → 匹配行列表。

    返回包含 ``JianyingPro.exe`` 的非空行(去掉表头)。
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    out: list[str] = []
    for line in result.stdout.splitlines():
        if "JianyingPro" in line:
            out.append(line.strip())
    return out


def _default_posix_proc_query() -> list[str]:
    """POSIX:`pgrep -fl JianyingPro` → 匹配行列表(best-effort,pgrep 不存在时不抛)。"""
    try:
        result = subprocess.run(
            ["pgrep", "-fl", "JianyingPro"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def check_jianying_not_running(
    proc_query_fn: Callable[[], list[str]] | None = None,
) -> bool:
    """检测剪映进程是否在运行(只读、不阻断)。

    Args:
        proc_query_fn: 可注入的进程查询函数(返回包含 ``JianyingPro`` 字符串的
            行列表);默认按平台自动选 ``tasklist`` 或 ``pgrep``。

    Returns:
        True 表示检测到剪映进程在运行(调用方应写告警日志);
        False 表示进程不在或检测失败(本机不在 Windows / 工具缺失)。
    """
    if proc_query_fn is not None:
        try:
            rows = proc_query_fn()
        except Exception:  # pragma: no cover - 防御
            return False
        return any("JianyingPro" in r for r in rows)

    if sys.platform == "win32":
        rows = _default_windows_proc_query()
    else:
        rows = _default_posix_proc_query()
    return any("JianyingPro" in r for r in rows)


# ---------------------------------------------------------------------------
# 快照机制
# ---------------------------------------------------------------------------

_SNAPSHOT_DIR_NAME = ".snapshots"


def _utc_iso_now() -> str:
    """UTC 时间戳(秒级精度,用于快照目录名)。

    注:Windows 目录名禁止 ``:``,所以用 ``-`` 替代时间分隔符。字典序仍与时间序一致。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def snapshot_before_edit(
    draft_dir: Path,
    snapshot_fn: Callable[[Path, Path], None] | None = None,
    ts: str | None = None,
) -> Path:
    """写入前复制整草稿目录到 ``<draft_dir>/.snapshots/<ts>/``。

    Args:
        draft_dir: 草稿目录(将被复制)。
        snapshot_fn: 可注入的复制函数;默认 :func:`shutil.copytree`。
            单测可换成 ``lambda src, dst: dst.mkdir(parents=True, exist_ok=True)``
            跳过实际复制。
        ts: 时间戳目录名(UTC ISO,秒级);默认 :func:`_utc_iso_now`。
            测试时可注入固定值。

    Returns:
        快照目录路径(``<draft_dir>/.snapshots/<ts>``)。
    """
    draft_dir = Path(draft_dir)
    if not draft_dir.exists():
        # 空草稿目录:不复制,直接返回预期快照路径
        snap_dir = draft_dir / _SNAPSHOT_DIR_NAME / (ts or _utc_iso_now())
        snap_dir.mkdir(parents=True, exist_ok=True)
        return snap_dir

    snap_dir = draft_dir / _SNAPSHOT_DIR_NAME / (ts or _utc_iso_now())
    if snapshot_fn is None:
        def _default_snapshot(src: Path, dst: Path) -> None:
            # 必须排除 ``.snapshots`` 子目录,否则会把快照本身递归复制到快照里
            shutil.copytree(
                src,
                dst,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(_SNAPSHOT_DIR_NAME),
            )

        snapshot_fn = _default_snapshot
    snapshot_fn(draft_dir, snap_dir)
    return snap_dir


def restore_from_snapshot(
    draft_dir: Path,
    snapshot_dir: Path | None = None,
) -> Path:
    """从快照整目录回退 ``draft_dir`` 的内容。

    Args:
        draft_dir: 要回退的草稿目录(其内容会被快照覆盖)。
        snapshot_dir: 指定的快照目录;默认取 ``draft_dir/.snapshots/`` 下**最新**
            一个子目录(按字典序排序 — UTC ISO 时间戳字典序 == 时间序)。

    Returns:
        实际使用的快照路径。

    Raises:
        FileNotFoundError: 找不到任何快照目录。
    """
    draft_dir = Path(draft_dir)
    snapshots_root = draft_dir / _SNAPSHOT_DIR_NAME
    if snapshot_dir is None:
        if not snapshots_root.exists():
            raise FileNotFoundError(f"no snapshots dir: {snapshots_root}")
        candidates = sorted(p for p in snapshots_root.iterdir() if p.is_dir())
        if not candidates:
            raise FileNotFoundError(f"no snapshot subdir under {snapshots_root}")
        snapshot_dir = candidates[-1]
    snapshot_dir = Path(snapshot_dir)
    if not snapshot_dir.exists():
        raise FileNotFoundError(f"snapshot not found: {snapshot_dir}")

    # 清空 draft_dir 当前内容(但保留 .snapshots 目录)→ 把 snapshot 内容复制进来
    for child in draft_dir.iterdir():
        if child.name == _SNAPSHOT_DIR_NAME:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass
    for child in snapshot_dir.iterdir():
        dst = draft_dir / child.name
        if child.is_dir():
            shutil.copytree(child, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dst)
    return snapshot_dir


def cleanup_old_snapshots(draft_dir: Path, keep: int = 5) -> int:
    """GC:只保留最近 ``keep`` 个快照目录(按字典序 == UTC ISO 时间序);返回删除数。"""
    draft_dir = Path(draft_dir)
    snapshots_root = draft_dir / _SNAPSHOT_DIR_NAME
    if not snapshots_root.exists():
        return 0
    candidates = sorted(p for p in snapshots_root.iterdir() if p.is_dir())
    to_delete = candidates[:-keep] if len(candidates) > keep else []
    for p in to_delete:
        shutil.rmtree(p, ignore_errors=True)
    return len(to_delete)


__all__ = [
    "check_jianying_not_running",
    "snapshot_before_edit",
    "restore_from_snapshot",
    "cleanup_old_snapshots",
]
