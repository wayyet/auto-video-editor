"""P1-2 契约:素材 manifest 与多素材 sidecar(第六期)。

锁定(设计文档 §3.1 / 验收矩阵"素材发现"行):
- ``compute_asset_id``:同名不同目录不冲突;内容/路径变化 → 身份变化;
- ``analyze_media`` 按 ``asset_id`` 命名 probe sidecar(修 F-11 同名覆盖);
- discover 节点:manifest dict 产出(候选明细进正式契约 F-02)、plan 主导
  素材发现 + plan_clip_ids 映射、ToolResult ``[ERROR]`` 不伪装成功(F-12)、
  部分失败 degraded / 全部失败节点失败;
- ``manifest_to_media_reports`` / ``build_timeline_from_paths`` 兼容 manifest。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assembly_capabilities.build_timeline import (
    build_timeline_from_paths,
    manifest_to_media_reports,
)
from assembly_capabilities.media_probe import compute_asset_id
from assembly_capabilities.result import ToolResult
from nodes.assembly.node_discover_and_probe import (
    MEDIA_MANIFEST_SCHEMA_VERSION,
    assembly_discover_and_probe_node,
)


# ---------------------------------------------------------------------------
# 计算资产身份
# ---------------------------------------------------------------------------
def _make_video(tmp_path: Path, name: str, content: bytes = b"\x00" * 64) -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


class TestComputeAssetId:
    def test_same_name_different_dirs_distinct(self, tmp_path: Path) -> None:
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        a = _make_video(d1, "clip.mp4", b"A" * 64)
        b = _make_video(d2, "clip.mp4", b"B" * 64)
        assert compute_asset_id(a) != compute_asset_id(b)

    def test_stable_for_same_file(self, tmp_path: Path) -> None:
        p = _make_video(tmp_path, "clip.mp4")
        assert compute_asset_id(p) == compute_asset_id(p)

    def test_changes_with_mtime(self, tmp_path: Path) -> None:
        import os

        p = _make_video(tmp_path, "clip.mp4")
        before = compute_asset_id(p)
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        assert compute_asset_id(p) != before

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert compute_asset_id(tmp_path / "nope.mp4") is None

    def test_is_16hex(self, tmp_path: Path) -> None:
        p = _make_video(tmp_path, "clip.mp4")
        value = compute_asset_id(p)
        assert len(value) == 16
        int(value, 16)


# ---------------------------------------------------------------------------
# analyze_media probe sidecar 按 asset_id 命名(F-11)
# ---------------------------------------------------------------------------
class TestAnalyzeMediaSidecar:
    def test_asset_id_sidecar_not_overwritten_by_same_stem(self, tmp_path: Path, monkeypatch) -> None:
        """两个不同目录的同名 stem,probe sidecar 互不覆盖。"""
        import shutil
        import subprocess

        from assembly_capabilities.media_probe import analyze_media
        from assembly_capabilities.run_context import RunContext

        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not (ffmpeg and ffprobe):
            pytest.skip("ffmpeg/ffprobe 不可用(环境级跳过,与 require_real_ffmpeg 同口径)")

        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        # 用 ffmpeg 合成两个时长不同的最小 mp4(stem 同名;纯视频即可)
        for d, dur in ((d1, 1.0), (d2, 2.0)):
            proc = subprocess.run(
                [ffmpeg, "-v", "error", "-f", "lavfi",
                 "-i", f"color=c=black:size=64x64:rate=10:duration={dur}",
                 "-t", str(dur), str(d / "clip.mp4")],
                capture_output=True,
            )
            assert proc.returncode == 0, proc.stderr.decode()[:300]

        monkeypatch.chdir(tmp_path)  # RunContext 的 work_dir/output_dir 落 tmp
        ctx = RunContext(session_kind="pipeline")
        for d, asset in ((d1, "asset-a"), (d2, "asset-b")):
            result = analyze_media(
                {"input_path": str(d / "clip.mp4"), "asset_id": asset}, ctx
            )
            assert not result.text.startswith("[ERROR]"), result.text

        sidecar_dir = ctx.work_dir / "analysis"
        a = json.loads((sidecar_dir / "asset-a_media.json").read_text(encoding="utf-8"))
        b = json.loads((sidecar_dir / "asset-b_media.json").read_text(encoding="utf-8"))
        assert abs(a["duration_seconds"] - 1.0) < 0.3
        assert abs(b["duration_seconds"] - 2.0) < 0.3


# ---------------------------------------------------------------------------
# discover 节点:manifest 契约
# ---------------------------------------------------------------------------
def _patch_tools(monkeypatch, *, probe_data, analysis_payload, probe_error=False, analysis_error=False):
    """同时打源模块与消费端节点模块的双绑定(与 nodes/conftest 同纪律)。"""
    from assembly_capabilities import media_probe
    from nodes.assembly import node_discover_and_probe

    def fake_inspect(args, ctx):
        if probe_error:
            return ToolResult(text="[ERROR] ffprobe not found on PATH", data={})
        output_json = args.get("output_json")
        if output_json:
            Path(output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(output_json).write_text(json.dumps(probe_data), encoding="utf-8")
        return ToolResult(
            text=f"Inspected media: {args.get('input_path')}",
            data=dict(probe_data),
            artifacts=[output_json] if output_json else [],
        )

    def fake_analyze(args, ctx):
        output_json = args.get("output_json")
        if output_json:
            Path(output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(output_json).write_text(json.dumps(analysis_payload), encoding="utf-8")
        summary = {
            "input_path": args.get("input_path"),
            "duration_seconds": analysis_payload.get("duration_seconds"),
            "candidate_segment_count": len(analysis_payload.get("candidate_segments") or []),
        }
        if analysis_error:
            return ToolResult(text="[ERROR] analysis failed", data=summary)
        return ToolResult(
            text="Media analysis written",
            data=summary,
            artifacts=[output_json] if output_json else [],
        )

    monkeypatch.setattr(media_probe, "inspect_media", fake_inspect)
    monkeypatch.setattr(media_probe, "analyze_media", fake_analyze)
    monkeypatch.setattr(node_discover_and_probe, "inspect_media", fake_inspect)
    monkeypatch.setattr(node_discover_and_probe, "analyze_media", fake_analyze)


def _analysis_payload(path: str, dur: float, candidates: list[dict]) -> dict:
    return {
        "input_path": path,
        "duration_seconds": dur,
        "scene_threshold": 0.3,
        "scene_change_times": [c["start"] for c in candidates],
        "candidate_segments": candidates,
        "black_ranges": [{"start": 0.0, "end": 0.2, "duration": 0.2}],
        "silence_ranges": [],
    }


def _probe_data(path: str, dur: float) -> dict:
    return {
        "input_path": path,
        "duration_seconds": dur,
        "size_bytes": 1024,
        "summary": {"duration_seconds": dur, "has_video": True, "has_audio": True},
        "technical_risks": [],
    }


def _candidates(dur: float) -> list[dict]:
    return [
        {"index": 0, "start": 0.0, "end": dur / 2, "duration": dur / 2, "source": "scene_boundary"},
        {"index": 1, "start": dur / 2, "end": dur, "duration": dur / 2, "source": "scene_boundary"},
    ]


def _run_discover(monkeypatch, tmp_path: Path, state: dict) -> dict:
    monkeypatch.chdir(tmp_path)
    return assembly_discover_and_probe_node(state)


def _base_state(tmp_path: Path) -> dict:
    return {
        "session_id": "manifest-job",
        "storyline_outputs_root": str(tmp_path / "outputs" / "manifest-job"),
        "error_log": [],
        "status_log": [],
    }


def test_discover_writes_manifest_with_full_candidates(monkeypatch, tmp_path: Path) -> None:
    """三素材连续探测:每个素材的候选明细都存在,manifest 可完整还原区间。"""
    durations = {"a.mp4": 4.0, "b.mp4": 6.0, "c.mp4": 8.0}
    for name in durations:
        _make_video(tmp_path, name, name.encode() * 8)
    state = _base_state(tmp_path)
    state["video_input_path"] = str(tmp_path)

    def fake_inspect(args, ctx):
        name = Path(str(args["input_path"])).name
        output_json = args.get("output_json")
        if output_json:
            Path(output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(output_json).write_text(json.dumps(_probe_data(args["input_path"], durations.get(name, 4.0))), encoding="utf-8")
        return ToolResult(
            text=f"Inspected media: {args['input_path']}",
            data=_probe_data(args["input_path"], durations.get(name, 4.0)),
            artifacts=[output_json] if output_json else [],
        )

    def fake_analyze(args, ctx):
        name = Path(str(args["input_path"])).name
        dur = durations.get(name, 4.0)
        output_json = args.get("output_json")
        payload = _analysis_payload(args["input_path"], dur, _candidates(dur))
        if output_json:
            Path(output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(output_json).write_text(json.dumps(payload), encoding="utf-8")
        return ToolResult(
            text="Media analysis written",
            data={"input_path": args["input_path"], "duration_seconds": dur,
                  "candidate_segment_count": 2},
            artifacts=[output_json] if output_json else [],
        )

    from assembly_capabilities import media_probe
    from nodes.assembly import node_discover_and_probe
    monkeypatch.setattr(media_probe, "inspect_media", fake_inspect)
    monkeypatch.setattr(media_probe, "analyze_media", fake_analyze)
    monkeypatch.setattr(node_discover_and_probe, "inspect_media", fake_inspect)
    monkeypatch.setattr(node_discover_and_probe, "analyze_media", fake_analyze)

    out = _run_discover(monkeypatch, tmp_path, state)

    assert "assembly_discover_done" in out["status_log"]
    manifest = json.loads(Path(out["assembly_media_artifact"]).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == MEDIA_MANIFEST_SCHEMA_VERSION
    assert manifest["source_count"] >= 3
    srcs = {Path(s["source_path"]).name: s for s in manifest["sources"]}
    assert {"a.mp4", "b.mp4", "c.mp4"} <= set(srcs)
    for name, dur in durations.items():
        src = srcs[name]
        assert src["probe_status"] == "ok" and src["analysis_status"] == "ok"
        assert len(src["candidate_segments"]) == 2
        # 候选区间可还原(每素材独立时长)
        assert src["candidate_segments"][0]["end"] == dur / 2
        assert src["black_ranges"] == [{"start": 0.0, "end": 0.2, "duration": 0.2}]
    assert not manifest["degraded"]
    # sidecar 按 asset_id 分目录保存,互不覆盖
    sidecars = list((tmp_path / "outputs" / "manifest-job" / "assembly" / "media_analysis").glob("*.json"))
    assert len(sidecars) >= len(manifest["sources"]) * 2


def test_discover_same_name_files_not_overwritten(monkeypatch, tmp_path: Path) -> None:
    """两个不同目录的同名 clip.mp4:asset_id 不同,产物互不覆盖。"""
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    v1 = _make_video(d1, "clip.mp4", b"A" * 32)
    v2 = _make_video(d2, "clip.mp4", b"B" * 32)

    payloads: dict[str, dict] = {}

    def fake_inspect(args, ctx):
        return ToolResult(
            text=f"Inspected media: {args['input_path']}",
            data=_probe_data(args["input_path"], 5.0),
        )

    def fake_analyze(args, ctx):
        output_json = args.get("output_json")
        payload = _analysis_payload(args["input_path"], 5.0, _candidates(5.0))
        payloads[args["input_path"]] = payload
        if output_json:
            Path(output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(output_json).write_text(json.dumps(payload), encoding="utf-8")
        return ToolResult(text="ok", data={"duration_seconds": 5.0})

    from assembly_capabilities import media_probe
    from nodes.assembly import node_discover_and_probe
    monkeypatch.setattr(media_probe, "inspect_media", fake_inspect)
    monkeypatch.setattr(media_probe, "analyze_media", fake_analyze)
    monkeypatch.setattr(node_discover_and_probe, "inspect_media", fake_inspect)
    monkeypatch.setattr(node_discover_and_probe, "analyze_media", fake_analyze)

    state = _base_state(tmp_path)
    state["video_input_path"] = str(v1)
    state["video_input_paths"] = [str(v1), str(v2)]
    out = _run_discover(monkeypatch, tmp_path, state)

    manifest = json.loads(Path(out["assembly_media_artifact"]).read_text(encoding="utf-8"))
    assert len(manifest["sources"]) == 2
    ids = {s["asset_id"] for s in manifest["sources"]}
    assert len(ids) == 2
    assert ids == {compute_asset_id(v1), compute_asset_id(v2)}


def test_discover_plan_driven_sources_and_clip_map(monkeypatch, tmp_path: Path) -> None:
    """storyline_plan 主导素材发现:素材集=plan 引用;clip 映射与 base hash 落 manifest。"""
    v1 = _make_video(tmp_path, "plan_a.mp4", b"P1" * 16)
    v2 = _make_video(tmp_path, "plan_b.mp4", b"P2" * 16)
    plan = {
        "schema_version": "1.0",
        "job_id": "manifest-job",
        "source_media": [
            {"media_id": "media_0001", "file_uri": f"file:///{v1.as_posix()}", "duration_ms": 5000, "media_type": "video"},
            {"media_id": "media_0002", "file_uri": f"file:///{v2.as_posix()}", "duration_ms": 5000, "media_type": "video"},
        ],
        "clips": [
            {"clip_id": "clip_0001", "source_media_id": "media_0001", "source_in_ms": 0, "source_out_ms": 2000, "timeline_in_ms": 0, "timeline_out_ms": 2000},
            {"clip_id": "clip_0002", "source_media_id": "media_0002", "source_in_ms": 0, "source_out_ms": 2000, "timeline_in_ms": 2000, "timeline_out_ms": 4000},
        ],
    }
    state = _base_state(tmp_path)
    state["storyline_plan"] = plan
    state["video_input_path"] = str(tmp_path / "NOT_IN_PLAN.mp4")  # 不应被采用

    _patch_tools(
        monkeypatch,
        probe_data=_probe_data("x", 5.0),
        analysis_payload=_analysis_payload("x", 5.0, _candidates(5.0)),
    )
    out = _run_discover(monkeypatch, tmp_path, state)

    manifest = json.loads(Path(out["assembly_media_artifact"]).read_text(encoding="utf-8"))
    assert {Path(s["source_path"]).name for s in manifest["sources"]} == {"plan_a.mp4", "plan_b.mp4"}
    by_media = {s["plan_media_id"]: s for s in manifest["sources"]}
    assert by_media["media_0001"]["plan_clip_ids"] == ["clip_0001"]
    assert by_media["media_0002"]["plan_clip_ids"] == ["clip_0002"]
    assert out.get("assembly_base_plan_sha256")
    assert manifest["base_plan_sha256"] == out["assembly_base_plan_sha256"]


def test_discover_error_result_not_marked_success(monkeypatch, tmp_path: Path) -> None:
    """ToolResult [ERROR] / 缺必需字段 → failed + degraded_reason,不伪装成功。"""
    v = _make_video(tmp_path, "bad.mp4")
    state = _base_state(tmp_path)
    state["video_input_path"] = str(v)
    _patch_tools(
        monkeypatch,
        probe_data=_probe_data(str(v), 5.0),
        analysis_payload=_analysis_payload(str(v), 5.0, _candidates(5.0)),
        probe_error=True,
    )
    out = _run_discover(monkeypatch, tmp_path, state)

    manifest = json.loads(Path(out["assembly_media_artifact"]).read_text(encoding="utf-8"))
    src = manifest["sources"][0]
    assert src["probe_status"] == "failed"
    assert src["analysis_status"] == "skipped"
    assert src["degraded_reason"]
    assert "assembly_discover_degraded" not in out["status_log"]
    assert "assembly_discover_failed" in out["status_log"]  # 全部失败 → 节点失败
    assert any("TOOL_EXECUTION_FAILED" in e for e in out["error_log"])


def test_discover_partial_failure_degrades(monkeypatch, tmp_path: Path) -> None:
    """单素材失败:节点降级完成(tag assembly_discover_degraded),失败条目保留。"""
    good = _make_video(tmp_path, "good.mp4", b"G" * 32)
    bad = _make_video(tmp_path, "bad.mp4", b"B" * 32)

    def fake_inspect(args, ctx):
        if "bad.mp4" in str(args["input_path"]):
            return ToolResult(text="[ERROR] File not found", data={})
        return ToolResult(text="ok", data=_probe_data(args["input_path"], 5.0))

    def fake_analyze(args, ctx):
        output_json = args.get("output_json")
        payload = _analysis_payload(args["input_path"], 5.0, _candidates(5.0))
        if output_json:
            Path(output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(output_json).write_text(json.dumps(payload), encoding="utf-8")
        return ToolResult(text="ok", data={"duration_seconds": 5.0})

    from assembly_capabilities import media_probe
    from nodes.assembly import node_discover_and_probe
    monkeypatch.setattr(media_probe, "inspect_media", fake_inspect)
    monkeypatch.setattr(media_probe, "analyze_media", fake_analyze)
    monkeypatch.setattr(node_discover_and_probe, "inspect_media", fake_inspect)
    monkeypatch.setattr(node_discover_and_probe, "analyze_media", fake_analyze)

    state = _base_state(tmp_path)
    state["video_input_path"] = str(good)
    state["video_input_paths"] = [str(good), str(bad)]
    out = _run_discover(monkeypatch, tmp_path, state)

    manifest = json.loads(Path(out["assembly_media_artifact"]).read_text(encoding="utf-8"))
    assert manifest["degraded"] is True
    statuses = {Path(s["source_path"]).name: s["probe_status"] for s in manifest["sources"]}
    assert statuses == {"good.mp4": "ok", "bad.mp4": "failed"}
    assert "assembly_discover_degraded" in out["status_log"]
    assert "assembly_discover_done" in out["status_log"]
    # 失败素材未被静默丢失
    bad_src = next(s for s in manifest["sources"] if s["source_path"].endswith("bad.mp4"))
    assert bad_src["degraded_reason"]


# ---------------------------------------------------------------------------
# manifest 消费端适配
# ---------------------------------------------------------------------------
def test_manifest_to_media_reports_roundtrip() -> None:
    manifest = {
        "schema_version": 1,
        "base_plan_sha256": "a" * 64,
        "sources": [
            {
                "asset_id": "abc123",
                "source_path": "C:/x/a.mp4",
                "duration_seconds": 8.0,
                "probe_status": "ok",
                "analysis_status": "ok",
                "probe_summary": {"has_video": True},
                "analysis_summary": {"scene_change_count": 2},
                "candidate_segments": [{"index": 0, "start": 0.0, "end": 8.0}],
                "black_ranges": [],
                "silence_ranges": [],
                "technical_risks": [],
                "probe_text": "p",
                "analysis_text": "a",
            }
        ],
    }
    reports = manifest_to_media_reports(manifest)
    assert len(reports) == 1
    rep = reports[0]
    assert rep["source"] == "C:/x/a.mp4"
    assert rep["analysis"]["candidate_segments"] == [{"index": 0, "start": 0.0, "end": 8.0}]
    # list 输入原样透传
    assert manifest_to_media_reports([{"source": "s"}]) == [{"source": "s"}]
    assert manifest_to_media_reports(None) == []


def test_build_timeline_from_paths_accepts_manifest(tmp_path: Path) -> None:
    """build_timeline_from_paths 直接吃 manifest dict(P1-2 最小适配)。"""
    manifest = {
        "schema_version": 1,
        "sources": [
            {
                "asset_id": "abc123",
                "source_path": "C:/x/a.mp4",
                "duration_seconds": 10.0,
                "probe_status": "ok",
                "analysis_status": "ok",
                "probe_summary": {"duration_seconds": 10.0},
                "analysis_summary": {"duration_seconds": 10.0},
                "candidate_segments": [
                    {"index": 0, "start": 0.0, "end": 4.0, "duration": 4.0, "source": "scene_boundary"},
                    {"index": 1, "start": 4.0, "end": 8.0, "duration": 4.0, "source": "scene_boundary"},
                ],
                "black_ranges": [],
                "silence_ranges": [],
                "technical_risks": [],
                "probe_text": "p",
                "analysis_text": "a",
            }
        ],
    }
    manifest_file = tmp_path / "media.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    timeline = build_timeline_from_paths(media_artifact=str(manifest_file))
    clips = timeline["tracks"][0]["clips"]
    # stub LLM 客户端不产生合法选段 → fallback 场景边界候选
    assert clips, "manifest 候选应进入 timeline 构建"
    assert timeline["metadata"]["candidate_segments_count"] == 2
