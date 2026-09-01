"""draft_content.json 原子写入。

设计依据(附件 2.4 节,严格对齐"三步法"):
1. 临时文件写入(`tempfile.mkstemp(dir=draft_file.parent)` 保证同盘,rename
   才能原子)
2. 校验 JSON 合法性(前置 `json.loads(serialized)`,写入完成后再 read-back
   不是必要的,序列化本身就能保证合法)
3. 操作系统级重命名覆盖(`os.replace` 在 POSIX 与 NTFS 都是原子的)

任何步骤失败 → 清理临时文件后 raise,确保目标文件不被污染。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


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
