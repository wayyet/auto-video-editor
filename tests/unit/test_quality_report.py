"""2026-11-11 模糊修复阶段5单测 —— 画质诊断交付报告。

设计文档 §4.5:交付前报告须包含原始素材/草稿声明/成片三方 coded+display
尺寸、逐段 source/target/speed、renderer 标记;成片编码/像素格式/视频流
码率/帧率/色彩信息在提供 ``exported_file`` 时补全。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from draft_ops.quality_report import build_quality_report, write_quality_report


def _draft(materials_videos: list[dict], segments: list[dict]) -> dict:
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"videos": materials_videos},
        "tracks": [{"type": "video", "segments": segments}],
    }


def _write_draft(tmp_path: Path, draft: dict) -> Path:
    d = tmp_path / "draft"
    d.mkdir(parents=True, exist_ok=True)
    (d / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False), encoding="utf-8"
    )
    return d


def _media_file(tmp_path: Path) -> str:
    """真实存在的素材路径(报告只对存在的本机文件做探测)。"""
    f = tmp_path / "a.mp4"
    f.write_bytes(b"\x00" * 8)
    return str(f)


def _dims(w: int, h: int, rot: int = 0):
    from video_edit_capabilities.render import VideoDimensions

    return VideoDimensions(
        coded_width=h if rot in (90, 270) else w,
        coded_height=w if rot in (90, 270) else h,
        rotation_degrees=rot,
        display_width=w,
        display_height=h,
        codec_name="hevc",
        pix_fmt="yuv420p10le",
    )


def _fake_probe(w: int, h: int, rot: int = 0):
    return lambda _p: _dims(w, h, rot)


class TestBuildQualityReport:
    def test_healthy_draft_no_warnings(self, tmp_path, monkeypatch) -> None:
        d = _write_draft(
            tmp_path,
            _draft(
                [
                    {
                        "id": "video-1",
                        "material_name": "clip_0001",
                        "path": _media_file(tmp_path),
                        "width": 1080,
                        "height": 1920,
                    }
                ],
                [
                    {
                        "id": "seg-1",
                        "material_id": "video-1",
                        "source_timerange": {"start": 0, "duration": 10_000_000},
                        "target_timerange": {"start": 0, "duration": 10_000_000},
                        "speed": 1.0,
                    }
                ],
            ),
        )
        monkeypatch.setattr("draft_ops.quality_report._probe_dims", _fake_probe(1080, 1920))
        report = build_quality_report(d)
        assert report["warnings"] == []
        assert report["canvas"] == {"width": 1080, "height": 1920, "ratio": 1080 / 1920}
        assert report["renderer"] == "jianying_manual"
        assert report["materials"][0]["probe"]["display_width"] == 1080
        assert report["segments"][0]["speed_consistent"] is True

    def test_declared_size_mismatch_warns(self, tmp_path, monkeypatch) -> None:
        """声明 1920x1080(实机事故值)但显示方向 1080x1920 → 必须告警。"""
        d = _write_draft(
            tmp_path,
            _draft(
                [
                    {
                        "id": "video-1",
                        "path": _media_file(tmp_path),
                        "width": 1920,
                        "height": 1080,
                    }
                ],
                [],
            ),
        )
        monkeypatch.setattr("draft_ops.quality_report._probe_dims", _fake_probe(1080, 1920, rot=270))
        report = build_quality_report(d)
        assert any("声明尺寸" in w for w in report["warnings"])
        assert any("旋转元数据" in w for w in report["warnings"])

    def test_stretched_segment_flags_below_min_speed(self, tmp_path, monkeypatch) -> None:
        """实机事故形态:0.883s 源铺 11.593s → CLIP_SPEED_INVALID 告警。"""
        d = _write_draft(
            tmp_path,
            _draft(
                [{"id": "video-1", "path": _media_file(tmp_path)}],
                [
                    {
                        "id": "seg-1",
                        "source_timerange": {"start": 0, "duration": 883_000},
                        "target_timerange": {"start": 0, "duration": 11_593_000},
                        "speed": 1.0,
                    }
                ],
            ),
        )
        monkeypatch.setattr("draft_ops.quality_report._probe_dims", _fake_probe(1080, 1920))
        report = build_quality_report(d)
        assert any("CLIP_SPEED_INVALID" in w for w in report["warnings"])

    def test_speed_contradiction_warns(self, tmp_path, monkeypatch) -> None:
        d = _write_draft(
            tmp_path,
            _draft(
                [{"id": "video-1", "path": _media_file(tmp_path)}],
                [
                    {
                        "id": "seg-1",
                        "source_timerange": {"start": 0, "duration": 10_000_000},
                        "target_timerange": {"start": 0, "duration": 5_000_000},
                        "speed": 1.0,  # 派生值 2.0,声明 1.0 → 矛盾
                    }
                ],
            ),
        )
        monkeypatch.setattr("draft_ops.quality_report._probe_dims", _fake_probe(1080, 1920))
        report = build_quality_report(d)
        assert any("矛盾" in w for w in report["warnings"])

    def test_probe_failure_degrades_without_warning(self, tmp_path, monkeypatch) -> None:
        """探测失败(合成路径/缺 ffprobe)→ probe_error 留痕,不产生告警噪音。"""
        d = _write_draft(
            tmp_path,
            _draft([{"id": "video-1", "path": _media_file(tmp_path)}], []),
        )

        def _boom(_p):
            raise RuntimeError("ffprobe not found")

        monkeypatch.setattr("draft_ops.quality_report._probe_dims", _boom)
        report = build_quality_report(d)
        assert report["materials"][0]["probe"] is None
        assert "probe_error" in report["materials"][0]
        assert report["warnings"] == []

    def test_exported_file_section(self, tmp_path, monkeypatch) -> None:
        """提供成片文件 → exported 段含编码/分辨率/码率/帧率/色彩。"""
        d = _write_draft(tmp_path, _draft([], []))
        exported = tmp_path / "out.mp4"
        exported.write_bytes(b"\x00" * 8)
        monkeypatch.setattr(
            "draft_ops.quality_report._probe_export_file",
            lambda p: {
                "path": str(p),
                "width": 1080,
                "height": 1920,
                "codec_name": "hevc",
                "pix_fmt": "yuv420p10le",
                "video_bit_rate": "9240000",
                "avg_frame_rate": "30/1",
                "color_transfer": "arib-std-b67",
            },
        )
        report = build_quality_report(d, exported_file=exported)
        assert report["exported"]["width"] == 1080
        assert report["exported"]["video_bit_rate"] == "9240000"

    def test_missing_exported_file_warns(self, tmp_path, monkeypatch) -> None:
        d = _write_draft(tmp_path, _draft([], []))
        report = build_quality_report(d, exported_file=tmp_path / "nope.mp4")
        assert report["exported"] is None
        assert any("成片文件不存在" in w for w in report["warnings"])

    def test_unreadable_draft_returns_partial(self, tmp_path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        report = build_quality_report(d)
        assert report["canvas"] is None
        assert any("不可读" in w for w in report["warnings"])


def test_write_quality_report_atomic(tmp_path, monkeypatch) -> None:
    d = _write_draft(tmp_path, _draft([], []))
    monkeypatch.setattr("draft_ops.quality_report._probe_dims", _fake_probe(1080, 1920))
    report = build_quality_report(d)
    out = write_quality_report(d, report)
    assert Path(out).is_file()
    loaded = json.loads(Path(out).read_text(encoding="utf-8"))
    assert loaded["schema"] == "autovideo_quality_report/1"
