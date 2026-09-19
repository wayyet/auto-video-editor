"""draft_content.json 原子写入。

设计依据(附件 2.4 节,严格对齐"三步法"):
1. 临时文件写入(`tempfile.mkstemp(dir=draft_file.parent)` 保证同盘,rename
   才能原子)
2. 校验 JSON 合法性(前置 `json.loads(serialized)`,写入完成后再 read-back
   不是必要的,序列化本身就能保证合法)
3. 操作系统级重命名覆盖(`os.replace` 在 POSIX 与 NTFS 都是原子的)

任何步骤失败 → 清理临时文件后 raise,确保目标文件不被污染。

剪映 5.9+ 双写:
- ``atomic_write_draft_pair(draft_dir, content)`` 同时原子写 ``draft_content.json``
  和 ``draft_info.json``(剪映客户端 5.9+ 期望二者内容一致;Week 5 起接入)。
- 两个文件各自独立 mkstemp + os.replace,**不**共用临时文件。
  - 任一失败不影响另一个(已成功的那个保留;失败的清临时文件后 raise)。
  - 若两个都失败,目标文件可能保持旧值 — 由上层的 ``safe_write_draft`` 配套
    ``snapshot_before_edit`` + ``restore_from_snapshot`` 在 verify 失败时整目录回退。

Week 5 顶层入口:
- ``safe_write_draft(draft_dir, content)`` 把
  ``check_jianying_not_running`` + ``snapshot_before_edit`` +
  ``atomic_write_draft_pair`` + ``verify_draft_loadable`` + 可选 ``update_duration_index``
  串成一个完整流程;verify 失败时触发 ``restore_from_snapshot`` 整目录回退。
  节点 5/7/8/9/10/11/13/16 的 ``_write_marker`` 全部走这个入口。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable


def atomic_write_draft(draft_file: Path, content: dict) -> None:
    """以原子方式写入 draft_content.json。

    Args:
        draft_file: 目标文件路径(通常 draft_dir/draft_content.json)。
        content: 草稿内容,顶层 dict,会被 json.dumps(ensure_ascii=False,
            indent=2) 序列化。

    Raises:
        TypeError: content 不可 JSON 序列化。
        ValueError: content 序列化后的字符串不是合法 JSON(理论上不会发生,
            因为 json.dumps 总产出合法 JSON;此校验是防御性的)。
        OSError: 写入或 rename 失败。
    """
    # 前置校验:确保生成内容本身是合法 JSON
    serialized = json.dumps(content, ensure_ascii=False, indent=2)
    json.loads(serialized)

    # 同盘 mkstemp 才能保证后续 os.replace 原子
    draft_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=draft_file.parent,
        prefix=".draft_tmp_",
        suffix=".json",
    )
    tmp_path_obj = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        # 操作系统级原子重命名(POSIX/NTFS)
        os.replace(tmp_path, draft_file)
    except Exception:
        if tmp_path_obj.exists():
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def atomic_write_draft_pair(draft_dir: Path, content: dict[str, Any]) -> None:
    """同时原子写入 ``draft_content.json`` 与 ``draft_info.json``。

    剪映 5.9+ 期望这两个文件内容一致(否则"缩略图 00:00/时间轴乱");Week 5 起
    节点统一走该入口而不是直接 ``atomic_write_draft`` 单写。

    Args:
        draft_dir: 草稿目录(将写入 ``draft_dir/draft_content.json`` 和
            ``draft_dir/draft_info.json``)。
        content: 草稿内容(dict),两个文件**内容完全相同** — 序列化字节级一致。

    Raises:
        TypeError: content 不可 JSON 序列化。
        OSError: 任一文件的写入或 rename 失败。

    Notes:
        - 两个文件**各自独立** mkstemp + os.replace;不共用临时文件。
        - 第一个文件写失败:立即 raise;第二个文件不会被尝试。
        - 第一个文件写成功、第二个文件写失败:raise,但第一个文件已成功
          (本函数不负责回退 — 由上层的 ``safe_write_draft`` 在 verify 失败时
          整目录回退)。
    """
    draft_dir = Path(draft_dir)
    draft_dir.mkdir(parents=True, exist_ok=True)

    content_file = draft_dir / "draft_content.json"
    info_file = draft_dir / "draft_info.json"

    # 先写 content(若失败,info 不动)
    atomic_write_draft(content_file, content)
    # 再写 info(若失败,content 已写入新值)
    atomic_write_draft(info_file, content)


# ---------------------------------------------------------------------------
# safe_write_draft — Week 5 顶层入口(组合上述三模块)
# ---------------------------------------------------------------------------

# 类型别名 — 可注入的"剪映进程检测"函数,签名同 check_jianying_not_running
ProcChecker = Callable[[], bool]
# 可注入的"整目录快照"函数,签名同 snapshot_before_edit(返回快照路径)
Snapshotter = Callable[[Path], Path]
# 可注入的"整目录回退"函数,签名同 restore_from_snapshot(返回快照路径)
Restorer = Callable[[Path], Path]
# 可注入的"双文件校验"函数,签名同 verify_draft_loadable(返回 (bool, reason))
Verifier = Callable[[Path], tuple[bool, str]]
# 可注入的"时长索引更新"函数(便于测试跳过)
DurationUpdater = Callable[[Path, int], dict[str, bool]]


def _default_proc_checker() -> bool:
    from draft_ops.safe_write_guard import check_jianying_not_running

    return check_jianying_not_running()


def _default_snapshotter(draft_dir: Path) -> Path:
    from draft_ops.safe_write_guard import snapshot_before_edit

    return snapshot_before_edit(draft_dir)


def _default_restorer(draft_dir: Path) -> Path:
    from draft_ops.safe_write_guard import restore_from_snapshot

    return restore_from_snapshot(draft_dir)


def _default_verifier(draft_dir: Path) -> tuple[bool, str]:
    from draft_ops.verify_after_write import verify_draft_loadable

    return verify_draft_loadable(draft_dir)


def _default_duration_updater(draft_dir: Path, duration_us: int) -> dict[str, bool]:
    from draft_ops.duration_index import update_duration_index

    return update_duration_index(draft_dir, duration_us)


def safe_write_draft(
    draft_dir: Path,
    content: dict[str, Any],
    *,
    duration_us: int | None = None,
    proc_checker: ProcChecker | None = None,
    snapshotter: Snapshotter | None = None,
    restorer: Restorer | None = None,
    verifier: Verifier | None = None,
    duration_updater: DurationUpdater | None = None,
) -> dict[str, Any]:
    """Week 5 顶层写入入口:剪映 5.9+ 双写 + 校验 + 回退 + 时长索引同步。

    流程(对齐计划 §2.1):
        1. ``proc_checker`` — 剪映进程检测(默认 :func:`check_jianying_not_running`)。
           **不**阻断;若返回 True,把告警字符串 append 到 ``status_log``(由调用方读取)。
        2. ``snapshotter`` — 整目录快照(默认 :func:`snapshot_before_edit`)。
           即使剪映在跑,也保证写入失败时能回退。
        3. ``atomic_write_draft_pair`` — 双写 content + info(各自独立 mkstemp)。
        4. ``verifier`` — 双文件一致性 + JSON 合法性校验。
           - 通过 → 返回 ``{"ok": True, ...}``,无回退。
           - 失败 → 触发 ``restorer`` 整目录回退,然后 raise ``RuntimeError``。
        5. ``duration_updater``(可选)— 若 ``duration_us`` 给出,同步更新
           ``draft_meta_info.json`` + ``root_meta_info.json`` 的 ``tm_duration``。

    Args:
        draft_dir: 草稿目录(将写入 ``draft_content.json`` + ``draft_info.json``)。
        content: 草稿内容(dict)。
        duration_us: 若给定,同步更新时长索引(微秒)。
        proc_checker: 可注入的进程检测函数;默认 stdlib ``check_jianying_not_running``。
        snapshotter: 可注入的快照函数(``(draft_dir) -> snap_path``)。
        restorer: 可注入的回退函数(``(draft_dir) -> used_snap_path``)。
        verifier: 可注入的校验函数(``(draft_dir) -> (ok, reason)``)。
        duration_updater: 可注入的时长索引更新函数。

    Returns:
        字典 ``{"ok": bool, "snapshot": Path, "verified": bool, "reason": str,
        "duration_index": dict | None}``,便于上层写到 ``state``。

    Raises:
        RuntimeError: verify 失败且已 restore 时携带 ``reason``。
        TypeError/OSError: 透传自 :func:`atomic_write_draft_pair`。
    """
    draft_dir = Path(draft_dir)

    if proc_checker is None:
        proc_checker = _default_proc_checker
    if snapshotter is None:
        snapshotter = _default_snapshotter
    if restorer is None:
        restorer = _default_restorer
    if verifier is None:
        verifier = _default_verifier
    if duration_updater is None:
        duration_updater = _default_duration_updater

    jianying_running = bool(proc_checker())

    snapshot_path = snapshotter(draft_dir)

    atomic_write_draft_pair(draft_dir, content)

    ok, reason = verifier(draft_dir)
    if not ok:
        # 校验失败:从最近快照整目录回退 → 然后 raise
        used_snap = restorer(draft_dir)
        raise RuntimeError(
            f"safe_write_draft 校验失败 ({reason});已从快照 {used_snap} 回退"
        )

    duration_index_result: dict[str, bool] | None = None
    if duration_us is not None:
        duration_index_result = duration_updater(draft_dir, int(duration_us))

    return {
        "ok": True,
        "snapshot": snapshot_path,
        "verified": True,
        "reason": "ok",
        "jianying_running": jianying_running,
        "duration_index": duration_index_result,
    }


__all__ = [
    "atomic_write_draft",
    "atomic_write_draft_pair",
    "safe_write_draft",
]
