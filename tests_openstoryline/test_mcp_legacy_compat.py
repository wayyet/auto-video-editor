"""旧 MCP 导入路径必须与规范路径指向同一模块对象。"""

from importlib import import_module

import pytest

@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("open_storyline.hooks.chat_middleware", "open_storyline.mcp.hooks.chat_middleware"),
        ("open_storyline.hooks.node_interceptors", "open_storyline.mcp.hooks.node_interceptors"),
        ("open_storyline.register_tools", "open_storyline.mcp.register_tools"),
        ("open_storyline.sampling_handler", "open_storyline.mcp.sampling_handler"),
        ("open_storyline.sampling_requester", "open_storyline.mcp.sampling_requester"),
        ("open_storyline.server", "open_storyline.mcp.server"),
    ],
)
def test_legacy_import_is_canonical_module(legacy: str, canonical: str) -> None:
    assert import_module(legacy) is import_module(canonical)
