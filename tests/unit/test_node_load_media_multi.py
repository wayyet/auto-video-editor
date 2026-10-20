"""``storyline_load_media`` 多视频支持单测(2026-10 多选导入)。

覆盖:
1. ``_build_inputs``:多路径 → N 个 entry;单路径 → 1 个 entry(行为不变)。
2. ``load_media`` 产物:N 个源视频 → N 条 media(``media_0001`` / ``media_0002`` ...)。
3. 不存在的路径进 ``skipped``,不抛异常(降级纪律)。
"""
from __future__ import annotations

import json
from pathlib import Path

from nodes.storyline.node_load_media import (
    _build_inputs,
    storyline_load_media_node,
)


def _write_fake_video(path: Path) -> Path:
    """造一个最小可用的 ``.mp4``(load_media 只要后缀 + 存在即可)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 1024)
    return path


def test_build_inputs_single_path_unchanged() -> None:
    """单视频:1 个 entry(与改动前一致)。"""
    inputs = _build_inputs({"video_input_path": "E:/a.mp4"})
    assert inputs == [{"path": "E:/a.mp4"}]


def test_build_inputs_multiple_paths() -> None:
    """多视频:每个源路径一个 entry(顺序保持)。"""
    inputs = _build_inputs({
        "video_input_path": "E:/a.mp4",
        "video_input_paths": ["E:/a.mp4", "E:/b.mp4", "E:/c.mp4"],
    })
    assert inputs == [
        {"path": "E:/a.mp4"},
        {"path": "E:/b.mp4"},
        {"path": "E:/c.mp4"},
    ]


def test_load_media_node_produces_multiple_media(tmp_path: Path) -> None:
    """两个源视频 → 产物里有 2 条 media,media_id 递增且不重复。"""
    a = _write_fake_video(tmp_path / "a.mp4")
    b = _write_fake_video(tmp_path / "b.mp4")
    outputs_root = tmp_path / "outputs" / "job-multi"

    out = storyline_load_media_node({
        "session_id": "job-multi",
        "video_input_path": str(a),
        "video_input_paths": [str(a), str(b)],
        "storyline_outputs_root": str(outputs_root),
        "status_log": [],
        "error_log": [],
    })

    artifact = Path(out["storyline_media_artifact"])
    data = json.loads(artifact.read_text(encoding="utf-8"))
    media = data["media"]
    assert len(media) == 2
    assert [m["media_id"] for m in media] == ["media_0001", "media_0002"]
    assert {m["path"] for m in media} == {str(a), str(b)}


def test_missing_path_goes_to_skipped_not_exception(tmp_path: Path) -> None:
    """不存在的路径进 ``skipped``,不抛异常(环境级失败降级)。"""
    a = _write_fake_video(tmp_path / "a.mp4")
    missing = tmp_path / "nope.mp4"
    outputs_root = tmp_path / "outputs" / "job-missing"

    out = storyline_load_media_node({
        "session_id": "job-missing",
        "video_input_path": str(a),
        "video_input_paths": [str(a), str(missing)],
        "storyline_outputs_root": str(outputs_root),
        "status_log": [],
        "error_log": [],
    })

    data = json.loads(Path(out["storyline_media_artifact"]).read_text(encoding="utf-8"))
    assert len(data["media"]) == 1
    assert data["summary"]["skipped"] == 1, "不存在的路径应记进 skipped"
    assert data["summary"]["skipped_items"], "skipped 明细应有 1 条"
