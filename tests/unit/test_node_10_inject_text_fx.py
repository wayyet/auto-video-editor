"""节点 10 花字样式追加单测。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_10_inject_text_fx import inject_text_fx


@pytest.fixture
def tmp_draft_with_texts(tmp_path: Path) -> Path:
    """构造含 2 条字幕的草稿。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {
            "texts": [
                {
                    "id": "t1",
                    "content": "你好",
                    "target_timerange": {"start": 0, "duration": 1_500_000},
                    "style": {"size": 5.0},
                },
                {
                    "id": "t2",
                    "content": "世界",
                    "target_timerange": {"start": 1_500_000, "duration": 1_500_000},
                    "style": {"size": 5.0},
                },
            ],
        },
        "tracks": [],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _state(draft_path: Path) -> dict:
    return {"draft_path": str(draft_path), "status_log": [], "error_log": []}


def test_inject_text_fx_appends_outline_shadow(tmp_path: Path, tmp_draft_with_texts: Path) -> None:
    """每条字幕的 style 应被追加 outline/shadow/entrance_animation 字段。"""
    state = _state(tmp_draft_with_texts)
    out = inject_text_fx(state)

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    for text in draft["materials"]["texts"]:
        style = text["style"]
        assert style.get("outline") is True
        assert style.get("shadow") is True
        assert "entrance_animation" in style
        # 默认 entrance_animation 为 None(Week 4 从 AVAILABLE_ASSETS.md 选定)
        assert style["entrance_animation"] is None
        # 原有 size 字段不应被覆盖
        assert style["size"] == 5.0

    assert "node_10_inject_text_fx_done" in out["status_log"]


def test_inject_text_fx_works_on_empty_texts(tmp_path: Path) -> None:
    """materials.texts 为空时:节点应直接通过,无副作用。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 5_000_000,
        "materials": {},
        "tracks": [],
    }
    p = tmp_path / "draft.json"
    p.write_text(json.dumps(draft), encoding="utf-8")

    state = {"draft_path": str(p), "status_log": [], "error_log": []}
    out = inject_text_fx(state)
    assert "node_10_inject_text_fx_done" in out["status_log"]
    # 草稿未被改写
    draft_after = json.loads(p.read_text(encoding="utf-8"))
    assert "texts" not in draft_after["materials"]