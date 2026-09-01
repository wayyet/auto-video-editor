"""节点 8 字幕注入单测 — ASR Mock + 样式字段完整性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jy_common.asr_client import set_default_client
from nodes.node_08_add_subtitles import add_subtitles


class _FixedASRClient:
    """返回固定字幕段的 ASR mock。"""

    def __init__(self, segments: list[dict]) -> None:
        self._segments = segments

    def transcribe(self, video_path: str) -> list[dict]:
        return list(self._segments)


@pytest.fixture
def tmp_draft(tmp_path: Path) -> Path:
    """最小可用草稿 — 含 video track,无 texts。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {"videos": [{"id": "v1"}]},
        "tracks": [{"type": "video", "segments": [{"id": "s1", "target_timerange": {"start": 0, "duration": 10_000_000}}]}],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _state(draft_path: Path, video_path: str = "C:/v.mp4") -> dict:
    return {
        "draft_path": str(draft_path),
        "video_input_path": video_path,
        "status_log": [],
        "error_log": [],
    }


def test_add_subtitles_basic(tmp_draft: Path) -> None:
    """ASR 返回 3 段 → materials.texts 应含 3 条,样式字段完整。"""
    set_default_client(_FixedASRClient([
        {"text": "你好", "start_s": 0.0, "end_s": 1.5},
        {"text": "世界", "start_s": 1.5, "end_s": 3.0},
        {"text": "再见", "start_s": 3.0, "end_s": 4.5},
    ]))

    out = add_subtitles(_state(tmp_draft))

    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    texts = draft["materials"]["texts"]
    assert len(texts) == 3
    # 第一条 target_timerange 应是 [0, 1.5s]
    assert texts[0]["target_timerange"]["start"] == 0
    assert texts[0]["target_timerange"]["duration"] == 1_500_000
    # 样式字段完整性(借鉴 jianying-add-subtitles 惯例)
    style = texts[0]["style"]
    assert style["size"] == 5.0
    assert style["bold"] is True
    assert style["align"] == 1
    assert style["border"]["width"] == 40.0
    assert style["transform_y"] == -0.8
    assert texts[0]["track_name"] == "Subtitles"
    assert "node_08_add_subtitles_done" in out["status_log"]


def test_add_subtitles_empty_asr(tmp_draft: Path) -> None:
    """ASR 返回空 → materials.texts 应为空列表。"""
    set_default_client(_FixedASRClient([]))
    out = add_subtitles(_state(tmp_draft))

    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert draft["materials"]["texts"] == []
    assert "node_08_add_subtitles_done" in out["status_log"]


def test_add_subtitles_uses_snapshot2(tmp_path: Path, tmp_draft: Path) -> None:
    """若 state.snapshot2_path 存在,优先从 snapshot2 读取。"""
    # 构造 snapshot2 文件
    snap2_dir = tmp_path / "snapshots" / "snapshot2"
    snap2_dir.mkdir(parents=True)
    snap2_path = snap2_dir / "draft_content.json"
    snap2_draft = {"canvas_config": {"width": 1080, "height": 1920}, "materials": {}, "tracks": []}
    snap2_path.write_text(json.dumps(snap2_draft), encoding="utf-8")

    set_default_client(_FixedASRClient([{"text": "A", "start_s": 0, "end_s": 1}]))
    state = _state(tmp_draft)
    state["snapshot2_path"] = str(snap2_path)
    out = add_subtitles(state)

    # 写入的是 draft_path(应保留原有内容);读取的是 snapshot2
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert len(draft["materials"]["texts"]) == 1