"""draft_ops.atomic_writer_file 单测(Week 4 新增,字节流版)。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from draft_ops.atomic_writer_file import atomic_write_file


def test_atomic_write_file_happy_path(tmp_path: Path) -> None:
    """正常写入后文件字节内容正确,无残留临时文件。"""
    target = tmp_path / "covers" / "cover_16x9_zh.png"
    data = b"\x89PNG\r\n\x1a\nfake-png-bytes-for-test"

    atomic_write_file(target, data)

    assert target.exists()
    assert target.read_bytes() == data
    # 不应残留临时文件
    leftovers = [p for p in target.parent.iterdir() if p.name.startswith(".tmp_")]
    assert leftovers == []


def test_atomic_write_file_creates_parent_dirs(tmp_path: Path) -> None:
    """父目录不存在时应自动创建。"""
    target = tmp_path / "deep" / "nested" / "file.bin"
    atomic_write_file(target, b"hello")
    assert target.exists()
    assert target.read_bytes() == b"hello"


def test_atomic_write_file_replace_failure_keeps_target_intact(tmp_path: Path) -> None:
    """os.replace 前抛异常 → 目标未被污染,临时文件已清理。"""
    target = tmp_path / "file.bin"
    original = b"original-bytes"
    target.write_bytes(original)

    new_data = b"new-bytes"

    with patch("draft_ops.atomic_writer_file.os.replace", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            atomic_write_file(target, new_data)

    # 目标应保留旧内容
    assert target.read_bytes() == original
    # 临时文件应已被清理
    leftovers = [p for p in target.parent.iterdir() if p.name.startswith(".tmp_")]
    assert leftovers == []


def test_atomic_write_file_overwrites_existing(tmp_path: Path) -> None:
    """对已存在文件应能成功覆盖。"""
    target = tmp_path / "existing.bin"
    target.write_bytes(b"v1")
    atomic_write_file(target, b"v2")
    assert target.read_bytes() == b"v2"


def test_atomic_write_file_empty_bytes(tmp_path: Path) -> None:
    """写入空字节也应成功(零字节文件)。"""
    target = tmp_path / "empty.bin"
    atomic_write_file(target, b"")
    assert target.exists()
    assert target.read_bytes() == b""