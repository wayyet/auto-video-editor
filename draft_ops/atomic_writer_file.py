"""原子字节流写入(Week 4 新增,对照计划 §4.2)。

与 ``draft_ops.atomic_writer.atomic_write_draft`` 同样遵循"三步法":
1. 同盘 mkstemp — 临时文件必须与目标同盘,os.replace 才能原子
2. fsync — 强制内核把数据写回磁盘
3. os.replace — POSIX/NTFS 都保证原子

用途:封面 PNG、SRT 字幕文件等任意字节流交付物。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_file(path: Path, data: bytes) -> None:
    """以原子方式写入字节流文件(用于封面 PNG / SRT 等交付物)。

    Args:
        path: 目标文件路径(父目录会自动创建)。
        data: 要写入的字节流。

    Raises:
        OSError: 写入或 rename 失败。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 同盘 mkstemp 才能保证后续 os.replace 原子
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=".tmp_",
        suffix=path.suffix,
    )
    tmp_path_obj = Path(tmp_path)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # 操作系统级原子重命名(POSIX/NTFS)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path_obj.exists():
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise