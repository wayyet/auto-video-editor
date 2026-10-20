"""集成:多视频导入的**接线**是否贯通(2026-10 多选导入)。

链路:网页【导入视频】→ ``import_video_result.json``(多文件)→
``node_04a`` 回填 ``state["video_input_paths"]`` → 下游
``resolve_video_input_paths`` 拿到 N 个源视频。

同时守住**单视频零行为变更**:只导入 1 个时,``video_input_paths`` 长度 1,
且与旧行为(``video_input_path`` 单值)等���。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
from nodes.node_04a_import_video import (
    import_video,
    import_video_result_filename,
    write_import_result,
)
from nodes.storyline._common import resolve_video_input_paths


def test_multi_file_import_wires_video_input_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """导入 3 个视频 → state["video_input_paths"] 长度 3,下游能读到 3 个。"""
    out_root = tmp_path / "outputs"
    monkeypatch.setattr(config, "storyline_outputs_root", lambda: out_root)

    write_import_result(
        "job-wire",
        {
            "ok": True,
            "web_session_id": "websid",
            "media_id": "media_0001",
            "filename": "a.mov",
            "stored_path": "E:/media/a.mov",
            "media_paths": ["E:/media/a.mov", "E:/media/b.mov", "E:/media/c.mov"],
        },
        root=out_root,
    )
    out = import_video(
        {"session_id": "job-wire", "error_log": []},
        outputs_root=out_root,
        poll_interval_s=0,
        timeout_s=0,
    )

    assert out["import_video_status"] == "imported"
    assert len(out["video_input_paths"]) == 3
    assert resolve_video_input_paths(out) == [
        "E:/media/a.mov", "E:/media/b.mov", "E:/media/c.mov"
    ]
    # 旧字段仍指向第一个(下游单值消费方不受影响)
    assert out["video_input_path"] == "E:/media/a.mov"
    assert out["import_video_media_path"] == "E:/media/a.mov"


def test_single_file_import_keeps_legacy_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只导入 1 个 → video_input_paths 长度 1,且值 == video_input_path。"""
    out_root = tmp_path / "outputs"
    monkeypatch.setattr(config, "storyline_outputs_root", lambda: out_root)

    write_import_result(
        "job-one",
        {
            "ok": True,
            "media_id": "media_0001",
            "filename": "a.mp4",
            "stored_path": "E:/media/a.mp4",
            "media_paths": ["E:/media/a.mp4"],
        },
        root=out_root,
    )
    out = import_video(
        {"session_id": "job-one", "video_input_path": "E:/orig.mp4", "error_log": []},
        outputs_root=out_root,
        poll_interval_s=0,
        timeout_s=0,
    )
    assert out["video_input_paths"] == ["E:/media/a.mp4"]
    assert out["video_input_path"] == "E:/media/a.mp4"
    assert resolve_video_input_paths(out) == ["E:/media/a.mp4"]


def test_result_file_records_all_media_paths(tmp_path: Path) -> None:
    """握手结果文件里含 N 条 ``media_paths``(网页端回写的契约)。"""
    from nodes.node_04a_import_video import build_result_payload, write_import_result

    out_root = tmp_path / "outputs"
    target = write_import_result(
        "job-payload",
        build_result_payload("websid", [
            {"media_id": "media_0001", "filename": "a.mov", "stored_path": "E:/a.mov"},
            {"media_id": "media_0002", "filename": "b.mov", "stored_path": "E:/b.mov"},
        ]),
        root=out_root,
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert len(payload["media_paths"]) == 2
    assert payload["media_paths"] == ["E:/a.mov", "E:/b.mov"]
    # 单值字段(= 第一个)也在,老读取方不会挂
    assert payload["stored_path"] == "E:/a.mov"
    assert target.name == import_video_result_filename
