"""节点单测共享 fixture / helper。

fixtures (auto-injected by pytest):
- ``base_state``: 最小可用的 WorkflowState(session_id + video_input_path + outputs_root)
- ``fake_video_path``: 临时 mp4 文件
- ``mock_assembly_capabilities``: monkeypatch 全部 ``assembly_capabilities.*`` 工具
- ``mock_qc_blocking``: 把 ``qc_preview`` 单独换成 blocking 版本(其余用 mock_assembly_capabilities)

helpers (普通函数,可在测试里 from .conftest import ...):
- ``make_media_reports`` / ``make_timeline``
- ``fake_inspect_media`` / ``fake_analyze_media`` / ``fake_speech_transcribe_success`` /
  ``fake_video_ingest_success`` / ``fake_validate_timeline`` /
  ``fake_render_preview_success`` / ``fake_qc_preview_pass`` /
  ``fake_qc_preview_with_blocking_issues`` / ``fake_timeline_diff_success``
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from assembly_capabilities.result import ToolResult


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_media_reports(source: str, candidate_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source": source,
            "probe": {"duration_seconds": 10.0, "size_bytes": 1024, "summary": {"duration_seconds": 10.0}},
            "analysis": {
                "input_path": source,
                "duration_seconds": 10.0,
                "scene_change_count": len(candidate_segments) - 1 if candidate_segments else 0,
                "candidate_segment_count": len(candidate_segments),
                "candidate_segments": candidate_segments,
            },
            "probe_text": f"Inspected media: {source}",
            "analysis_text": "ok",
        }
    ]


def make_timeline(clips: list[dict[str, Any]], source: str) -> dict[str, Any]:
    total = sum((c.get("end", 0) - c.get("start", 0)) for c in clips)
    return {
        "project": {"name": "test"},
        "assets": [{"id": "a1", "path": source, "duration": total, "width": 16, "height": 16, "fps": 30}],
        "sequence": {"duration": total, "fps": 30, "canvas": {"width": 16, "height": 16, "fps": 30}},
        "tracks": [{"type": "video", "name": "v1", "clips": clips}],
    }


def fake_inspect_media(args, ctx):
    return ToolResult(
        text="Inspected media",
        data={"input_path": args.get("input_path"), "duration_seconds": 5.0, "summary": {"duration_seconds": 5.0}},
        artifacts=[],
    )


def fake_analyze_media(args, ctx):
    segments = [
        {"index": 0, "start": 0.0, "end": 2.0, "duration": 2.0, "source": "scene_boundary"},
        {"index": 1, "start": 3.0, "end": 5.0, "duration": 2.0, "source": "scene_boundary"},
    ]
    return ToolResult(
        text="Media analysis written",
        data={
            "input_path": args.get("input_path"),
            "duration_seconds": 5.0,
            "scene_change_count": 1,
            "candidate_segment_count": 2,
            "candidate_segments": segments,
        },
        artifacts=[],
    )


def fake_speech_transcribe_success(args, ctx):
    output_json = args.get("output_json")
    payload = {
        "language": "zh",
        "segments": [{"start": 0, "end": 5, "text": "你好"}],
        "words": [],
        "provider": "external",
        "source_media": args.get("input_path"),
    }
    if output_json:
        Path(output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(output_json).write_text(json.dumps(payload), encoding="utf-8")
    return ToolResult(
        text=f"external transcript written: {output_json}",
        data={
            "provider": "external",
            "channel": "external_passthrough",
            "output_json": output_json,
            "source_media": args.get("input_path"),
            "language": "zh",
            "word_count": 0,
            "segment_count": 1,
        },
        artifacts=[output_json] if output_json else [],
    )


def fake_video_ingest_success(args, ctx):
    output_json = args.get("output_json")
    sheet_path = Path(args["video_path"]).with_suffix(".sheet.jpg")
    payload = {
        "tool": "video_ingest",
        "video_path": args.get("video_path"),
        "media": {"sampled_frames": 4, "duration": 5.0},
        "sheet_paths": [str(sheet_path)],
        "frame_paths": [str(sheet_path)],
        "transcript_text": "",
    }
    if output_json:
        Path(output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(output_json).write_text(json.dumps(payload), encoding="utf-8")
    return ToolResult(
        text="video_ingest completed",
        data={
            "tool": "video_ingest",
            "output_json": output_json,
            "video_path": args.get("video_path"),
            "image_count": 1,
            "sampled_frames": 4,
            "media": payload["media"],
        },
        artifacts=[output_json, str(sheet_path)] if output_json else [str(sheet_path)],
        image_paths=[str(sheet_path)],
    )


def fake_validate_timeline(args, ctx):
    timeline_path = args.get("timeline_path")
    output_json = args.get("output_json")
    report = {
        "status": "pass",
        "timeline_path": timeline_path,
        "timeline_sha256": "abc",
        "clip_count": 2,
        "project_contract": {"ok": True, "missing": []},
        "issues": [],
    }
    if output_json:
        Path(output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(output_json).write_text(json.dumps(report), encoding="utf-8")
    return ToolResult(text=f"Timeline validation pass", data=report, artifacts=[output_json] if output_json else [])


def fake_render_preview_success(args, ctx):
    output_path = args.get("output_path")
    report_path = Path(output_path).with_suffix(".render_report.json")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_bytes(b"\x00" * 1024)
    report = {
        "status": "pass",
        "timeline_path": args.get("timeline_path"),
        "timeline_sha256": "abc",
        "output_path": output_path,
        "output_sha256": "fake-sha",
        "segment_count": 1,
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return ToolResult(
        text=f"Preview rendered: {output_path}",
        data=report,
        artifacts=[output_path, str(report_path)],
        video_paths=[output_path],
    )


def fake_qc_preview_pass(args, ctx):
    output_json = args.get("output_json")
    report = {
        "status": "pass",
        "video_path": args.get("video_path"),
        "video_sha256": "fake-sha",
        "has_video_stream": True,
        "has_audio_stream": False,
        "media": {"duration_seconds": 5.0},
        "issues": [],
        "elapsed_seconds": 0.1,
    }
    if output_json:
        Path(output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(output_json).write_text(json.dumps(report), encoding="utf-8")
    return ToolResult(text="QC pass", data=report, artifacts=[output_json] if output_json else [])


def fake_qc_preview_with_blocking_issues(args, ctx):
    output_json = args.get("output_json")
    report = {
        "status": "fail",
        "video_path": args.get("video_path"),
        "has_video_stream": True,
        "media": {"duration_seconds": 5.0},
        "issues": [
            {"severity": "error", "message": "fake blocking issue"},
        ],
        "elapsed_seconds": 0.1,
    }
    if output_json:
        Path(output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(output_json).write_text(json.dumps(report), encoding="utf-8")
    return ToolResult(text="QC fail", data=report, artifacts=[output_json] if output_json else [])


def fake_timeline_diff_success(args, ctx):
    timeline_path = args.get("timeline_path")
    output_json = args.get("output_json")
    Path(timeline_path).parent.mkdir(parents=True, exist_ok=True)
    Path(timeline_path).write_text(
        json.dumps({"metadata": {"repair_marker": True}}),
        encoding="utf-8",
    )
    diff = {
        "timeline_path": timeline_path,
        "applied": True,
        "after_validation_status": "pass",
        "after_validation_issues": [],
        "before_clip_count": 2,
        "after_clip_count": 2,
    }
    if output_json:
        Path(output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(output_json).write_text(json.dumps(diff), encoding="utf-8")
    return ToolResult(
        text="Timeline diff applied",
        data=diff,
        artifacts=[output_json, timeline_path] if output_json else [timeline_path],
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_assembly_capabilities(monkeypatch):
    """monkeypatch 全部 ``assembly_capabilities.*`` 工具,返回上面的 fake 实现。

    注意:节点模块(如 ``nodes/assembly/node_discover_and_probe.py``)在顶部用
    ``from assembly_capabilities.X import Y`` 形式 import —— 这种 import
    把 Y 绑定到节点模块的命名空间。要让 mock 生效,必须同时 patch
    ``assembly_capabilities.X.Y``(源模块属性)与
    ``nodes.assembly.node_xxx.Y``(消费端模块属性)。
    """
    from assembly_capabilities import (
        media_probe, qc_preview, render_preview, speech_asr,
        timeline_ops, visual_observe,
    )
    from nodes.assembly import (
        node_asr_and_visual_observe,
        node_discover_and_probe,
        node_repair_loop,
        node_validate_render_qc,
    )

    # 源模块属性(保险)
    monkeypatch.setattr(media_probe, "inspect_media", fake_inspect_media)
    monkeypatch.setattr(media_probe, "analyze_media", fake_analyze_media)
    monkeypatch.setattr(speech_asr, "speech_transcribe", fake_speech_transcribe_success)
    monkeypatch.setattr(visual_observe, "video_ingest", fake_video_ingest_success)
    monkeypatch.setattr(timeline_ops, "validate_timeline", fake_validate_timeline)
    monkeypatch.setattr(render_preview, "render_preview", fake_render_preview_success)
    monkeypatch.setattr(qc_preview, "qc_preview", fake_qc_preview_pass)
    monkeypatch.setattr(timeline_ops, "timeline_diff", fake_timeline_diff_success)
    # 消费端模块属性(关键:节点模块顶部已 import 过这些名字)
    monkeypatch.setattr(node_discover_and_probe, "inspect_media", fake_inspect_media)
    monkeypatch.setattr(node_discover_and_probe, "analyze_media", fake_analyze_media)
    monkeypatch.setattr(node_asr_and_visual_observe, "speech_transcribe", fake_speech_transcribe_success)
    monkeypatch.setattr(node_asr_and_visual_observe, "video_ingest", fake_video_ingest_success)
    monkeypatch.setattr(node_validate_render_qc, "validate_timeline", fake_validate_timeline)
    monkeypatch.setattr(node_validate_render_qc, "render_preview", fake_render_preview_success)
    monkeypatch.setattr(node_validate_render_qc, "qc_preview", fake_qc_preview_pass)
    monkeypatch.setattr(node_repair_loop, "timeline_diff", fake_timeline_diff_success)


@pytest.fixture
def mock_qc_blocking(monkeypatch):
    """把 ``qc_preview`` 单独换成 blocking 版本。"""
    from assembly_capabilities import qc_preview as qc_module
    from nodes.assembly import node_validate_render_qc

    monkeypatch.setattr(qc_module, "qc_preview", fake_qc_preview_with_blocking_issues)
    monkeypatch.setattr(node_validate_render_qc, "qc_preview", fake_qc_preview_with_blocking_issues)


@pytest.fixture
def fake_video_path(tmp_path):
    p = tmp_path / "input.mp4"
    p.write_bytes(b"\x00" * 1024)
    return p


@pytest.fixture
def base_state(fake_video_path, tmp_path, monkeypatch):
    """最小可用的 WorkflowState。``monkeypatch.chdir(tmp_path)`` 把 outputs_root 重定向。"""
    monkeypatch.chdir(tmp_path)
    return {
        "session_id": "test-job",
        "video_input_path": str(fake_video_path),
        "storyline_outputs_root": str(tmp_path / "outputs" / "test-job"),
        "status_log": [],
        "error_log": [],
    }
