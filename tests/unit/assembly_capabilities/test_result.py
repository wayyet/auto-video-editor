"""``ToolResult`` 数据类的形态测试(无外部依赖)。"""
from __future__ import annotations

from assembly_capabilities.result import ToolResult


def test_toolresult_defaults():
    r = ToolResult(text="hello")
    assert r.text == "hello"
    assert r.data == {}
    assert r.artifacts == []
    assert r.image_paths == []
    assert r.video_paths == []


def test_toolresult_mutable_defaults_isolated():
    """两个 ToolResult 不应共享 data / artifacts 等可变默认值。"""
    r1 = ToolResult(text="a")
    r2 = ToolResult(text="b")
    r1.data["x"] = 1
    r1.artifacts.append("/tmp/a")
    r1.image_paths.append("/tmp/img")
    r1.video_paths.append("/tmp/v")
    assert r2.data == {}
    assert r2.artifacts == []
    assert r2.image_paths == []
    assert r2.video_paths == []


def test_toolresult_text_contains_error_marker():
    r = ToolResult(text="[ERROR] something broke")
    assert r.text.startswith("[ERROR]")
