"""节点 13 占位行为的兼容保留测试 — Week 3 补全后节点 13 是真实实现。

Week 3 末补全后,本文件仅保留"无 audio track 时降级通过"这一兼容路径断言,
其余真实实现覆盖见 ``test_node_13_adjust_volume_real.py``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_13_adjust_volume import adjust_volume


@pytest.fixture
def tmp_draft(tmp_path: Path) -> Path:
    """任意无 audio track 的草稿 — 用于降级路径测试。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": {},
        "tracks": [{"type": "video", "segments": []}],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _state(draft_path: Path) -> dict:
    return {"draft_path": str(draft_path), "status_log": [], "error_log": []}


def test_adjust_volume_no_audio_track_does_not_modify_draft(tmp_draft: Path) -> None:
    """无 audio track:节点 13 不应修改 draft_content.json(降级路径)。"""
    before = tmp_draft.read_text(encoding="utf-8")
    out = adjust_volume(_state(tmp_draft))
    after = tmp_draft.read_text(encoding="utf-8")

    assert before == after, "无 audio track 时草稿不应被修改"
    assert out["volume_adjusted"] is False
    assert any("audio" in e for e in out["error_log"])