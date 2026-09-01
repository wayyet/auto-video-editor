"""draft_content.json 加密状态检测。

设计依据(附件 2.2 节):
- 剪映自 v6.0.0 起对本地 draft_content.json 引入 AES 强加密,直接导致
  明文读写工具失效。
- 检测顺序:文件不存在 → NOT_FOUND;能 utf-8 decode + json.loads 成功 →
  PLAINTEXT;否则调用 capcut decrypt 交叉验证。

[TODO: Stage B 开工实测] `capcut decrypt` 的返回码语义需用已知明文 +
已知加密各跑一次核实,目前默认:`returncode == 0` 表示"成功解密"(即
PLAINTEXT 假阳性),非 0 表示"加密"。这是占位约定,固化前必须实测。
"""

from __future__ import annotations

import json
import subprocess
from enum import Enum
from pathlib import Path
from typing import Callable

from config import CAPCUT_DECRYPT_CMD


class DraftStatus(Enum):
    PLAINTEXT = "plaintext"
    ENCRYPTED = "encrypted"
    NOT_FOUND = "not_found"


def default_decrypt_runner(draft_dir: Path) -> int:
    """默认 capcut decrypt 调用方,封装 subprocess 调用以便单测注入 mock。"""
    result = subprocess.run(
        [*CAPCUT_DECRYPT_CMD, str(draft_dir)],
        capture_output=True,
        text=True,
    )
    return result.returncode


def detect_draft_encryption(
    draft_dir: Path,
    *,
    decrypt_runner: Callable[[Path], int] | None = None,
) -> DraftStatus:
    """检测 draft_dir/draft_content.json 的加密状态。

    Args:
        draft_dir: 剪映草稿目录(包含 draft_content.json)。
        decrypt_runner: 可选的 capcut decrypt 包装函数,接收 draft_dir 返回
            returncode。None 时使用默认 subprocess 实现。

    Returns:
        DraftStatus.PLAINTEXT / ENCRYPTED / NOT_FOUND
    """
    draft_file = draft_dir / "draft_content.json"
    if not draft_file.exists():
        return DraftStatus.NOT_FOUND

    raw = draft_file.read_bytes()
    try:
        raw.decode("utf-8")
        json.loads(raw)
        return DraftStatus.PLAINTEXT
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    # 二次确认:调用 capcut decrypt 工具交叉验证
    # 仅在"明文 JSON 判定失败"后触发,避免把"JSON 格式损坏"误判为"加密"
    runner = decrypt_runner if decrypt_runner is not None else default_decrypt_runner
    returncode = runner(draft_dir)
    return DraftStatus.ENCRYPTED if returncode != 0 else DraftStatus.PLAINTEXT
