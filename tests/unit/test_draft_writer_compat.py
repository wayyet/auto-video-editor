"""``jy_common.draft_writer`` 薄包装兼容性单测。

覆盖:
(a) ``atomic_write_draft`` 与 ``draft_ops.atomic_writer.atomic_write_draft`` 是同一对象(re-export)。
(b) ``atomic_write_draft_json`` 行为等价于 ``atomic_write_draft``。
(c) ``atomic_write_draft_json`` 接受 str 路径(Path 转换)。
(d) ``atomic_write_draft_json`` 写入失败时,目标文件保持旧内容(原子写入契约继承)。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from draft_ops.atomic_writer import atomic_write_draft as internal_atomic_write
from jy_common.draft_writer import (
    atomic_write_draft,
    atomic_write_draft_json,
)


def test_atomic_write_draft_is_same_object_as_internal() -> None:
    """``jy_common.draft_writer.atomic_write_draft`` 与 ``draft_ops.atomic_writer`` 同对象。"""
    assert atomic_write_draft is internal_atomic_write


def test_atomic_write_draft_json_happy_path(tmp_path: Path) -> None:
    """``atomic_write_draft_json`` 正常写入,内容可被 json.loads 还原。"""
    draft_file = tmp_path / "draft_content.json"
    content = {"canvas_config": {"width": 1080, "height": 1920}, "tracks": []}
    atomic_write_draft_json(draft_file, content)

    assert draft_file.exists()
    assert json.loads(draft_file.read_text(encoding="utf-8")) == content


def test_atomic_write_draft_json_accepts_str_path(tmp_path: Path) -> None:
    """``atomic_write_draft_json`` 接受 str 路径(Path 自动转换)。"""
    draft_file = tmp_path / "draft_content.json"
    atomic_write_draft_json(str(draft_file), {"k": "v"})
    assert json.loads(draft_file.read_text(encoding="utf-8")) == {"k": "v"}


def test_atomic_write_draft_json_failure_keeps_target_intact(tmp_path: Path) -> None:
    """写入失败时,目标文件不被污染(原子写入契约继承)。"""
    draft_file = tmp_path / "draft_content.json"
    original = {"old": True}
    draft_file.write_text(json.dumps(original), encoding="utf-8")

    with patch("draft_ops.atomic_writer.os.replace", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            atomic_write_draft_json(draft_file, {"new": True})

    # 目标应保留旧内容
    assert json.loads(draft_file.read_text(encoding="utf-8")) == original