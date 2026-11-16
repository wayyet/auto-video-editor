"""P0-06 集成(20261117):speed 修复后候选预览真实渲染链路。

不 mock ``render_preview``:ffmpeg 合成素材 → 候选计划(压缩槽位,tts_align
实锤形状)→ ``candidate_projection_timeline`` 投影 → ``validate_timeline``
零 error → ``render_preview`` 实渲 → 预览时长 ≈ sequence.duration
(容差按输出 fps 1-2 帧;渲染器可能补尾帧,放宽到 ±0.15s)。

环境依赖:本机需 ffmpeg/ffprobe(libx264+aac),缺失 → skip
(不得用单测 mock 通过代替真实渲染验收 —— 审查文档 §7.3)。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from assembly_capabilities.build_timeline import candidate_projection_timeline
from assembly_capabilities.render_preview import render_preview
from assembly_capabilities.run_context import RunContext
from assembly_capabilities.timeline_ops import validate_timeline

pytestmark = [
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="本机没有 ffmpeg/ffprobe,无法合成素材与真实渲染",
    ),
]


def _make_clip_with_audio(path: Path, *, seconds: int, hue: int) -> None:
    """ffmpeg 合成带音轨的测试视频(testsrc 调色 + 440Hz 正弦)。"""
    proc = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=25",
            "-f", "lavfi", "-i", f"sine=frequency={hue}:duration={seconds}",
            "-vf", f"hue=h={hue}",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-shortest",
            str(path),
        ],
        capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, f"ffmpeg 合成失败: {proc.stderr[-500:]}"


def _probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, timeout=60, text=True,
    )
    assert proc.returncode == 0, f"ffprobe 失败: {proc.stderr[-300:]}"
    return float(proc.stdout.strip())


def test_compressed_candidate_renders_preview_matching_plan(tmp_path, monkeypatch) -> None:
    """压缩槽位候选(speed≈2×/2.67×)→ 校验零 error + 预览时长≈计划时长。"""
    monkeypatch.chdir(tmp_path)  # RunContext/渲染工作目录全部隔离在 tmp
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    clip_a = media_dir / "a.mp4"
    clip_b = media_dir / "b.mp4"
    _make_clip_with_audio(clip_a, seconds=4, hue=10)
    _make_clip_with_audio(clip_b, seconds=3, hue=200)

    plan = {
        "schema_version": "1.0",
        "job_id": "assembly-preview-it",
        "source_media": [
            {"media_id": "media_0001", "file_uri": clip_a.as_uri(),
             "duration_ms": 4000, "media_type": "video"},
            {"media_id": "media_0002", "file_uri": clip_b.as_uri(),
             "duration_ms": 3000, "media_type": "video"},
        ],
        "clips": [
            # speed=2.0:4000ms 源 → 2000ms 槽位
            {"clip_id": "clip_0001", "source_media_id": "media_0001",
             "source_in_ms": 0, "source_out_ms": 4000,
             "timeline_in_ms": 0, "timeline_out_ms": 2000},
            # speed=2.0:3000ms 源 → 1500ms 槽位
            {"clip_id": "clip_0002", "source_media_id": "media_0002",
             "source_in_ms": 0, "source_out_ms": 3000,
             "timeline_in_ms": 2000, "timeline_out_ms": 3500},
        ],
    }
    reports = [
        {"source": str(clip_a), "asset_id": "asset_a", "probe": {"duration_seconds": 4.0}},
        {"source": str(clip_b), "asset_id": "asset_b", "probe": {"duration_seconds": 3.0}},
    ]

    projection = candidate_projection_timeline(plan, reports)
    for clip in projection["tracks"][0]["clips"]:
        assert clip["speed"] == pytest.approx(2.0)
    assert projection["sequence"]["duration"] == pytest.approx(3.5)

    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps(projection, ensure_ascii=False), encoding="utf-8")

    ctx = RunContext()
    validation = validate_timeline(
        {"timeline_path": str(timeline_path),
         "output_json": str(tmp_path / "timeline_validation.json")},
        ctx,
    )
    assert not validation.text.startswith("[ERROR]"), validation.text
    errors = [i for i in validation.data.get("issues", []) if i.get("severity") == "error"]
    assert not errors, f"speed 修复后校验不应有 error: {errors}"

    preview_path = tmp_path / "preview.mp4"
    render = render_preview(
        {"timeline_path": str(timeline_path), "output_path": str(preview_path)},
        ctx,
    )
    assert not render.text.startswith("[ERROR]"), render.text
    assert preview_path.is_file(), "preview.mp4 必须真实生成"

    duration = _probe_duration(preview_path)
    # 容差:30fps 下 1-2 帧 ≈ 0.067s;渲染器补尾帧再放宽到 ±0.15s
    assert duration == pytest.approx(3.5, abs=0.15), (
        f"预览时长 {duration:.3f}s 应与计划 3.5s 一致(速度语义正确)"
    )
