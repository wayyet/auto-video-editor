"""写入后验证(Week 5 新增;对齐计划 §2.1)。

剪映 5.9+ 期望 ``draft_content.json`` 与 ``draft_info.json`` 内容一致,
且都能被解析为合法 JSON。本模块在 :func:`safe_write_draft` 双写后调用,
失败时触发 ``restore_from_snapshot`` 整目录回退。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


JSON_LOADER: Callable[[str], Any] = json.loads


def verify_draft_loadable(
    draft_dir: Path,
    *,
    reader: Callable[[str], Any] | None = None,
) -> tuple[bool, str]:
    """校验 ``draft_dir/draft_content.json`` 与 ``draft_info.json`` 双文件状态。

    校验项(全部满足才返回 ``(True, "ok")``):
    1. 两个文件均存在
    2. 两个文件都是合法 JSON(``json.loads`` 成功)
    3. 两个文件**反序列化后深度相等**

    Args:
        draft_dir: 草稿目录。
        reader: 可注入的 JSON 解析函数;默认 stdlib :func:`json.loads`(便于
            CI 在 Linux 上跑,不依赖 Windows-only 的 ``pyJianYingDraft`` 解析)。

    Returns:
        ``(ok, reason)`` 元组 — ``ok=True`` 表示通过;``ok=False`` 时 ``reason``
        是简短诊断字符串(供上层写入 ``state["error_log"]`` 或 raise 时携带)。
    """
    if reader is None:
        reader = JSON_LOADER

    draft_dir = Path(draft_dir)
    content_file = draft_dir / "draft_content.json"
    info_file = draft_dir / "draft_info.json"

    if not content_file.exists():
        return False, "missing_draft_content.json"
    if not info_file.exists():
        return False, "missing_draft_info.json"

    try:
        content_text = content_file.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"read_draft_content_failed:{exc}"
    try:
        info_text = info_file.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"read_draft_info_failed:{exc}"

    try:
        content_obj = reader(content_text)
    except Exception as exc:  # noqa: BLE001 - 任意 JSON 异常都视为不合法
        return False, f"draft_content_not_json:{type(exc).__name__}:{exc}"
    try:
        info_obj = reader(info_text)
    except Exception as exc:  # noqa: BLE001
        return False, f"draft_info_not_json:{type(exc).__name__}:{exc}"

    if content_obj != info_obj:
        return False, "draft_content_info_mismatch"

    return True, "ok"


__all__ = ["verify_draft_loadable"]
