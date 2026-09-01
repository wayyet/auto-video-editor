"""阶段 B:draft_content.json 加密检测、版本策略、原子写入。

本包为**纯函数库**,无 LangGraph 依赖,可独立单测。
"""

from draft_ops.atomic_writer import atomic_write_draft
from draft_ops.encryption_detector import (
    DraftStatus,
    default_decrypt_runner,
    detect_draft_encryption,
)
from draft_ops.version_strategy import VersionStrategy, resolve_strategy

__all__ = [
    "DraftStatus",
    "VersionStrategy",
    "atomic_write_draft",
    "default_decrypt_runner",
    "detect_draft_encryption",
    "resolve_strategy",
]
