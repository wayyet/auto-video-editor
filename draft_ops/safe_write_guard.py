"""剪映写入安全护栏(Week 5 新增;对齐计划 §1.4 / §2.1)。

模块组成:
- :func:`probe_jianying_state` — 剪映进程**三态**检测(running / not_running /
  unknown,fail-closed 语义;设计审查 20261118 P1-02)。
- :func:`check_jianying_not_running` — 旧布尔口径(True=在跑),语义固化在
  告警类消费方,内部改由三态探测实现(unknown 折叠为 False,行为不变)。
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
# 剪映进程检测(三态,fail-closed)
# ---------------------------------------------------------------------------

# 三态检测结果(设计审查 20261118 §5.4):
PROC_RUNNING = "running"          # 确认剪映进程在运行(写入门禁应拒绝)
PROC_NOT_RUNNING = "not_running"  # 确认剪映进程不存在(允许写入)
PROC_UNKNOWN = "unknown"          # 检测失败/超时/工具缺失/结果无法判定(一律拒绝写入)

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


def _default_windows_proc_query() -> list[str] | None:
    """Windows:`tasklist /FI "IMAGENAME eq JianyingPro.exe"` → 匹配行列表。

    返回包含 ``JianyingPro`` 的非空行(去掉表头);命令失败/超时/工具缺失时
    返回 ``None`` —— 调用方必须把 ``None`` 视为**状态未知**(UNKNOWN),不得
    当成"剪映不在运行"(fail-open)。
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe", "/NH"],
            capture_output=True,
            text=True,
            # tasklist 在中文 Windows 上按 OEM 代码页(GBK)输出。设了
            # PYTHONUTF8=1(计划 §6.6 中文路径乱码的标准处置)之后,text=True
            # 会按 UTF-8 解码,遇到汉字直接 UnicodeDecodeError 把整个守卫炸掉。
            # errors="replace": 解码失败只丢个别字节,不抛异常 —— 这里只做进程名
            # 子串匹配,丢字节不影响判断。
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out: list[str] = []
    for line in (result.stdout or "").splitlines():
        if "JianyingPro" in line:
            out.append(line.strip())
    return out


def _default_posix_proc_query() -> list[str] | None:
    """POSIX:`pgrep -fl JianyingPro` → 匹配行列表;失败/超时/工具缺失 → None。"""
    try:
        result = subprocess.run(
            ["pgrep", "-fl", "JianyingPro"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def probe_jianying_state(
    proc_query_fn: Callable[[], list[str] | None] | None = None,
) -> str:
    """三态剪映进程检测 —— 写入门禁专用(fail-closed,设计审查 20261118 P1-02)。

    Args:
        proc_query_fn: 可注入的进程查询函数(返回包含 ``JianyingPro`` 字符串的
            行列表,命令失败返回 ``None``);默认按平台自动选 ``tasklist`` 或
            ``pgrep``。

    Returns:
        ``PROC_RUNNING``:确认剪映进程在运行 → 写入必须拒绝;
        ``PROC_NOT_RUNNING``:确认剪映进程不存在 → 允许写入;
        ``PROC_UNKNOWN``:命令失败/超时/工具缺失/注入函数返回 ``None`` 或抛异常
        → **一律拒绝写入**(检测状态不明确 ≠ 剪映不在运行)。

    Notes:
        这只是"进程状态门禁":检查通过后剪映仍可能随即被启动,没有系统级
        互斥机制,日志与文档不得声称获得排他锁。
    """
    if proc_query_fn is not None:
        try:
            rows = proc_query_fn()
        except Exception:
            return PROC_UNKNOWN
        if rows is None:
            return PROC_UNKNOWN
        return PROC_RUNNING if any("JianyingPro" in r for r in rows) else PROC_NOT_RUNNING

    rows = (
        _default_windows_proc_query()
        if sys.platform == "win32"
        else _default_posix_proc_query()
    )
    if rows is None:
        return PROC_UNKNOWN
    return PROC_RUNNING if any("JianyingPro" in r for r in rows) else PROC_NOT_RUNNING


def check_jianying_not_running(
    proc_query_fn: Callable[[], list[str] | None] | None = None,
) -> bool:
    """旧布尔口径(True = 检测到剪映进程在运行;只读、不阻断)。

    True=在跑 的语义固化在 5 处调用方(draft_lock / draft_publisher / 脚本预检 /
    atomic_writer 默认门禁 / 单测),不得反转。unknown 状态在本口径下折叠为
    False(与历史 fail-open 行为一致,仅供告警类消费方);**写入门禁必须直接用
    :func:`probe_jianying_state` 并对 unknown 拒绝**(见 atomic_writer)。
    """
    return probe_jianying_state(proc_query_fn) == PROC_RUNNING


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
    "PROC_RUNNING",
    "PROC_NOT_RUNNING",
    "PROC_UNKNOWN",
    "check_jianying_not_running",
    "probe_jianying_state",
    "snapshot_before_edit",
    "restore_from_snapshot",
    "cleanup_old_snapshots",
]
