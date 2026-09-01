"""draft_ops.atomic_writer 单测(附件 2.5 节)。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from draft_ops.atomic_writer import atomic_write_draft


def test_atomic_write_happy_path(tmp_path: Path) -> None:
    """正常写入后文件内容正确,无残留临时文件。"""
    draft_file = tmp_path / "draft_content.json"
    content = {"canvas_config": {"width": 1080, "height": 1920}, "tracks": []}
    atomic_write_draft(draft_file, content)

    assert draft_file.exists()
    loaded = json.loads(draft_file.read_text(encoding="utf-8"))
    assert loaded == content
    # 不应残留临时文件
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".draft_tmp_")]
    assert leftovers == []


def test_atomic_write_replace_failure_keeps_target_intact(tmp_path: Path) -> None:
    """os.replace 前抛异常 → 目标未被污染,临时文件已清理。"""
    draft_file = tmp_path / "draft_content.json"
    # 预置一个旧版本
    original = {"old": True}
    draft_file.write_text(json.dumps(original), encoding="utf-8")

    content = {"new": True}

    with patch("draft_ops.atomic_writer.os.replace", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            atomic_write_draft(draft_file, content)

    # 目标应保留旧内容
    loaded = json.loads(draft_file.read_text(encoding="utf-8"))
    assert loaded == original
    # 临时文件应已被清理
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".draft_tmp_")]
    assert leftovers == []


def test_atomic_write_rejects_non_serializable_content(tmp_path: Path) -> None:
    """content 不可 JSON 序列化 → TypeError,不写任何文件。"""
    draft_file = tmp_path / "draft_content.json"
    with pytest.raises(TypeError):
        atomic_write_draft(draft_file, {"bad": set([1, 2, 3])})
    assert not draft_file.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".draft_tmp_")]
    assert leftovers == []
