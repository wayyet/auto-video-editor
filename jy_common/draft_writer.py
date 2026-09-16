"""Week 3 计划文档约定的原子写入入口 — 薄包装 re-export。

依据:`docs/AI视频剪辑自动化工作流_第三周详细实施计划.md` §4.8 示例使用
``atomic_write_draft_json`` 命名 + 该计划文档附录 2.4 节将原子写入规划在
``jy_common/draft_writer.py``。本文件在不破坏既有 12 处
``from draft_ops.atomic_writer import atomic_write_draft`` 调用前提下,新增
计划文档期望的别名函数,让节点 13 等新代码可走"计划文档示例命名"路径。

内部实现仍在 ``draft_ops/atomic_writer.py``(纯函数库,无 LangGraph 依赖)。
Week 4 后如需把实现主体搬到本文件,只需修改 re-export 来源,调用方零改动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 内部权威实现 — 复用,不在本文件重写
from draft_ops.atomic_writer import atomic_write_draft as _atomic_write_draft


def atomic_write_draft_json(draft_file: Path | str, content: dict[str, Any]) -> None:
    """计划文档 §4.8 示例使用的命名 — 等价于 ``atomic_write_draft``。

    Args:
        draft_file: 目标草稿文件路径(Path 或 str)。
        content: 草稿内容,顶层 dict。

    行为契约与 ``draft_ops.atomic_writer.atomic_write_draft`` 完全一致:
    1. 前置 ``json.dumps`` + ``json.loads`` 合法性校验
    2. 同目录 ``tempfile.mkstemp`` 写临时文件
    3. ``flush`` + ``fsync`` 落盘
    4. ``os.replace`` 原子覆盖
    5. 任意步骤失败 → 清理临时文件后 raise,目标文件不被污染
    """
    _atomic_write_draft(Path(draft_file), content)


# 让 ``from jy_common.draft_writer import atomic_write_draft`` 也可用
# (兼容既有 import 风格,不强制改写)
atomic_write_draft = _atomic_write_draft


__all__ = ["atomic_write_draft", "atomic_write_draft_json"]