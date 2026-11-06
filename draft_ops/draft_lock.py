"""草稿写入互斥与恢复前文件安全(2026-11-06 人工关卡与剪映草稿导入 阶段 3)。

计划 §8 阶段 3 的统一门禁组件(计划 §7.3 组件表中的 ``draft_lock.py``):
- **剪映进程检查**:委托 :mod:`draft_ops.safe_write_guard`(stdlib tasklist/pgrep,
  检测失败 = 不在跑,不误伤);
- **草稿目录占用探测**:best-effort —— Windows 上以读写模式打开
  ``draft_content.json``,``PermissionError`` 即视为被其他进程(剪映自动保存)
  占用;探测本身绝不修改文件内容;
- **恢复前校验**(:func:`validate_draft_for_resume`):草稿文件存在、JSON 可解析、
  mtime 合理(不允许未来时间/epoch 0);
- **审计哈希**(:func:`hash_draft_files`):对人工修改后的草稿算 SHA-256,由恢复
  入口写进任务 manifest(console_run.json)或打印到 CLI stderr。

互斥语义(计划 §8 阶段 3):
- 写入侧:``safe_write_draft`` 按 ``config.JY_WRITE_FAIL_ON_JIANYING_RUNNING``
  (默认开)在剪映运行时**失败**而不是告警继续;
- 恢复侧:恢复入口按 ``config.JY_RESUME_REQUIRE_CLOSED``(默认开)在剪映运行时
  **拒绝恢复** —— 人工关卡期间允许剪映打开,但"保存并关闭剪映"是恢复的前置条件。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable


#: 可注入的剪映进程检测(签名同 safe_write_guard.check_jianying_not_running)
ProcChecker = Callable[[], bool]


def jianying_running(proc_checker: ProcChecker | None = None) -> bool:
    """剪映客户端是否在运行(委托 safe_write_guard;检测失败按不在跑)。"""
    if proc_checker is not None:
        try:
            return bool(proc_checker())
        except Exception:  # noqa: BLE001 — 检测失败不误伤主流程
            return False
    from draft_ops.safe_write_guard import check_jianying_not_running

    return check_jianying_not_running()


def draft_dir_locked(draft_dir: Path) -> bool:
    """探测草稿目录是否被占用(best-effort,只读不写)。

    以 ``O_RDWR`` 打开 ``draft_content.json``:Windows 上剪映自动保存/索引期间
    会持有写句柄,``PermissionError`` = 占用;其他异常(不存在等)不算占用 ——
    存在性由调用方另行校验。POSIX 无强制锁,探测恒 False(进程检查兜底)。
    """
    target = Path(draft_dir) / "draft_content.json"
    if not target.is_file():
        return False
    try:
        fd = os.open(str(target), os.O_RDWR)
    except PermissionError:
        return True
    except OSError:
        return False
    else:
        os.close(fd)
        return False


def validate_draft_for_resume(draft_file: Path | str | None) -> list[str]:
    """恢复前草稿文件校验;返回问题列表(空列表 = 通过,不抛异常)。

    检查项(计划 §8 阶段 3"恢复前检查草稿文件存在、JSON 可解析、修改时间符合预期"):
    - 路径缺失 / 文件不存在;
    - JSON 解析失败(人工半保存 / 剪映写坏);
    - mtime 在未来(时钟漂移 / 异常)或为 0;
    - 草稿目录被占用(draft_dir_locked)。
    每条问题都是可直接展示给操作员的中文消息。
    """
    problems: list[str] = []
    if not draft_file or not str(draft_file).strip():
        return ["state.draft_path 缺失:无法定位要校验的草稿"]
    path = Path(str(draft_file))
    if not path.is_file():
        problems.append(f"草稿文件不存在(可能被删除/移动): {path}")
        return problems
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        problems.append(f"草稿 JSON 解析失败(剪映可能未保存完成或文件损坏): {path} ({e})")
    try:
        mtime = path.stat().st_mtime
        if mtime <= 0:
            problems.append(f"草稿修改时间异常(mtime={mtime}): {path}")
        elif mtime > time.time() + 60:
            problems.append(
                f"草稿修改时间在未来(mtime 比当前时间晚 60s 以上,疑似时钟漂移): {path}"
            )
    except OSError as e:
        problems.append(f"草稿 stat 失败: {path} ({e})")
    if draft_dir_locked(path.parent):
        problems.append(
            f"草稿目录被其他进程占用(剪映可能未完全退出): {path.parent};请保存并关闭剪映后重试"
        )
    return problems


def hash_draft_files(draft_file: Path | str) -> dict[str, str]:
    """对草稿双文件计算 SHA-256(审计用;缺哪个文件就少一个键)。"""
    out: dict[str, str] = {}
    base = Path(str(draft_file))
    for name in ("draft_content.json", "draft_info.json"):
        p = base.parent / name
        if not p.is_file():
            continue
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        out[name] = h.hexdigest()
    return out


def assert_resume_ready(
    draft_file: Path | str | None,
    *,
    proc_checker: ProcChecker | None = None,
    require_jianying_closed: bool | None = None,
) -> dict[str, Any]:
    """恢复前总门禁;返回 ``{"ok": bool, "problems": [...], "hashes": {...}}``。

    Args:
        draft_file: state.draft_path(草稿文件路径)。
        proc_checker: 可注入进程检测(单测)。
        require_jianying_closed: 缺省读 ``config.JY_RESUME_REQUIRE_CLOSED``。

    人工关卡期间允许剪映打开编辑;恢复动作的前置条件是"保存并关闭剪映"。
    """
    if require_jianying_closed is None:
        import config

        require_jianying_closed = bool(
            getattr(config, "JY_RESUME_REQUIRE_CLOSED", True)
        )

    problems = validate_draft_for_resume(draft_file)
    running = jianying_running(proc_checker)
    if running and require_jianying_closed:
        problems.append(
            "检测到剪映客户端正在运行:请先在剪映中保存草稿并完全退出剪映,"
            "再执行恢复(恢复后工作流要继续改写同一份草稿,避免与剪映自动保存竞争)"
        )
    hashes = hash_draft_files(draft_file) if draft_file and Path(str(draft_file)).is_file() else {}
    return {"ok": not problems, "problems": problems, "hashes": hashes, "jianying_running": running}


__all__ = [
    "assert_resume_ready",
    "draft_dir_locked",
    "hash_draft_files",
    "jianying_running",
    "validate_draft_for_resume",
]
