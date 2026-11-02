"""旧导入路径兼容层。

唯一实现位于 :mod:`open_storyline.mcp.server`。此文件不再保存业务实现；导入时直接
将旧模块名映射到规范模块对象，确保公开/私有符号、模块级状态和 monkeypatch
都指向同一份实现。
"""
from __future__ import annotations

import sys

from open_storyline.mcp import server as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
