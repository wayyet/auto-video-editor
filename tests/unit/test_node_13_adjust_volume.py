"""节点 13 占位节点单测 — Week 3 不动草稿,Week 4 实现。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_13_adjust_volume import adjust_volume


@pytest.fixture
def tmp_draft(tmp_path: Path) -> Path:
    """任意草稿文件。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": {},
        "tracks": [],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _state(draft_path: Path) -> dict:
    return {"draft_path": str(draft_path), "status_log": [], "error_log": []}


def test_adjust_volume_placeholder_passes(tmp_draft: Path) -> None:
    """占位节点:仅追加 status_log + 标记 volume_adjusted=False。"""
    state = _state(tmp_draft)
    out = adjust_volume(state)

    assert "node_13_adjust_volume_placeholder_pass" in out["status_log"]
    assert out["volume_adjusted"] is False


def test_adjust_volume_does_not_modify_draft(tmp_draft: Path) -> None:
    """占位实现不应修改 draft_content.json。"""
    before = tmp_draft.read_text(encoding="utf-8")
    adjust_volume(_state(tmp_draft))
    after = tmp_draft.read_text(encoding="utf-8")
    assert before == after


def test_adjust_volume_preserves_existing_status_log(tmp_draft: Path) -> None:
    state = _state(tmp_draft)
    state["status_log"] = ["node_12_done"]
    out = adjust_volume(state)
    assert out["status_log"][0] == "node_12_done"
    assert "node_13_adjust_volume_placeholder_pass" in out["status_log"]