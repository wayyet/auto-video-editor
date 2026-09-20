"""节点 4 单测 — 2026-09 读产物版(对照 plan §4.3)。

覆盖路径:
1. OpenStoryline 未就绪 → fallback shot_plan(写 ``shot_plan``)。
2. 幂等命中 → 复用 manifest,不再读盘。
3. 预置 plan_timeline_pro 产物 → 走 ``plan_reader`` 拍平,写入 ``storyline_plan``。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_04_import_and_plan import import_video_and_plan_shots
from storyline.output_isolation import (
    OutputJobPaths,
    build_manifest,
    compute_idempotency_key,
    compute_input_sha256,
    write_manifest,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
def _write_plan_timeline_pro(session_dir: Path, *, job_id: str = "test-job") -> Path:
    """在 ``<session_dir>/plan_timeline_pro/`` 下写一份最小可用产物。

    注意:
    - ``source_window.duration`` 必须是整段素材的总时长(便于 plan_reader 算
      ``SourceMedia.duration_ms``),而不是单个 clip 的切片时长。
    - clip 的 ``source_out_ms`` 必须 ≤ ``SourceMedia.duration_ms``(Pydantic 不变量)。
    """
    ptp_dir = session_dir / "plan_timeline_pro"
    ptp_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "tracks": {
            "video": [
                {
                    "source_path": "E:/clips/a.mp4",
                    "source_window": {"start": 0, "end": 5000, "duration": 10000},
                    "timeline_window": {"start": 0, "end": 5000, "duration": 5000},
                    "clip_id": "clip-a-1",
                    "size": [1920, 1080],
                },
                {
                    "source_path": "E:/clips/a.mp4",
                    "source_window": {"start": 5000, "end": 10000, "duration": 10000},
                    "timeline_window": {"start": 5000, "end": 10000, "duration": 5000},
                    "clip_id": "clip-a-2",
                    "size": [1920, 1080],
                },
            ],
            "subtitles": [{"text": "第一句"}],
            "voiceover": [],
            "bgm": [],
        },
    }
    plan_file = ptp_dir / "plan_timeline_pro_test.json"
    plan_file.write_text(
        json.dumps({"payload": plan, "artifact_id": "plan-art-1"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return plan_file


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def test_openstoryline_not_ready_falls_back_to_shot_plan(tmp_path: Path) -> None:
    """OpenStoryline 未就绪 → fallback shot_plan。"""
    state = {
        "session_id": "job-fallback",
        "video_input_path": "/tmp/30s.mp4",
        "openstoryline_ready": False,
        "error_log": [],
    }
    out = import_video_and_plan_shots(
        state,
        outputs_root=tmp_path / "outputs",
        openstoryline_outputs_root=tmp_path / "os_outputs",
    )
    assert "shot_plan" in out
    assert out["shot_plan"]["video_path"] == "/tmp/30s.mp4"
    assert isinstance(out["shot_plan"]["shots"], list)
    assert any("OpenStoryline 服务未就绪" in e for e in out["error_log"])


def test_idempotent_hit_reuses_manifest(tmp_path: Path) -> None:
    """已有 manifest.json 且 key 一致 → 直接复用,不再读盘。"""
    import hashlib
    from config import WORKFLOW_ENV as _we
    job_id = "job-idem"
    video_path = "/tmp/30s.mp4"
    state = {
        "session_id": job_id,
        "video_input_path": video_path,
        "openstoryline_ready": True,
        "error_log": [],
    }
    # node_04 内部 cfg_snapshot 形如 {"WORKFLOW_ENV": WORKFLOW_ENV},
    # input_sha = sha256(video_path)(无 extras);这里手工算同样的 key。
    cfg_snapshot = {"WORKFLOW_ENV": _we}
    cfg_sha = hashlib.sha256(
        json.dumps(cfg_snapshot, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    input_sha = compute_input_sha256(video_path=video_path)
    expected_key = compute_idempotency_key(
        job_id=job_id, input_sha256=input_sha, config_sha256=cfg_sha,
    )

    # 预置一份 manifest(模拟上次跑完留下的),key 与 node_04 计算一致。
    outputs_root = tmp_path / "outputs"
    paths = OutputJobPaths.for_job(job_id=job_id, root=outputs_root)
    paths.ensure()
    manifest = build_manifest(
        job_id=job_id,
        video_path=video_path,
        config_snapshot=cfg_snapshot,
        storyline_session_id="old-sid",
        storyline_artifact_ids=["old-art"],
        draft_path=str((tmp_path / "drafts" / "draft_content.json").resolve()),
    )
    # build_manifest 算的 key 与 node_04 不同(它用 video_path+extras 算 input_sha),
    # 这里强制覆盖成与 node_04 一致的 key,模拟"上次跑的 key 跟这次一致"。
    manifest.idempotency_key = expected_key
    write_manifest(paths, manifest)

    out = import_video_and_plan_shots(
        state,
        outputs_root=outputs_root,
        openstoryline_outputs_root=tmp_path / "os_outputs_no_dir",  # 不存在也不该读
    )
    # 命中:out 应直接带回 draft_path / storyline_session_id
    assert out.get("draft_path") == manifest.draft_path
    assert out.get("storyline_session_id") == "old-sid"
    assert any("idempotency hit" in e for e in out["error_log"])


def test_happy_path_reads_latest_plan_timeline_pro(tmp_path: Path) -> None:
    """预置产物 → 走 plan_reader 拍平为 CanonicalTimeline。"""
    job_id = "job-happy"
    os_outputs = tmp_path / "os_outputs"
    session_dir = os_outputs / "sid-real"
    _write_plan_timeline_pro(session_dir, job_id=job_id)

    state = {
        "session_id": job_id,
        "video_input_path": "E:/clips/a.mp4",
        "openstoryline_ready": True,
        "error_log": [],
    }
    out = import_video_and_plan_shots(
        state,
        outputs_root=tmp_path / "outputs",
        openstoryline_outputs_root=os_outputs,
    )

    # CanonicalTimeline 拍平成功
    assert "storyline_plan" in out
    sp = out["storyline_plan"]
    assert sp["schema_version"] == "1.0"
    assert sp["job_id"] == job_id
    assert len(sp["clips"]) == 2
    assert sp["clips"][0]["clip_id"] == "clip-a-1"
    # source_media 唯一化(同一 source_path 只算一份)
    assert len(sp["source_media"]) == 1
    # session_id / outputs_root 回填
    assert out["storyline_session_id"] == "sid-real"
    assert out["storyline_outputs_root"].endswith("sid-real")
    # manifest 写入
    assert any("node_04_import_and_plan_done" in s for s in out["status_log"])


def test_no_session_dir_falls_back_to_shot_plan(tmp_path: Path) -> None:
    """openstoryline/outputs/ 下没有任何目录 → fallback shot_plan。"""
    state = {
        "session_id": "job-empty",
        "video_input_path": "/tmp/30s.mp4",
        "openstoryline_ready": True,
        "error_log": [],
    }
    empty_os_outputs = tmp_path / "os_empty"
    empty_os_outputs.mkdir()  # 目录存在但里面为空
    out = import_video_and_plan_shots(
        state,
        outputs_root=tmp_path / "outputs",
        openstoryline_outputs_root=empty_os_outputs,
    )
    assert "shot_plan" in out
    assert out["shot_plan"]["_fallback_reason"] == "CONTRACT_INVALID"
    assert any("未在" in e for e in out["error_log"])


def test_plan_reader_error_falls_back_to_shot_plan(tmp_path: Path) -> None:
    """预置的产物 JSON 损坏 → fallback shot_plan。"""
    job_id = "job-bad-json"
    os_outputs = tmp_path / "os_outputs"
    session_dir = os_outputs / "sid-bad"
    ptp_dir = session_dir / "plan_timeline_pro"
    ptp_dir.mkdir(parents=True, exist_ok=True)
    # 写一份 JSON 不闭合的文件
    (ptp_dir / "plan_timeline_pro_broken.json").write_text(
        '{"tracks": {"video": [', encoding="utf-8"
    )

    state = {
        "session_id": job_id,
        "video_input_path": "/tmp/30s.mp4",
        "openstoryline_ready": True,
        "error_log": [],
    }
    out = import_video_and_plan_shots(
        state,
        outputs_root=tmp_path / "outputs",
        openstoryline_outputs_root=os_outputs,
    )
    assert "shot_plan" in out
    assert any("产物读取失败" in e for e in out["error_log"])