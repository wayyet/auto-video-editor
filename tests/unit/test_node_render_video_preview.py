"""noop 治理阶段3(方案A):storyline_render_video 真实 FFmpeg 故事线预览单测。

全路径依赖注入,不碰真 ffmpeg(真渲染由 test_draft_exporter 覆盖):
- 开关关闭 → skip,不写任何路径字段(旁支纪律);
- 开启 + 时间线缺失/非法 JSON/校验失败 → 显式失败报告,不抛异常;
- 开启 + 成功 → 预览草稿双写 preview/draft/ + run_export 被调用 +
  preview_path/report 写入 + renderer 标记;
- run_export 抛 DraftExportError / 任意异常 / 返回 failed → 失败不炸工作流。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nodes.storyline.node_render_video import storyline_render_video_node


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
_TIMELINE: dict[str, Any] = {
    "schema_version": "1.0",
    "job_id": "test",
    "created_at_ms": 0,
    "source_media": [
        {
            "media_id": "m1",
            "file_uri": "file:///x",
            "duration_ms": 5000,
            "media_type": "video",
        }
    ],
    "clips": [
        {
            "clip_id": "c1",
            "source_media_id": "m1",
            "source_in_ms": 0,
            "source_out_ms": 5000,
            "timeline_in_ms": 0,
            "timeline_out_ms": 5000,
        }
    ],
    "audio": {"bgm_ref": None, "voiceover": None},
    "subtitles": {"zh": None, "en": None},
    "options": {
        "enable_ai_transition": False,
        "enable_voiceover": False,
        "max_duration_ms": 5000,
    },
}


def _make_state(outputs_root: Path, timeline_path: str | None = None) -> dict:
    return {
        "session_id": "sess_test",
        "storyline_outputs_root": str(outputs_root),
        "storyline_timeline_plan": timeline_path or "",
        "status_log": [],
        "error_log": [],
    }


def _write_timeline(outputs_root: Path, raw: str) -> str:
    tp = outputs_root / "storyline" / "timeline_plan.json"
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(raw, encoding="utf-8")
    return str(tp)


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("config.STORYLINE_ENABLE_RENDER_SMOKE_TEST", True)


def _ok_export_report(out_path: Path) -> dict[str, Any]:
    return {
        "status": "succeeded",
        "output_path": str(out_path),
        "sha256": "a" * 64,
        "size_bytes": 1234,
        "expected_duration_us": 5_000_000,
        "probed": {
            "duration_seconds": 5.0,
            "width": 1920,
            "height": 1080,
            "video_codec": "h264",
            "audio_codec": None,
            "audio_sample_rate": None,
        },
        "fidelity_warnings": ["1 个视频特效未渲染"],
        "issues": [],
        "ffmpeg_cmd": ["ffmpeg", "-y"],
    }


# ---------------------------------------------------------------------------
# 关闭路径(默认)
# ---------------------------------------------------------------------------
def test_disabled_by_default_writes_no_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认开关关闭 → 只 append _skipped;不写预览字段、不落任何文件。"""
    monkeypatch.setattr("config.STORYLINE_ENABLE_RENDER_SMOKE_TEST", False)

    delta = storyline_render_video_node(_make_state(tmp_path))

    assert any("storyline_render_video_skipped" in t for t in delta["status_log"])
    assert delta.get("storyline_render_preview_path") in (None, "")
    assert delta.get("storyline_render_preview_report") is None
    # 旁支纪律:skip 路径不建目录不落文件
    assert not (tmp_path / "storyline" / "preview").exists()


# ---------------------------------------------------------------------------
# 开启 + 时间线异常 → 显式失败,不抛异常
# ---------------------------------------------------------------------------
def test_enabled_missing_timeline_fails_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    delta = storyline_render_video_node(_make_state(tmp_path, ""))

    assert any("storyline_render_video_failed" in t for t in delta["status_log"])
    report = delta["storyline_render_preview_report"]
    assert report["status"] == "failed"
    assert report["renderer"] == "ffmpeg_storyline_preview"
    assert report["issues"]
    assert delta["error_log"]
    # 失败不写预览路径
    assert delta.get("storyline_render_preview_path") in (None, "")


def test_enabled_invalid_json_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    tp = _write_timeline(tmp_path, "{not-json")
    delta = storyline_render_video_node(_make_state(tmp_path, tp))

    assert any("storyline_render_video_failed" in t for t in delta["status_log"])
    assert "读取时间线 JSON 失败" in delta["storyline_render_preview_report"]["issues"][0]


def test_enabled_schema_violation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    bad = dict(_TIMELINE)
    bad["clips"] = [{"clip_id": "c1"}]  # 缺 source_media_id 等必填
    tp = _write_timeline(tmp_path, json.dumps(bad, ensure_ascii=False))
    delta = storyline_render_video_node(_make_state(tmp_path, tp))

    assert any("storyline_render_video_failed" in t for t in delta["status_log"])
    assert "时间线校验失败" in delta["storyline_render_preview_report"]["issues"][0]


# ---------------------------------------------------------------------------
# 开启 + 成功 → 真映射 + 预览草稿双写 + run_export 被调用
# ---------------------------------------------------------------------------
def test_enabled_success_writes_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    tp = _write_timeline(tmp_path, json.dumps(_TIMELINE, ensure_ascii=False))
    out_path = tmp_path / "storyline" / "preview" / "storyline_preview.mp4"

    captured: dict[str, Any] = {}

    def fake_run_export(draft: dict, path: Path, **_kw: Any) -> dict[str, Any]:
        captured["draft"] = draft
        captured["out_path"] = Path(path)
        return _ok_export_report(out_path)

    monkeypatch.setattr("draft_ops.draft_exporter.run_export", fake_run_export)

    delta = storyline_render_video_node(_make_state(tmp_path, tp))

    # run_export 收到的是 canonical_to_draft 的产物 + 约定输出路径
    assert captured["out_path"] == out_path
    assert isinstance(captured["draft"], dict)
    assert "materials" in captured["draft"] and "tracks" in captured["draft"]

    # 预览草稿已双写(draft_content.json + draft_info.json)
    draft_dir = tmp_path / "storyline" / "preview" / "draft"
    assert (draft_dir / "draft_content.json").is_file()
    assert (draft_dir / "draft_info.json").is_file()

    # state 契约:路径 + 报告 + status 标签
    assert delta["storyline_render_preview_path"] == str(out_path)
    report = delta["storyline_render_preview_report"]
    assert report["status"] == "succeeded"
    assert report["renderer"] == "ffmpeg_storyline_preview"
    assert report["preview_draft_dir"] == str(draft_dir)
    assert report["timeline_plan"] == tp
    assert any("storyline_render_video_done" in t for t in delta["status_log"])
    assert not delta.get("error_log")


def test_success_report_is_json_serializable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    tp = _write_timeline(tmp_path, json.dumps(_TIMELINE, ensure_ascii=False))
    monkeypatch.setattr(
        "draft_ops.draft_exporter.run_export",
        lambda d, p, **kw: _ok_export_report(Path(p)),
    )

    delta = storyline_render_video_node(_make_state(tmp_path, tp))
    blob = json.dumps(
        delta["storyline_render_preview_report"], ensure_ascii=False
    )
    assert "ffmpeg_storyline_preview" in blob


# ---------------------------------------------------------------------------
# 开启 + run_export 失败 → 失败不炸工作流
# ---------------------------------------------------------------------------
def test_run_export_draft_error_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from draft_ops.draft_exporter import DraftExportError

    _enable(monkeypatch)
    tp = _write_timeline(tmp_path, json.dumps(_TIMELINE, ensure_ascii=False))

    def boom(_draft: dict, _path: Path, **_kw: Any) -> dict[str, Any]:
        raise DraftExportError("素材路径不存在")

    monkeypatch.setattr("draft_ops.draft_exporter.run_export", boom)
    delta = storyline_render_video_node(_make_state(tmp_path, tp))

    assert any("storyline_render_video_failed" in t for t in delta["status_log"])
    report = delta["storyline_render_preview_report"]
    assert report["status"] == "failed"
    assert "素材路径不存在" in report["issues"][0]
    assert delta["error_log"]
    assert delta.get("storyline_render_preview_path") in (None, "")


def test_run_export_unexpected_exception_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    tp = _write_timeline(tmp_path, json.dumps(_TIMELINE, ensure_ascii=False))

    def boom(_draft: dict, _path: Path, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("ffmpeg not found")

    monkeypatch.setattr("draft_ops.draft_exporter.run_export", boom)
    delta = storyline_render_video_node(_make_state(tmp_path, tp))

    assert any("storyline_render_video_failed" in t for t in delta["status_log"])
    assert "ffmpeg not found" in (
        delta["storyline_render_preview_report"]["issues"][0]
    )


def test_run_export_failed_status_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_export 返回 failed(渲染失败但不抛)同样走显式失败路径。"""
    _enable(monkeypatch)
    tp = _write_timeline(tmp_path, json.dumps(_TIMELINE, ensure_ascii=False))
    monkeypatch.setattr(
        "draft_ops.draft_exporter.run_export",
        lambda d, p, **kw: {"status": "failed", "issues": ["ffmpeg 渲染失败(rc=1)"]},
    )

    delta = storyline_render_video_node(_make_state(tmp_path, tp))
    report = delta["storyline_render_preview_report"]
    assert report["status"] == "failed"
    assert report["renderer"] == "ffmpeg_storyline_preview"
    assert delta["error_log"]
