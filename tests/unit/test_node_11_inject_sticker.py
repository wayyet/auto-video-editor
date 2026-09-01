"""节点 11 贴纸 resource_id 关联单测。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_11_inject_sticker import inject_sticker


@pytest.fixture
def tmp_draft_with_texts(tmp_path: Path) -> Path:
    """构造含字幕 + 视频段的草稿。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {
            "texts": [
                {
                    "id": "t1",
                    "content": "真棒,给你点赞",   # 命中关键词 "赞"
                    "target_timerange": {"start": 0, "duration": 2_000_000},
                },
                {
                    "id": "t2",
                    "content": "继续努力加油",  # 命中关键词 "加油"
                    "target_timerange": {"start": 2_000_000, "duration": 2_000_000},
                },
                {
                    "id": "t3",
                    "content": "没有匹配的关键词",  # 不命中
                    "target_timerange": {"start": 4_000_000, "duration": 2_000_000},
                },
            ],
        },
        "tracks": [{
            "type": "video",
            "segments": [
                {"id": "s1", "target_timerange": {"start": 0, "duration": 6_000_000}},
            ],
        }],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _state(draft_path: Path) -> dict:
    return {"draft_path": str(draft_path), "status_log": [], "error_log": []}


def test_inject_sticker_matches_keywords(tmp_draft_with_texts: Path) -> None:
    """字幕文本含 "赞" / "加油" → 贴纸 material 应被创建。"""
    state = _state(tmp_draft_with_texts)
    out = inject_sticker(state)

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    stickers = draft["materials"]["stickers"]
    # 至少 2 个贴纸被创建(关键词 "赞" + "加油")
    assert len(stickers) >= 2
    # 每个贴纸应含 resource_id + target_timerange
    for sticker in stickers:
        assert sticker.get("resource_id")
        assert "start" in sticker["target_timerange"]
        assert "duration" in sticker["target_timerange"]
    assert "node_11_inject_sticker_done" in out["status_log"]


def test_inject_sticker_preserves_timerange_zero_deviation(tmp_draft_with_texts: Path) -> None:
    """贴纸 timerange 应与对应字幕 timerange 偏差为 0(精确复制)。"""
    state = _state(tmp_draft_with_texts)
    inject_sticker(state)

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    texts = draft["materials"]["texts"]
    stickers = draft["materials"]["stickers"]

    # 取出含关键词的前两条字幕
    target_texts = [t for t in texts if "赞" in t["content"] or "加油" in t["content"]]

    # 每条字幕对应的贴纸 timerange 应精确匹配
    for t in target_texts:
        matching = [
            s for s in stickers
            if s["target_timerange"]["start"] == t["target_timerange"]["start"]
            and s["target_timerange"]["duration"] == t["target_timerange"]["duration"]
        ]
        assert matching, f"未找到 timerange 匹配的贴纸 for {t['content']}"


def test_inject_sticker_handles_missing_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模板库缺失时:不创建贴纸 material,节点通过。"""
    # 把 sticker_resolver 替换为返回 None
    monkeypatch.setattr(
        "nodes.node_11_inject_sticker.resolve_sticker_resource_id",
        lambda _content: None,
    )

    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 5_000_000,
        "materials": {"texts": [{"id": "t1", "content": "赞", "target_timerange": {"start": 0, "duration": 1_000_000}}]},
        "tracks": [{"type": "video", "segments": []}],
    }
    p = tmp_path / "draft.json"
    p.write_text(json.dumps(draft), encoding="utf-8")
    state = {"draft_path": str(p), "status_log": [], "error_log": []}
    out = inject_sticker(state)
    assert "node_11_inject_sticker_done" in out["status_log"]
    # stickers 应为空或不存在
    draft_after = json.loads(p.read_text(encoding="utf-8"))
    assert not draft_after["materials"].get("stickers")