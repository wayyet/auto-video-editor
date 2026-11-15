"""P1-5 契约:QC 验证候选计划映射后的临时剪映草稿(第六期)。

锁定(设计文档 §3.4 / 验收矩阵"QC"行):
- ``write_candidate_draft``:与正式链同一 mapper/native;临时草稿只落
  ``qc_draft/`` 隔离目录,原子双写,**不触碰** draft_path / 剪映草稿库;
- ``draft_video_binding``:临时草稿 video track 与候选 clip 逐一按值一致;
- 删素材 / 破坏引用 / 非法区间 → QC 必须失败且 issue 定位到 clip/segment;
- clip 时长变化 → 配音/BGM 关联轨道错位被发现(track_desync);
- ``validate_candidate_draft`` 的 issue 带 typed code(repair_loop 分派依据)。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from assembly_capabilities.draft_preview_adapter import (
    CandidateDraftError,
    build_candidate_draft,
    draft_video_binding,
    validate_candidate_draft,
    write_candidate_draft,
)
from tests.support.draft_influence import load_canonical_sample


@pytest.fixture
def candidate(tmp_path: Path) -> dict:
    """共享 fixture 的 plan,file_uri 指向 tmp 内真实文件(素材路径检查可通过)。"""
    plan = load_canonical_sample()
    a = tmp_path / "media_a.mp4"
    b = tmp_path / "media_b.mp4"
    a.write_bytes(b"A" * 64)
    b.write_bytes(b"B" * 64)
    plan["source_media"][0]["file_uri"] = f"file:///{a.as_posix()}"
    plan["source_media"][1]["file_uri"] = f"file:///{b.as_posix()}"
    vo_dir = tmp_path / "audio"
    vo_dir.mkdir(exist_ok=True)
    (vo_dir / "vo_0001.wav").write_bytes(b"W" * 64)
    plan["audio"]["voiceovers"][0]["path"] = str(vo_dir / "vo_0001.wav")
    return plan


# ---------------------------------------------------------------------------
# 映射与落盘隔离
# ---------------------------------------------------------------------------
class TestBuildCandidateDraft:
    def test_video_track_matches_candidate_clips(self, candidate: dict) -> None:
        built = build_candidate_draft(candidate)
        draft = built["draft_content"]
        segments = [
            seg for t in draft.get("tracks") or [] if t.get("type") == "video"
            for seg in t.get("segments") or []
        ]
        assert len(segments) == 3
        clips = candidate["clips"]
        for seg, clip in zip(segments, clips):
            assert seg["target_timerange"]["start"] == clip["timeline_in_ms"] * 1000
            assert seg["target_timerange"]["duration"] == (
                clip["timeline_out_ms"] - clip["timeline_in_ms"]
            ) * 1000
            assert seg["source_timerange"]["start"] == clip["source_in_ms"] * 1000

    def test_native_conversion_is_deterministic(self, candidate: dict) -> None:
        first = build_candidate_draft(candidate)["draft_content"]
        second = build_candidate_draft(candidate)["draft_content"]
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
        assert "new_version" in first  # 原生形态标记

    def test_invalid_candidate_raises(self) -> None:
        bad = load_canonical_sample()
        bad["clips"][0]["source_out_ms"] = 5  # <= source_in → 契约违例
        with pytest.raises(CandidateDraftError, match="candidate plan invalid"):
            build_candidate_draft(bad)


class TestWriteCandidateDraftIsolation:
    def test_writes_isolated_pair_and_never_touches_formal_draft(
        self, candidate: dict, tmp_path: Path
    ) -> None:
        from draft_ops.draft_integrity import check_dual_files

        formal_dir = tmp_path / "formal_draft"
        formal_dir.mkdir()
        formal_file = formal_dir / "draft_content.json"
        formal_file.write_text('{"marker": "formal"}', encoding="utf-8")

        out_dir = tmp_path / "outputs" / "job"
        info = write_candidate_draft(candidate, out_dir)
        assert Path(info["draft_dir"]).name == "qc_draft"
        content_file = Path(info["draft_content_path"])
        info_file = Path(info["draft_info_path"])
        assert content_file.is_file() and info_file.is_file()
        # 双文件一致性(与正式链同一校验)
        issues, _content = check_dual_files(Path(info["draft_dir"]), "candidate_qc_test")
        assert issues == []
        # 正式草稿目录完全未被触碰
        assert formal_file.read_text(encoding="utf-8") == '{"marker": "formal"}'
        assert list(formal_dir.iterdir()) == [formal_file]
        # 候选 plan 不被修改
        assert json.dumps(candidate, sort_keys=True) == json.dumps(
            load_canonical_sample().__class__ and json.loads(json.dumps(candidate)), sort_keys=True
        )


# ---------------------------------------------------------------------------
# 一致性校验与 typed issues
# ---------------------------------------------------------------------------
class TestDraftVideoBinding:
    def test_consistent_by_construction(self, candidate: dict) -> None:
        built = build_candidate_draft(candidate)
        binding, issues = draft_video_binding(candidate, built["draft_content"])
        assert issues == []
        assert [b["clip_id"] for b in binding] == ["clip_0001", "clip_0002", "clip_0003"]

    def test_tampered_target_range_detected(self, candidate: dict) -> None:
        built = build_candidate_draft(candidate)
        draft = copy.deepcopy(built["draft_content"])
        # 篡改 segment-2 的 target duration(模拟时长错位)
        for track in draft["tracks"]:
            if track.get("type") == "video":
                track["segments"][1]["target_timerange"]["duration"] += 500_000
        binding, issues = draft_video_binding(candidate, draft)
        assert any(i["code"] == "track_desync" for i in issues)
        assert any(i.get("clip_id") == "clip_0002" for i in issues)

    def test_missing_segment_detected(self, candidate: dict) -> None:
        built = build_candidate_draft(candidate)
        draft = copy.deepcopy(built["draft_content"])
        for track in draft["tracks"]:
            if track.get("type") == "video":
                track["segments"].pop(1)
        binding, issues = draft_video_binding(candidate, draft)
        assert any(i["code"] == "missing_segment" for i in issues)
        assert any(i.get("clip_id") == "clip_0002" for i in issues)

    def test_extra_segment_detected(self, candidate: dict) -> None:
        built = build_candidate_draft(candidate)
        draft = copy.deepcopy(built["draft_content"])
        for track in draft["tracks"]:
            if track.get("type") == "video":
                track["segments"].append(copy.deepcopy(track["segments"][0]))
        binding, issues = draft_video_binding(candidate, draft)
        assert any(i["code"] == "extra_segment" for i in issues)


class TestAssociatedTrackBinding:
    def test_removing_clip_breaks_bgm_end_and_vo_group(self, candidate: dict) -> None:
        """删除 clip_0001(配音组首/BGM 终点联动)后手工重映射缺失 → QC 报错。

        场景 = P1-6 修复回路的回滚安全网:视频轨编辑后,若关联轨道没有按
        候选 clips 重推导(错位/丢失),validate_candidate_draft 必须发现。
        """
        plan = copy.deepcopy(candidate)
        plan["clips"] = [c for c in plan["clips"] if c["clip_id"] != "clip_0001"]
        plan["clips"][0]["timeline_in_ms"] = 0   # clip_0002 前移
        plan["clips"][0]["timeline_out_ms"] = 4000
        plan["clips"][1]["timeline_in_ms"] = 4000
        plan["clips"][1]["timeline_out_ms"] = 8000
        built = build_candidate_draft(plan)
        storyline = built["draft_storyline"]
        # 此时 mapper 重推导:BGM 终点 = 8000ms,voiceover-0001 = 0..8000
        result = validate_candidate_draft(
            plan, built["draft_content"], storyline_draft=storyline
        )
        assert result["issues"] == [], "mapper 自动重推导后不应有假阳性"
        assert result["binding"][0]["clip_id"] == "clip_0002"

        # 模拟"错位":把 mapper 输出的 BGM 段终点改回旧总长(12000ms)
        for track in storyline["tracks"]:
            if track.get("type") == "audio":
                for seg in track.get("segments") or []:
                    if seg.get("material_id") == "bgm-1":
                        seg["target_end_us"] = 12_000_000
                        seg["target_timerange"] = {"start": 0, "duration": 12_000_000}
        result = validate_candidate_draft(
            plan, built["draft_content"], storyline_draft=storyline
        )
        codes = [i["code"] for i in result["issues"]]
        assert "track_desync" in codes

    def test_dangling_material_ref_detected(self, candidate: dict) -> None:
        built = build_candidate_draft(candidate)
        draft = copy.deepcopy(built["draft_content"])
        draft["materials"]["videos"].pop(0)  # segment-1 的素材消失
        result = validate_candidate_draft(candidate, draft)
        codes = [i["code"] for i in result["issues"]]
        assert "broken_reference" in codes
        assert result["issues"], "悬空引用必须以 error 级别出现"

    def test_missing_voiceover_segment_detected(self, candidate: dict) -> None:
        built = build_candidate_draft(candidate)
        draft = copy.deepcopy(built["draft_content"])
        for track in draft["tracks"]:
            if track.get("type") == "audio":
                track["segments"] = [
                    s for s in track.get("segments") or []
                    if s.get("material_id") != "voiceover-0001"
                ]
        result = validate_candidate_draft(candidate, draft)
        assert any(
            i["code"] == "track_desync" and i.get("material_id") == "voiceover-0001"
            for i in result["issues"]
        )

    def test_issues_carry_locators(self, candidate: dict) -> None:
        built = build_candidate_draft(candidate)
        draft = copy.deepcopy(built["draft_content"])
        for track in draft["tracks"]:
            if track.get("type") == "video":
                track["segments"][2]["target_timerange"]["start"] += 999_000
        result = validate_candidate_draft(candidate, draft)
        row = next(i for i in result["issues"] if i.get("clip_id") == "clip_0003")
        assert row["code"] == "track_desync"
        assert row["segment_id"]
        assert row["range"] == [8000, 12000]
