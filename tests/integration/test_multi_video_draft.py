"""集成:多视频 → 剪映草稿(2026-10 多选导入)。

验证契约层/能力层**原生就支持多源**(本次不改它们):
``plan_reader.to_canonical`` → ``canonical_to_draft`` → 草稿
``materials.videos`` 含来自不同源视频的条目,且 ``tracks[video].segments``
的 ``material_id`` 各自指向正确的 material。
"""
from __future__ import annotations

from storyline.mapper import canonical_to_draft
from storyline.plan_reader import PlanTimelineProData


def _plan_with_two_sources() -> dict:
    """造一份含 2 个源视频的 plan_timeline_pro 产物(2 个源各 2 个 clip)。

    ``source_window.duration`` 是**整段源视频**的时长(契约会校验
    ``source_out_ms <= media.duration_ms``),这里给 10s;
    ``start`` / ``end`` 才是该 clip 在源内的区间。
    """
    return {
        "create_time": 0,
        "tracks": {
            "video": [
                {
                    "source_path": "E:/media/a.mov",
                    "source_window": {"start": 0, "end": 2000, "duration": 10000},
                    "timeline_window": {"start": 0, "end": 2000, "duration": 2000},
                    "clip_id": "clip_a1",
                },
                {
                    "source_path": "E:/media/b.mov",
                    "source_window": {"start": 500, "end": 2500, "duration": 10000},
                    "timeline_window": {"start": 2000, "end": 4000, "duration": 2000},
                    "clip_id": "clip_b1",
                },
                {
                    "source_path": "E:/media/a.mov",
                    "source_window": {"start": 2000, "end": 4000, "duration": 10000},
                    "timeline_window": {"start": 4000, "end": 6000, "duration": 2000},
                    "clip_id": "clip_a2",
                },
            ],
        },
    }


def test_canonical_timeline_has_two_source_media() -> None:
    """拍平后有 2 条 source_media(按 source_path 去重)。"""
    plan = PlanTimelineProData(artifact_id="a1", raw=_plan_with_two_sources())
    canonical = plan.to_canonical(job_id="job-multi")
    assert len(canonical.source_media) == 2
    assert {m.file_uri for m in canonical.source_media} == {
        "file:///E:/media/a.mov",
        "file:///E:/media/b.mov",
    }
    assert len(canonical.clips) == 3


def test_draft_materials_contain_two_distinct_paths() -> None:
    """草稿 ``materials.videos`` 含 2 个不同 path。"""
    plan = PlanTimelineProData(artifact_id="a1", raw=_plan_with_two_sources())
    canonical = plan.to_canonical(job_id="job-multi")
    draft = canonical_to_draft(canonical)

    videos = draft["materials"]["videos"]
    assert len(videos) == 3
    paths = {v["path"] for v in videos}
    assert paths == {"E:\\media\\a.mov", "E:\\media\\b.mov"}, (
        f"草稿应含来自不同源视频的素材,实际: {paths}"
    )


def test_segment_material_id_points_to_correct_material() -> None:
    """每个 segment 的 ``material_id`` 指向的 material 的 path 与该 clip 的源一致。"""
    plan = PlanTimelineProData(artifact_id="a1", raw=_plan_with_two_sources())
    canonical = plan.to_canonical(job_id="job-multi")
    draft = canonical_to_draft(canonical)

    by_id = {v["id"]: v for v in draft["materials"]["videos"]}
    video_track = next(t for t in draft["tracks"] if t.get("type") == "video")
    segments = video_track["segments"]
    assert len(segments) == 3

    # clip 顺序 = plan 里的 video 顺序:a1 / b1 / a2
    expected_paths = [
        "E:\\media\\a.mov",
        "E:\\media\\b.mov",
        "E:\\media\\a.mov",
    ]
    for seg, expected in zip(segments, expected_paths):
        material = by_id[seg["material_id"]]
        assert material["path"] == expected

    # 时间轴:source→target 两跳可达
    seg = segments[1]
    assert seg["source_timerange"] == {"start": 500_000, "duration": 2_000_000}
    assert seg["target_timerange"] == {"start": 2_000_000, "duration": 2_000_000}
