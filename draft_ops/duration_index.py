"""剪映草稿时长索引更新(Week 5 新增;对齐计划 §2.2)。

剪映客户端在两个地方维护草稿时长:
- ``<draft_dir>/draft_meta_info.json`` — ``tm_duration`` 字段(微秒)。
- ``<drafts_root>/root_meta_info.json`` — ``all_draft_store[]`` 数组,按
  ``draft_name`` 匹配条目,改其 ``tm_duration``。

本模块在每次 :func:`safe_write_draft` 写入完成后调用,确保两个索引与
``draft["duration"]`` 同步。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any] | None:
    """读取 JSON 文件;不存在或损坏时返回 None(不抛)。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, content: dict[str, Any]) -> None:
    """写 JSON 文件(非原子 — 调用方一般在 atomic_write_draft 之后调用,
    此时 disk-level 原子性已由前者保证)。"""
    path.write_text(
        json.dumps(content, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_duration_index(
    draft_dir: Path,
    duration_us: int,
    *,
    log: list[str] | None = None,
) -> dict[str, bool]:
    """同步更新 ``draft_meta_info.json`` + ``root_meta_info.json`` 的 ``tm_duration``。

    Args:
        draft_dir: 草稿目录(通常 ``<drafts_root>/<draft_name>/``)。
        duration_us: 新时长(微秒)。
        log: 可选的状态日志列表(命中 / 缺失会 append 字符串;便于上层写到
            ``state["status_log"]``)。

    Returns:
        字典 ``{"draft_meta": bool, "root_meta": bool}`` — True 表示该文件被
        实际更新,False 表示缺失 / 未匹配条目(仅告警)。
    """
    draft_dir = Path(draft_dir)
    duration_us = int(duration_us)
    result = {"draft_meta": False, "root_meta": False}

    # ---- 1. draft_meta_info.json(草稿目录内)----
    draft_meta_path = draft_dir / "draft_meta_info.json"
    draft_meta = _read_json(draft_meta_path)
    if draft_meta is None:
        # 文件不存在:创建一个最小可用版(剪映客户端首次打开也会生成)
        draft_meta = {}
    draft_meta["tm_duration"] = duration_us
    _write_json(draft_meta_path, draft_meta)
    result["draft_meta"] = True
    if log is not None:
        log.append(
            f"[duration_index] draft_meta_info.json tm_duration={duration_us}"
        )

    # ---- 2. root_meta_info.json(草稿根目录)----
    # 草稿根 = draft_dir 的父目录(因为 draft_dir = <drafts_root>/<draft_name>)
    root_meta_path = draft_dir.parent / "root_meta_info.json"
    if not root_meta_path.exists():
        if log is not None:
            log.append(
                f"[duration_index] root_meta_info.json 缺失({root_meta_path}),仅更新 draft_meta_info.json"
            )
        return result

    root_meta = _read_json(root_meta_path)
    if root_meta is None:
        if log is not None:
            log.append(
                f"[duration_index] root_meta_info.json 解析失败({root_meta_path}),跳过"
            )
        return result

    all_drafts = root_meta.get("all_draft_store")
    if not isinstance(all_drafts, list):
        if log is not None:
            log.append(
                "[duration_index] root_meta_info.all_draft_store 不是 list,跳过"
            )
        return result

    # 按 draft_name 匹配 — draft_name 通常 = draft_dir 的最后一级目录名
    draft_name = draft_dir.name
    matched = False
    for entry in all_drafts:
        if not isinstance(entry, dict):
            continue
        if entry.get("draft_name") == draft_name or entry.get("draft_id") == draft_name:
            entry["tm_duration"] = duration_us
            matched = True
            break
    if matched:
        _write_json(root_meta_path, root_meta)
        result["root_meta"] = True
        if log is not None:
            log.append(
                f"[duration_index] root_meta_info.json all_draft_store[{draft_name}].tm_duration={duration_us}"
            )
    else:
        if log is not None:
            log.append(
                f"[duration_index] root_meta_info.all_draft_store 未匹配 draft_name={draft_name},跳过"
            )

    return result


__all__ = ["update_duration_index"]
