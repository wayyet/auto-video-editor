"""fork_draft_for_english_branch — 英文分支起点(Week 4 新增,对照计划 §4.1)。

职责:把 snapshot② 草稿目录递归复制成独立副本,产出 ``draft_dir_en_branch``。

关键设计(对齐计划 §4.1):
- 目标目录 ``<drafts_root>/en_branch_<session_id>_<timestamp>/``,避免 thread 冲突
- ``shutil.copytree(src, dst, dirs_exist_ok=True)``,**不**做 symlink
- **幂等**:目标目录已存在且含 ``draft_content.json`` 时,复用并仅更新 state
- **不**调 ``detect_draft_encryption``(snapshot② 已确认明文)
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from config import resolve_draft_dir
from state import WorkflowState


def _compute_target_dir(state: WorkflowState) -> Path:
    """根据 session_id + 时间戳计算 en_branch 目录的绝对路径。"""
    base = resolve_draft_dir(state)
    session = state.get("session_id") or "default"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return base.parent / f"en_branch_{session}_{timestamp}"


def _find_existing_target(state: WorkflowState) -> Path | None:
    """查找已存在的 en_branch 目录(同 session_id)。

    实现:在 drafts_root.parent 下匹配 ``en_branch_<session_id>_*`` 的目录,
    取 mtime 最新且含 draft_content.json 的那个。
    """
    base = resolve_draft_dir(state)
    session = state.get("session_id") or "default"
    parent = base.parent
    if not parent.exists():
        return None
    prefix = f"en_branch_{session}_"
    candidates: list[Path] = []
    for entry in parent.iterdir():
        if entry.is_dir() and entry.name.startswith(prefix):
            if (entry / "draft_content.json").exists():
                candidates.append(entry)
    if not candidates:
        return None
    # 取最新 mtime
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _copy_snapshot(src_dir: Path, dst_dir: Path) -> None:
    """递归复制 src_dir 到 dst_dir(覆盖),不做 symlink。"""
    shutil.copytree(src=str(src_dir), dst=str(dst_dir), dirs_exist_ok=True)


def _fork(state: WorkflowState) -> dict:
    """纯函数:根据 state 计算或复用 en_branch 目录。

    Returns:
        包含 ``draft_dir_en_branch`` 字段的 dict。
    """
    snapshot2 = state.get("snapshot2_path")
    if not snapshot2:
        # snapshot2 路径缺失(节点 7 失败?),早退
        return {"draft_dir_en_branch": None}

    src_dir = Path(snapshot2).parent
    if not (src_dir / "draft_content.json").exists():
        return {"draft_dir_en_branch": None}

    # 幂等:state 或文件系统已有匹配目录 → 复用
    state_existing = state.get("draft_dir_en_branch")
    if state_existing and Path(state_existing).exists() and (Path(state_existing) / "draft_content.json").exists():
        return {"draft_dir_en_branch": str(state_existing)}

    fs_existing = _find_existing_target(state)
    if fs_existing is not None:
        return {"draft_dir_en_branch": str(fs_existing)}

    target = _compute_target_dir(state)
    target.parent.mkdir(parents=True, exist_ok=True)
    _copy_snapshot(src_dir, target)

    return {"draft_dir_en_branch": str(target)}


def fork_draft_for_english_branch(state: WorkflowState) -> dict:
    """LangGraph 节点:复制 snapshot② 到 en_branch 目录,写 state。

    返回**只含变更字段**的 dict(避免 fan-in 时与其他分支并发写同一字段)。
    ``status_log`` 用 reducer 合并,见 state._append_unique。
    """
    fork_result = _fork(state)
    target = fork_result.get("draft_dir_en_branch")

    return {
        "draft_dir_en_branch": fork_result.get("draft_dir_en_branch"),
        "status_log": [f"node_fork_english_branch_done:{target}"],
    }