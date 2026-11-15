"""P1-3 契约:候选级视觉观察成为编辑决策输入(第六期)。

锁定(设计文档 §3.2 / F-04 / 验收矩阵"视觉证据"行):
- ``build_candidate_observation``:每 candidate_id 独立抽帧关联;
  黑帧旗标 = 与 manifest black_ranges 的确定性重叠;失败候选 degraded、
  不可判读 unknown,绝不假装无风险;
- 缓存命中与未命中产出同一契约(cache_signature / cache_reused);
- 签名变化(时间区间不同)→ 重新观察;
- build_timeline 实际消费观察:选段 prompt 注入候选自己的拼贴图、
  clip 携带 evidence_refs、确定性 fallback 对 black_suspect 降权 ——
  同一份媒体候选在两种视觉 fixture 下产生不同排序(死产物检查)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assembly_capabilities import visual_observe as vo
from assembly_capabilities.build_timeline import build_timeline_from_media, manifest_to_media_reports
from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext


# ---------------------------------------------------------------------------
# fixture 构造
# ---------------------------------------------------------------------------
def _manifest(tmp_path: Path, *, black_ranges_a=None) -> dict:
    """两个候选的单素材 manifest(a.mp4);black_ranges_a 注入 a 候选的黑帧区间。"""
    video = tmp_path / "a.mp4"
    video.write_bytes(b"\x00" * 64)
    return {
        "schema_version": 1,
        "job_id": "obs-job",
        "base_plan_sha256": None,
        "sources": [
            {
                "asset_id": "assetA",
                "source_path": str(video),
                "duration_seconds": 10.0,
                "probe_status": "ok",
                "analysis_status": "ok",
                "candidate_segments": [
                    {"index": 0, "start": 0.0, "end": 4.0, "duration": 4.0, "source": "scene_boundary"},
                    {"index": 1, "start": 4.0, "end": 8.0, "duration": 4.0, "source": "scene_boundary"},
                ],
                "black_ranges": black_ranges_a or [],
                "silence_ranges": [],
            }
        ],
        "degraded": False,
    }


def _media_reports(tmp_path: Path) -> list[dict]:
    manifest = _manifest(tmp_path)
    reports = manifest_to_media_reports(manifest)
    # manifest_to_media_reports 会透传 asset_id → candidate_id 关联键成立
    assert reports[0]["asset_id"] == "assetA"
    return reports


def _patch_build_observation(monkeypatch, *, fail_ids: set[str] | None = None, unknown_ids: set[str] | None = None):
    """stub build_video_observation:按 output_json 文件名区分候选,实现真实缓存契约。"""
    fail_ids = fail_ids or set()
    unknown_ids = unknown_ids or set()

    def fake_build(*, ctx, defaults, video_path, output_json, frames_dir, label, prompt, original_time_range):
        candidate_id = output_json.stem.replace("observation_", "")
        if candidate_id in fail_ids:
            return ToolResult(text=f"[ERROR] {label}: ffmpeg exploded")
        output_json.parent.mkdir(parents=True, exist_ok=True)
        signature = vo.observation_signature(
            video_path, defaults, prompt, original_time_range, None, frames_dir
        )
        cached = vo.read_cached_observation(output_json, signature)
        if cached is not None:
            return ToolResult(
                text=f"{label} completed",
                data={"reused": True, "media": cached.get("media") or {}, "image_count": 1},
                artifacts=[str(output_json)],
            )
        # 落真实帧/拼贴文件(read_cached_observation 会校验文件存在)
        frames_dir.mkdir(parents=True, exist_ok=True)
        frame_paths = [frames_dir / "f1.jpg", frames_dir / "f2.jpg"]
        sheet_paths = [frames_dir / "sheet.jpg"]
        for p in [*frame_paths, *sheet_paths]:
            p.write_bytes(b"\xff\xd8fake")
        payload = {
            "tool": label,
            "video_path": str(video_path),
            "frame_paths": [str(p) for p in frame_paths],
            "sheet_paths": [str(p) for p in sheet_paths],
            "cache_signature": signature,
            "media": {"raw_frame_timestamps_sec": [original_time_range[0], original_time_range[1]]},
        }
        output_json.write_text(json.dumps(payload), encoding="utf-8")
        return ToolResult(
            text=f"{label} completed",
            data={"reused": False, "media": payload["media"], "image_count": 1},
            artifacts=[str(output_json)],
        )

    monkeypatch.setattr(vo, "build_video_observation", fake_build)


# ---------------------------------------------------------------------------
# build_candidate_observation
# ---------------------------------------------------------------------------
class TestBuildCandidateObservation:
    def test_per_candidate_evidence_and_black_flags(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        manifest = _manifest(tmp_path, black_ranges_a=[{"start": 1.0, "end": 2.0, "duration": 1.0}])
        _patch_build_observation(monkeypatch)
        out_dir = tmp_path / "assembly"

        obs = vo.build_candidate_observation(
            manifest, RunContext(session_kind="pipeline"), out_dir
        )

        assert obs["candidate_count"] == 2
        assert obs["degraded"] is False
        by_id = {c["candidate_id"]: c for c in obs["candidates"]}
        assert set(by_id) == {"assetA_c000", "assetA_c001"}
        for entry in by_id.values():
            assert entry["status"] == "ok"
            assert entry["sheet_paths"] and entry["frame_paths"]
            assert entry["frame_timestamps"] == [entry["start"], entry["end"]]
        # 黑帧旗标只落在重叠候选 c000(区间 0-4 与黑帧 1-2 重叠)
        assert [f["kind"] for f in by_id["assetA_c000"]["observed_flags"]] == ["black_suspect"]
        assert by_id["assetA_c001"]["observed_flags"] == []
        # 产物文件落盘,且每候选一份观察 JSON
        assert (out_dir / "visual_observation.json").is_file()
        assert (out_dir / "observation_assetA_c000.json").is_file()
        assert (out_dir / "observation_assetA_c001.json").is_file()

    def test_degraded_and_unknown_statuses(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        manifest = _manifest(tmp_path)
        _patch_build_observation(monkeypatch, fail_ids={"assetA_c000"})
        # 追加一个过短候选与一个缺源候选
        manifest["sources"][0]["candidate_segments"].append(
            {"index": 2, "start": 8.0, "end": 8.05, "duration": 0.05, "source": "scene_boundary"}
        )
        manifest["sources"].append(
            {
                "asset_id": "assetB",
                "source_path": str(tmp_path / "missing.mp4"),
                "duration_seconds": 5.0,
                "probe_status": "ok",
                "analysis_status": "ok",
                "candidate_segments": [{"index": 0, "start": 0.0, "end": 3.0, "duration": 3.0}],
                "black_ranges": [],
                "silence_ranges": [],
            }
        )

        obs = vo.build_candidate_observation(
            manifest, RunContext(session_kind="pipeline"), tmp_path / "assembly"
        )

        by_id = {c["candidate_id"]: c for c in obs["candidates"]}
        assert by_id["assetA_c000"]["status"] == "degraded" and by_id["assetA_c000"]["detail"]
        assert by_id["assetA_c001"]["status"] == "ok"
        assert by_id["assetA_c002"]["status"] == "unknown" and "too short" in by_id["assetA_c002"]["detail"]
        assert by_id["assetB_c000"]["status"] == "unknown" and "missing" in by_id["assetB_c000"]["detail"]
        assert obs["degraded"] is True

    def test_cache_hit_same_contract_and_signature_change_reobserves(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """缓存命中:同契约 + reused=True;时间区间变化 → 签名变化 → 重抽。"""
        monkeypatch.chdir(tmp_path)
        manifest = _manifest(tmp_path)
        ctx = RunContext(session_kind="pipeline")
        _patch_build_observation(monkeypatch)
        out_dir = tmp_path / "assembly"

        first = vo.build_candidate_observation(manifest, ctx, out_dir)
        second = vo.build_candidate_observation(manifest, ctx, out_dir)
        assert first["degraded"] is False
        assert second["degraded"] is False
        for c1, c2 in zip(first["candidates"], second["candidates"]):
            assert c1["cache_signature"] == c2["cache_signature"]
            assert c2["cache_reused"] is True and c1["cache_reused"] is False

        # 改候选区间 → observation_signature 含 original_time_range → 重抽
        changed = _manifest(tmp_path)
        changed["sources"][0]["candidate_segments"][0]["end"] = 3.0
        third = vo.build_candidate_observation(changed, ctx, out_dir)
        assert third["candidates"][0]["cache_reused"] is False
        assert third["candidates"][0]["end"] == 3.0


# ---------------------------------------------------------------------------
# build_timeline 消费观察(死产物检查)
# ---------------------------------------------------------------------------
def _observation(*, black_first: bool) -> dict:
    """两种视觉 fixture:black_first=True 时候选 0 有 black_suspect,否则候选 1 有。"""
    flag0 = [{"kind": "black_suspect", "range": {"start": 1.0, "end": 2.0}}] if black_first else []
    flag1 = [{"kind": "black_suspect", "range": {"start": 5.0, "end": 6.0}}] if not black_first else []
    return {
        "schema_version": 1,
        "candidates": [
            {
                "candidate_id": "assetA_c000",
                "status": "ok",
                "observed_flags": flag0,
                "sheet_paths": [f"C:/obs/sheet_c000_{'black' if black_first else 'clean'}.jpg"],
                "frame_paths": ["C:/obs/f_c000.jpg"],
            },
            {
                "candidate_id": "assetA_c001",
                "status": "ok",
                "observed_flags": flag1,
                "sheet_paths": [f"C:/obs/sheet_c001_{'clean' if black_first else 'black'}.jpg"],
                "frame_paths": ["C:/obs/f_c001.jpg"],
            },
        ],
        "candidate_count": 2,
        "degraded": False,
    }


def test_fallback_ranking_changes_with_visual_fixture(tmp_path: Path) -> None:
    """同一份媒体候选 + 两种视觉 fixture → 确定性 fallback 排序不同。"""
    reports = _media_reports(tmp_path)

    tl_black_first = build_timeline_from_media(
        media_reports=reports, observation_data=_observation(black_first=True), lang="zh"
    )
    tl_clean_first = build_timeline_from_media(
        media_reports=reports, observation_data=_observation(black_first=False), lang="zh"
    )

    order_black = [c.get("candidate_id") for c in tl_black_first["tracks"][0]["clips"]]
    order_clean = [c.get("candidate_id") for c in tl_clean_first["tracks"][0]["clips"]]
    assert order_black == ["assetA_c001", "assetA_c000"], "black_suspect 候选应降权到尾部"
    assert order_clean == ["assetA_c000", "assetA_c001"]
    # 候选有观察条目 → visual_evidence_used
    assert tl_black_first["metadata"]["visual_evidence_used"] is True
    assert tl_black_first["metadata"]["black_flagged_selected"] == 1


def test_selection_prompt_injects_candidate_sheets(tmp_path: Path) -> None:
    """选段 prompt 注入候选自己的拼贴图(F-04:不再拿无映射的全片前几张)。"""
    from storyline_capabilities.vlm_client import StubLLMClient

    seen_prompts: list[str] = []

    class _SpyClient:
        def chat(self, *, system_prompt, user_prompt, media=None, json_schema=None,
                 temperature=0.3, top_p=0.9, max_tokens=4096):
            seen_prompts.append(user_prompt)
            # 选候选 1(验证索引还原)
            return json.dumps({"selected_segments": [{"index": 1, "reason": "clean shot", "order": 0}]})

    reports = _media_reports(tmp_path)
    timeline = build_timeline_from_media(
        media_reports=reports,
        observation_data=_observation(black_first=True),
        lang="zh",
        client=_SpyClient(),
    )

    assert seen_prompts, "应发起 LLM 选段"
    # prompt 带 candidate_id 与候选自己的 sheet 路径
    assert "assetA_c001" in seen_prompts[0]
    assert "sheet_c001_clean.jpg" in seen_prompts[0]
    assert "assetA_c000" in seen_prompts[0]
    assert "sheet_c000_black.jpg" in seen_prompts[0]
    # LLM 选中的 clip 携带 evidence_refs(candidate_id + 首帧)
    clips = timeline["tracks"][0]["clips"]
    assert len(clips) == 1
    clip = clips[0]
    assert clip["candidate_id"] == "assetA_c001"
    assert clip["selection_basis"] == "llm_selection"
    assert clip["evidence_refs"][:1] == ["assetA_c001"]
    assert "C:/obs/f_c001.jpg" in clip["evidence_refs"]
    assert timeline["metadata"]["candidates_without_evidence"] == 0


def test_fallback_marks_deterministic_basis_and_evidence(tmp_path: Path) -> None:
    """fallback 选段的 clip 标注 deterministic_fallback,并保留候选证据引用。"""
    reports = _media_reports(tmp_path)
    timeline = build_timeline_from_media(
        media_reports=reports,
        observation_data=_observation(black_first=False),
        lang="zh",
        client=None,  # 默认 Stub → 空选段 → fallback
    )
    clips = timeline["tracks"][0]["clips"]
    assert clips
    for clip in clips:
        assert clip["selection_basis"] == "deterministic_fallback"
        assert clip["evidence_refs"][0] == clip["candidate_id"]
        # 每个选段 clip 都能找到观察条目(candidate_id 关联闭合)
    obs_ids = {c["candidate_id"] for c in _observation(black_first=False)["candidates"]}
    assert {c["candidate_id"] for c in clips} <= obs_ids


def test_no_observation_falls_back_cleanly(tmp_path: Path) -> None:
    """观察产物缺失:候选 evidence=None,metadata 如实标注,不虚构证据。"""
    reports = _media_reports(tmp_path)
    timeline = build_timeline_from_media(media_reports=reports, lang="zh")
    clips = timeline["tracks"][0]["clips"]
    assert clips
    for clip in clips:
        assert clip.get("selection_basis") == "deterministic_fallback"
        assert "evidence_refs" not in clip or clip["evidence_refs"] == [clip.get("candidate_id")]
    assert timeline["metadata"]["visual_evidence_used"] is False
    assert timeline["metadata"]["candidates_without_evidence"] == len(clips)
