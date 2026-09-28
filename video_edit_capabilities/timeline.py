"""精简版 ``timeline.py`` helpers —— 仅 2 个被阶段一工具直接调用的函数。

源端 `video-agent-kit 0.4.3 mcp/ve_tools/timeline.py` 是 1300+ 行的项目时间
轴校验器(含 ``validate_timeline`` / ``check_*`` 系列 / patch grammar 等),本
仓库阶段一只搬出 2 个被 ``video_basic_operation`` 与阶段二 ``subtitle_*``
复用的纯 helpers:

- ``file_sha256``          —— 流式读 ``Path`` 并算 SHA-256,产物写到报告 JSON
- ``media_duration_seconds``—— ffprobe 取时长,失败返回 ``None``(不抛异常,源
  端约定;调用方按需决定是否把它当作 hard error)

剩余 ``validate_timeline_*`` / patch grammar 校验等大型函数本仓库不直接调用,
留待阶段五按需取用;见 ``assembly_capabilities/timeline_ops.py`` 已存的对照实现。

依赖:
- ``.ffproc.run_proc`` —— ffprobe 子进程封装
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import ffproc as _ffproc


def file_sha256(path: Path) -> str:
    """流式读 ``Path`` 并算 SHA-256,大文件不会一次性载入内存。

    原样搬自 ``video-agent-kit 0.4.3 mcp/ve_tools/timeline.py:123``。本仓库
    ``video_basic_operation`` 用它给产出物生成指纹写到 ``*.basic_operation.json``
    报告里,后续节点 fallback 比对 input/output 文件内容(主键 ``output_sha256``)。
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def media_duration_seconds(path: Path) -> float | None:
    """返回视频时长(秒,float),失败 / 非视频流 / ffprobe 不可用 返回 ``None``。

    原样搬自 ``video-agent-kit 0.4.3 mcp/ve_tools/timeline.py:678``。**返回
    None 时上层必须自己决定是否报错** —— ``video_basic_operation.op_trim``
    把 ``None`` 当作 "无法比对" 容忍掉,而 ``ensure_range_within_source`` 是
    必须有数值时显式 ``raise ValueError``。这条边界由调用方负责,不在这层加。
    """
    if not shutil.which("ffprobe"):
        return None
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        proc = _ffproc.run_proc(cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None
