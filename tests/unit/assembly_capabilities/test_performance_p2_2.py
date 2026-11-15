"""P2-2:性能与资源(探测复用 / 并发上限 / 计时 / 缓存签名版本 / 抽帧预算)单测。

验收对照(计划文档 P2-2):
- 每个素材的 ffprobe 次数符合预期(analyze_media 复用 probe_data 后不重复探测);
- 并发处理不造成 sidecar 覆盖(逐素材独立 asset_id 目录);
- 缓存失效条件完整(工具版本参与签名);
- node_timing.json 含六节点条目;
- 抽帧预算经采样参数进入缓存签名。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import config
from assembly_capabilities import visual_observe as vo_mod
from assembly_capabilities.media_probe import analyze_media
from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext
from assembly_capabilities.timing import (
    record_node_timing,
    summarize_timing,
    timed_assembly_node,
)


# ---------------------------------------------------------------------------
# analyze_media probe 复用(P2-2 ffprobe 去重)
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_media(tmp_path: Path) -> Path:
    p = tmp_path / "media_a.mp4"
    p.write_bytes(b"\x00" * 1024)
    return p


def _stub_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 ffmpeg 扫描桩成确定性空结果(不依赖真实 ffmpeg)。"""
    from assembly_capabilities import media_probe

    monkeypatch.setattr(media_probe, "scan_scene_changes", lambda *a, **k: ([], "stub"))
    monkeypatch.setattr(
        media_probe, "scan_ranges", lambda *a, **k: ([], "stub")
    )


def _probe_data(duration: float = 10.0) -> dict[str, Any]:
    return {
        "input_path": "X:/fixture/media_a.mp4",
        "duration_seconds": duration,
        "summary": {"duration_seconds": duration},
        "technical_risks": [],
    }


def test_analyze_media_reuses_probe_data_no_second_ffprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_media: Path
) -> None:
    _stub_scans(monkeypatch)
    from assembly_capabilities import media_probe

    calls = {"inspect": 0}

    def _counting_inspect(args, ctx):
        calls["inspect"] += 1
        return ToolResult(text="Inspected", data=_probe_data(), artifacts=[])

    monkeypatch.setattr(media_probe, "inspect_media", _counting_inspect)
    monkeypatch.chdir(tmp_path)
    ctx = RunContext(session_kind="pipeline")
    result = analyze_media(
        {"input_path": str(fake_media), "asset_id": "a1", "probe_data": _probe_data()},
        ctx,
    )
    assert not result.text.startswith("[ERROR]"), result.text
    assert calls["inspect"] == 0, "传入 probe_data 后不应再跑 inspect_media(ffprobe 去重)"
    # duration 来自复用的 probe 数据
    assert result.data["duration_seconds"] == 10.0


def test_analyze_media_without_probe_data_falls_back_to_inspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_media: Path
) -> None:
    _stub_scans(monkeypatch)
    from assembly_capabilities import media_probe

    calls = {"inspect": 0}

    def _counting_inspect(args, ctx):
        calls["inspect"] += 1
        return ToolResult(text="Inspected", data=_probe_data(), artifacts=[])

    monkeypatch.setattr(media_probe, "inspect_media", _counting_inspect)
    monkeypatch.chdir(tmp_path)
    ctx = RunContext(session_kind="pipeline")
    result = analyze_media({"input_path": str(fake_media), "asset_id": "a1"}, ctx)
    assert not result.text.startswith("[ERROR]"), result.text
    assert calls["inspect"] == 1, "未传 probe_data 时保持原行为(内部 inspect 一次)"


# ---------------------------------------------------------------------------
# discover 并发(P2-2):顺序保持 + sidecar 不互覆
# ---------------------------------------------------------------------------
def test_discover_parallel_preserves_order_and_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_media: Path
) -> None:
    from nodes.assembly import node_discover_and_probe as node_dp

    monkeypatch.setattr(config, "ASSEMBLY_PROBE_CONCURRENCY", 3)
    # 3 个假素材
    videos = []
    for i in range(3):
        v = tmp_path / f"clip_{i}.mp4"
        v.write_bytes(b"\x00" * 256)
        videos.append(v)
    plan = {
        "source_media": [
            {
                "media_id": f"media_{i:04d}",
                "file_uri": f"file:///{str(v).replace(chr(92), '/')}",
                "duration_ms": 10000,
            }
            for i, v in enumerate(videos, start=1)
        ],
        "clips": [],
    }
    monkeypatch.setattr(node_dp, "inspect_media", lambda args, ctx: ToolResult(
        text="Inspected",
        data={
            "input_path": str(args.get("input_path")),
            "duration_seconds": 10.0,
            "summary": {"duration_seconds": 10.0},
        },
        artifacts=[],
    ))

    def _fake_analyze(args, ctx):
        output_json = args.get("output_json")
        report = {
            "input_path": args.get("input_path"),
            "duration_seconds": 10.0,
            "candidate_segments": [{"index": 0, "start": 0.0, "end": 10.0}],
            "scene_change_times": [],
            "black_ranges": [],
            "silence_ranges": [],
        }
        if output_json:
            Path(output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(output_json).write_text(json.dumps(report), encoding="utf-8")
        return ToolResult(text="analyzed", data={"candidate_segment_count": 1}, artifacts=[])

    monkeypatch.setattr(node_dp, "analyze_media", _fake_analyze)
    # analyze 内部复用 probe_data(P2-2):校验 discover 确实传了 probe_data
    captured: list[dict] = []

    def _capturing_analyze(args, ctx):
        captured.append(dict(args))
        return _fake_analyze(args, ctx)

    monkeypatch.setattr(node_dp, "analyze_media", _capturing_analyze)

    state = {
        "session_id": "p22-parallel",
        "video_input_path": str(fake_media),
        "storyline_plan": plan,
        "storyline_outputs_root": str(tmp_path / "outputs" / "p22-parallel"),
        "error_log": [],
        "status_log": [],
    }
    out = node_dp.assembly_discover_and_probe_node(state)
    assert out["assembly_media_artifact"]
    manifest = json.loads(Path(out["assembly_media_artifact"]).read_text(encoding="utf-8"))
    sources = manifest["sources"]
    assert [s["source_path"] for s in sources] == [str(v) for v in videos], "并发结果顺序必须与计划一致"
    assert all(s["probe_status"] == "ok" for s in sources)
    # 逐素材 sidecar 不互覆
    sidecars = sorted(
        p.name for p in (tmp_path / "outputs" / "p22-parallel" / "assembly" / "media_analysis").glob("*.json")
    )
    assert len([s for s in sidecars if not s.endswith("_probe.json")]) == 3
    # P2-2:analyze 收到 probe_data(ffprobe 去重接线)
    assert captured and all("probe_data" in a for a in captured)


# ---------------------------------------------------------------------------
# 计时(node_timing.json)
# ---------------------------------------------------------------------------
def test_record_node_timing_appends_and_summarizes(tmp_path: Path) -> None:
    path = record_node_timing(
        tmp_path,
        node_id="assembly_discover_and_probe",
        total_seconds=1.23456,
        stages={"probe_seconds": 0.5, "analyze_seconds": 0.7},
        extra={"source_count": 2},
    )
    assert path
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert len(data["entries"]) == 1
    assert data["entries"][0]["node_id"] == "assembly_discover_and_probe"
    assert data["entries"][0]["stages"]["probe_seconds"] == 0.5
    # 追加第二条
    record_node_timing(tmp_path, node_id="assembly_visual_observe", total_seconds=0.4)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert len(data["entries"]) == 2
    summary = summarize_timing(path)
    assert "assembly_discover_and_probe" in summary
    assert "probe_seconds=0.5" in summary


def test_timed_assembly_node_decorator_records_entry(tmp_path: Path) -> None:
    @timed_assembly_node("assembly_build_timeline")
    def _dummy(state):  # noqa: ANN001 — 节点签名
        return {"status_log": ["done"]}

    state = {"storyline_outputs_root": str(tmp_path / "outputs" / "job-x")}
    out = _dummy(state)
    assert out["status_log"] == ["done"]
    data = json.loads(
        (tmp_path / "outputs" / "job-x" / "assembly" / "node_timing.json").read_text(
            encoding="utf-8"
        )
    )
    assert data["entries"][0]["node_id"] == "assembly_build_timeline"
    assert data["entries"][0]["total_seconds"] >= 0


# ---------------------------------------------------------------------------
# 缓存签名:工具版本参与 + 抽帧预算进采样参数
# ---------------------------------------------------------------------------
def test_observation_signature_includes_tool_version(tmp_path: Path, fake_media: Path) -> None:
    defaults = vo_mod.VideoSamplingDefaults()
    kwargs = dict(
        defaults=defaults,
        prompt="p",
        original_time_range=(0.0, 4.0),
        extra_data=None,
        frames_dir=tmp_path / "frames",
    )
    sig_a = vo_mod.observation_signature(fake_media, **kwargs)
    assert sig_a["tool_version"] == vo_mod.VISUAL_OBSERVE_TOOL_VERSION
    monkeypatch_old = sig_a.copy()
    # 版本变化 → 签名变化(旧缓存失效)
    old = vo_mod.VISUAL_OBSERVE_TOOL_VERSION
    try:
        vo_mod.VISUAL_OBSERVE_TOOL_VERSION = "changed"
        sig_b = vo_mod.observation_signature(fake_media, **kwargs)
    finally:
        vo_mod.VISUAL_OBSERVE_TOOL_VERSION = old
    assert sig_b["tool_version"] == "changed"
    assert sig_a != sig_b
    assert monkeypatch_old["sampling"] == sig_b["sampling"]


def test_candidate_observation_applies_frame_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_media: Path
) -> None:
    """预算 clamp 进采样参数(随后自动进缓存签名);0 = 不限制。"""
    manifest = {
        "sources": [
            {
                "asset_id": "a1",
                "source_path": str(fake_media),
                "analysis_status": "ok",
                "black_ranges": [],
                "candidate_segments": [{"index": 0, "start": 0.0, "end": 4.0}],
            }
        ]
    }
    captured: list[vo_mod.VideoSamplingDefaults] = []

    def _fake_build_observation(*, ctx, defaults, video_path, output_json, frames_dir, label, prompt, original_time_range):
        captured.append(defaults)
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(
                {
                    "frame_paths": [],
                    "sheet_paths": [],
                    "cache_signature": "stub",
                    "media": {"raw_frame_timestamps_sec": []},
                }
            ),
            encoding="utf-8",
        )
        return ToolResult(text="observed", data={"reused": False}, artifacts=[])

    monkeypatch.setattr(vo_mod, "build_video_observation", _fake_build_observation)
    ctx = RunContext(session_kind="pipeline")

    monkeypatch.setattr(config, "ASSEMBLY_MAX_FRAMES_PER_CANDIDATE", 50)
    vo_mod.build_candidate_observation(manifest, ctx, tmp_path / "out")
    assert captured[0].max_video_frames == 50, "预算 50 应 clamp 全局 600"

    monkeypatch.setattr(config, "ASSEMBLY_MAX_FRAMES_PER_CANDIDATE", 0)
    vo_mod.build_candidate_observation(manifest, ctx, tmp_path / "out2")
    assert captured[1].max_video_frames == 600, "预算 0 = 不限制,回退全局上限"
