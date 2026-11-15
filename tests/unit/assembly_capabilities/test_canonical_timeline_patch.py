"""P1-4 契约:候选 CanonicalTimeline 构建与 DraftPatch 生成(第六期)。

锁定(设计文档 §3.3 验收):
- 基线 storyline_plan 不被覆盖;候选从基线深拷贝出发;
- manifest 黑帧区间 → 确定性 trim/remove ops(证据可追溯);
- LLM 选中的未入编候选 → select op(素材引用闭合,不虚构);
- 无有效候选 → 保留基线 + no_safe_candidate(不再整段首尾相接);
- ``CanonicalTimeline.model_validate`` 硬门:候选非法 → 节点显式失败;
- ``timeline.json`` = 候选投影(clip 集合与候选一致,供 validate/render/QC 沿用);
- influence trace 记录本节点 contribution(artifact → consumer)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assembly_capabilities.build_timeline import (
    build_candidate_plan_from_media,
    candidate_projection_timeline,
    manifest_to_media_reports,
)
from assembly_capabilities.draft_patch import plan_sha256
from assembly_capabilities.influence_trace import validate_influence_trace
from tests.support.draft_influence import load_canonical_sample


# ---------------------------------------------------------------------------
# fixture 构造
# ---------------------------------------------------------------------------
def _plan_with_media_uris(tmp_path: Path) -> dict:
    """共享 fixture 的 plan,source_media 的 file_uri 指向 tmp 内真实文件。"""
    plan = load_canonical_sample()
    a = tmp_path / "media_a.mp4"
    b = tmp_path / "media_b.mp4"
    a.write_bytes(b"A" * 64)
    b.write_bytes(b"B" * 64)
    plan["source_media"][0]["file_uri"] = f"file:///{a.as_posix()}"
    plan["source_media"][1]["file_uri"] = f"file:///{b.as_posix()}"
    return plan


def _manifest_for(plan: dict, *, black_ranges_by_media: dict[str, list] | None = None) -> list[dict]:
    """plan → P1-2 manifest 形状的 media_reports(manifest_to_media_reports 的输入)。"""
    sources = []
    path_by_id = {}
    from storyline.mapper import file_uri_to_path

    for m in plan["source_media"]:
        path = file_uri_to_path(m["file_uri"])
        path_by_id[m["media_id"]] = path
        sources.append({
            "asset_id": f"asset_{m['media_id'][-4:]}",
            "source_path": path,
            "plan_media_id": m["media_id"],
            "plan_clip_ids": [
                c["clip_id"] for c in plan["clips"] if c["source_media_id"] == m["media_id"]
            ],
            "duration_seconds": m["duration_ms"] / 1000.0,
            "probe_status": "ok",
            "analysis_status": "ok",
            "probe_summary": {"duration_seconds": m["duration_ms"] / 1000.0},
            "analysis_summary": {"duration_seconds": m["duration_ms"] / 1000.0},
            "candidate_segments": [
                {"index": 0, "start": 0.0, "end": m["duration_ms"] / 1000.0,
                 "duration": m["duration_ms"] / 1000.0, "source": "scene_boundary"},
            ],
            "black_ranges": (black_ranges_by_media or {}).get(m["media_id"], []),
            "silence_ranges": [],
            "technical_risks": [],
            "probe_text": "p",
            "analysis_text": "a",
        })
    return {
        "schema_version": 1,
        "job_id": plan["job_id"],
        "base_plan_sha256": plan_sha256(plan),
        "sources": sources,
        "degraded": False,
    }


def _build(monkeypatch, plan: dict, manifest: dict, *, client=None):
    monkeypatch.chdir(Path(__file__).parent)  # 不影响;RunContext 仅 stub 用
    return build_candidate_plan_from_media(
        base_plan=plan,
        base_plan_sha256=plan_sha256(plan),
        media_reports=manifest_to_media_reports(manifest),
        observation_data=None,
        lang="zh",
        client=client,
    )


# ---------------------------------------------------------------------------
# 基线保留 / no_safe_candidate
# ---------------------------------------------------------------------------
def test_no_candidates_keeps_baseline_and_marks_no_safe_candidate(monkeypatch, tmp_path: Path) -> None:
    """无黑帧 + Stub LLM 空选段 → 候选 = 基线,标 no_safe_candidate(不再整段拼接)。"""
    plan = _plan_with_media_uris(tmp_path)
    result = _build(monkeypatch, plan, _manifest_for(plan))

    assert result["no_safe_candidate"] is True
    assert result["validation"]["pass"] is True
    assert [c["clip_id"] for c in result["candidate_plan"]["clips"]] == [
        "clip_0001", "clip_0002", "clip_0003"
    ]
    # 候选内容与基线逐字段一致(基线保留语义)
    assert json.dumps(result["candidate_plan"], sort_keys=True) == json.dumps(
        plan, sort_keys=True
    )
    assert result["patch_report"]["applied_operations"] == []


def test_base_plan_not_mutated(monkeypatch, tmp_path: Path) -> None:
    plan = _plan_with_media_uris(tmp_path)
    snapshot = json.dumps(plan, sort_keys=True)
    manifest = _manifest_for(plan, black_ranges_by_media={"media_0001": [{"start": 0.5, "end": 3.9}]})
    _build(monkeypatch, plan, manifest)
    assert json.dumps(plan, sort_keys=True) == snapshot, "基线 storyline_plan 必须不可变"


# ---------------------------------------------------------------------------
# 确定性证据 ops
# ---------------------------------------------------------------------------
def test_black_overlap_produces_trim_op(monkeypatch, tmp_path: Path) -> None:
    """clip_0001(0-4s)黑帧 0.5-1.5s(覆盖率 25%)→ trim 到最大干净子区间。"""
    plan = _plan_with_media_uris(tmp_path)
    manifest = _manifest_for(plan, black_ranges_by_media={"media_0001": [{"start": 0.5, "end": 1.5}]})
    result = _build(monkeypatch, plan, manifest)

    assert result["deterministic_ops"] >= 1
    trim_ops = [op for op in result["patch"]["operations"] if op["operation"] == "trim"]
    assert len(trim_ops) == 1
    op = trim_ops[0]
    assert op["target_clip_id"] == "clip_0001"
    assert (op["source_in_ms"], op["source_out_ms"]) == (1500, 4000)
    assert op["evidence_refs"], "trim op 必须带黑帧证据"
    # 候选 clip 已应用 trim,且时间轴重排(后继 clip 前移)
    clips = {c["clip_id"]: c for c in result["candidate_plan"]["clips"]}
    assert (clips["clip_0001"]["source_in_ms"], clips["clip_0001"]["source_out_ms"]) == (1500, 4000)
    assert clips["clip_0002"]["timeline_in_ms"] == 2500
    # diff 可追溯
    assert any(d["change"] == "trimmed" and d["clip_id"] == "clip_0001" for d in result["diffs"])


def test_full_black_coverage_produces_remove_op(monkeypatch, tmp_path: Path) -> None:
    """黑帧覆盖 0-4s 全区间(100%)→ remove op,候选少一个 clip。"""
    plan = _plan_with_media_uris(tmp_path)
    manifest = _manifest_for(plan, black_ranges_by_media={"media_0001": [{"start": 0.0, "end": 4.0}]})
    result = _build(monkeypatch, plan, manifest)

    remove_ops = [op for op in result["patch"]["operations"] if op["operation"] == "remove"]
    assert len(remove_ops) == 1
    assert remove_ops[0]["target_clip_id"] == "clip_0001"
    ids = [c["clip_id"] for c in result["candidate_plan"]["clips"]]
    assert ids == ["clip_0002", "clip_0003"]
    assert any(d["change"] == "removed" and d["clip_id"] == "clip_0001" for d in result["diffs"])


# ---------------------------------------------------------------------------
# LLM select op
# ---------------------------------------------------------------------------
def test_llm_selection_appends_new_clip_from_existing_media(monkeypatch, tmp_path: Path) -> None:
    """Stub client 选中一个与既有 clip 不重叠的候选 → select op 追加新 clip。"""
    from storyline_capabilities.vlm_client import StubLLMClient

    plan = _plan_with_media_uris(tmp_path)
    # 候选=整段(0-30s);clip_0001 源区间 0-4s,重叠 4/30 ≈ 13% < 30% → 未入编
    client = StubLLMClient(default_json={
        "selected_segments": [
            {"index": 0, "reason": "开场高光", "beat": "hook", "order": 0},
        ],
    })
    result = _build(monkeypatch, plan, _manifest_for(plan), client=client)

    assert result["llm_ops"] == 1
    select_ops = [op for op in result["patch"]["operations"] if op["operation"] == "select"]
    assert len(select_ops) == 1
    op = select_ops[0]
    assert op["source_media_id"] == "media_0001"
    assert op["extra"]["beat"] == "hook"
    ids = [c["clip_id"] for c in result["candidate_plan"]["clips"]]
    assert ids == ["clip_0001", "clip_0002", "clip_0003", "clip_sel_0001"]
    appended = result["candidate_plan"]["clips"][-1]
    assert (appended["source_in_ms"], appended["source_out_ms"]) == (0, 30000)
    # 候选仍过硬门
    assert result["validation"]["pass"] is True


def test_llm_selection_matching_existing_clip_adds_no_op(monkeypatch, tmp_path: Path) -> None:
    """候选与既有 clip 重叠 >=30% → 视为已入编,不打 select op。"""
    from storyline_capabilities.vlm_client import StubLLMClient

    plan = _plan_with_media_uris(tmp_path)
    manifest = _manifest_for(plan)
    # 把候选改成与 clip_0001 完全一致的区间(0-4s)
    manifest["sources"][0]["candidate_segments"] = [
        {"index": 0, "start": 0.0, "end": 4.0, "duration": 4.0, "source": "scene_boundary"}
    ]
    client = StubLLMClient(default_json={"selected_segments": [{"index": 0, "reason": "已有镜头", "order": 0}]})
    result = _build(monkeypatch, plan, manifest, client=client)
    assert result["llm_ops"] == 0
    assert not any(op["operation"] == "select" for op in result["patch"]["operations"])


# ---------------------------------------------------------------------------
# 硬门与拒绝
# ---------------------------------------------------------------------------
def test_invalid_base_plan_fails_validation_gate(monkeypatch, tmp_path: Path) -> None:
    """基线本身非法(速度护栏违约)→ 候选非法 → 硬门 fail,不产出候选。"""
    plan = _plan_with_media_uris(tmp_path)
    # 把 clip_0001 拉长:0-4s 源 → 0-40s 时间轴(speed 0.1 < 0.5)
    plan["clips"][0]["timeline_out_ms"] = 40000
    plan["clips"][1]["timeline_in_ms"] = 40000
    plan["clips"][1]["timeline_out_ms"] = 44000
    plan["clips"][2]["timeline_in_ms"] = 44000
    plan["clips"][2]["timeline_out_ms"] = 48000
    result = _build(monkeypatch, plan, _manifest_for(plan))
    assert result["validation"]["pass"] is False
    assert "CLIP_SPEED_INVALID" in (result["validation"]["error"] or "")


def test_base_hash_mismatch_rejected(monkeypatch, tmp_path: Path) -> None:
    """state 基线 hash 与 plan 实际 hash 不一致 → PatchError(节点层显式失败)。"""
    plan = _plan_with_media_uris(tmp_path)
    manifest = _manifest_for(plan)
    with pytest.raises(Exception, match="基线漂移|mismatch|drift"):
        build_candidate_plan_from_media(
            base_plan=plan,
            base_plan_sha256="f" * 64,
            media_reports=manifest_to_media_reports(manifest),
            lang="zh",
            client=None,
        )


# ---------------------------------------------------------------------------
# 投影 timeline
# ---------------------------------------------------------------------------
def test_projection_matches_candidate_clips(monkeypatch, tmp_path: Path) -> None:
    plan = _plan_with_media_uris(tmp_path)
    manifest = _manifest_for(plan, black_ranges_by_media={"media_0001": [{"start": 0.5, "end": 1.5}]})
    result = _build(monkeypatch, plan, manifest)
    projection = candidate_projection_timeline(
        result["candidate_plan"], manifest_to_media_reports(manifest)
    )
    assert projection["metadata"]["candidate_projection"] is True
    proj_clips = projection["tracks"][0]["clips"]
    assert [c["id"] for c in proj_clips] == [c["clip_id"] for c in result["candidate_plan"]["clips"]]
    # 秒↔毫秒一致
    cand = {c["clip_id"]: c for c in result["candidate_plan"]["clips"]}
    for row in proj_clips:
        src = cand[row["id"]]
        assert row["start"] * 1000 == src["source_in_ms"]
        assert row["timeline_start"] * 1000 == src["timeline_in_ms"]
        assert row["asset_id"], "投影 clip 必须引用 manifest asset"
    assets_by_id = {a["id"]: a for a in projection["assets"]}
    for row in proj_clips:
        assert row["asset_id"] in assets_by_id


# ---------------------------------------------------------------------------
# 节点级:state 接线 + 产物 + 影响链
# ---------------------------------------------------------------------------
def test_node_candidate_path_writes_artifacts_and_trace(monkeypatch, tmp_path: Path) -> None:
    from nodes.assembly.node_build_timeline import assembly_build_timeline_node

    plan = _plan_with_media_uris(tmp_path)
    manifest = _manifest_for(plan, black_ranges_by_media={"media_0001": [{"start": 0.5, "end": 1.5}]})
    manifest_file = tmp_path / "media.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    state = {
        "session_id": "patch-job",
        "storyline_outputs_root": str(tmp_path / "outputs" / "patch-job"),
        "error_log": [],
        "status_log": [],
        "storyline_plan": plan,
        "assembly_media_artifact": str(manifest_file),
    }
    out = assembly_build_timeline_node(state)

    assert "assembly_build_timeline_done" in out["status_log"]
    assert out["assembly_candidate_plan"]["clips"]
    assert out["assembly_candidate_plan_sha256"]
    assert out["assembly_draft_patch"]["operations"]
    assembly_dir = tmp_path / "outputs" / "patch-job" / "assembly"
    assert (assembly_dir / "candidate_plan.json").is_file()
    assert (assembly_dir / "draft_patch.json").is_file()
    assert (assembly_dir / "timeline.json").is_file()
    # 候选文件能过 CanonicalTimeline 硬门
    from storyline.contract import CanonicalTimeline

    CanonicalTimeline.model_validate(json.loads((assembly_dir / "candidate_plan.json").read_text(encoding="utf-8")))
    # 影响链 trace:契约合法 + 本节点 contribution 指向 candidate_plan
    trace = json.loads((assembly_dir / "influence_trace.json").read_text(encoding="utf-8"))
    assert validate_influence_trace(trace) == []
    node_entries = [c for c in trace["node_contributions"] if c["node_id"] == "assembly_build_timeline"]
    assert node_entries and node_entries[0]["artifact"] == "assembly/candidate_plan.json"
    assert "assembly_validate_render_qc" in node_entries[0]["consumed_by"]


def test_node_without_baseline_falls_back_to_legacy_timeline(
    monkeypatch, tmp_path: Path
) -> None:
    """无 storyline_plan(单元测试兜底)→ 旧独立 timeline 路径,行为不变。"""
    from nodes.assembly.node_build_timeline import assembly_build_timeline_node

    manifest = _manifest_for(_plan_with_media_uris(tmp_path))
    manifest_file = tmp_path / "media.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    state = {
        "session_id": "legacy-job",
        "storyline_outputs_root": str(tmp_path / "outputs" / "legacy-job"),
        "error_log": [],
        "status_log": [],
        "assembly_media_artifact": str(manifest_file),
    }
    out = assembly_build_timeline_node(state)
    assert "assembly_timeline_path" in out
    assert "assembly_candidate_plan" not in out
    timeline = json.loads(Path(out["assembly_timeline_path"]).read_text(encoding="utf-8"))
    assert timeline["tracks"]
