"""mapper × AI 转场片段单测(2026-11-12 noop 治理阶段2b,设计文档 §2.4)。

转场物化后就是普通视频 clip:mapper 不需要任何"AI 转场"特殊逻辑,
应照常产出 ``materials.videos[]`` 条目与视频轨 segments,且转场段 speed=1.0
(源跨度==目标跨度)、原片段被顺序后移。
"""
from __future__ import annotations

from storyline.contract import CanonicalTimeline
from storyline.mapper import canonical_to_draft, draft_to_canonical


def _timeline_with_transition() -> dict:
    return {
        "schema_version": "1.0",
        "job_id": "mapper-test",
        "created_at_ms": 1,
        "source_media": [
            {"media_id": "m1", "file_uri": "file:///C:/media/a.mp4", "duration_ms": 60000,
             "media_type": "video", "width": 1080, "height": 1920, "fps": 30.0},
            {
                "media_id": "transition_media_ab12cd34",
                "file_uri": "file:///C:/outputs/t/transition_ab.mp4",
                "duration_ms": 500,
                "media_type": "video",
                "width": 1080,
                "height": 1920,
                "fps": 30.0,
                "sha256": "a" * 64,
            },
        ],
        "clips": [
            {
                "clip_id": "c1",
                "source_media_id": "m1",
                "source_in_ms": 0,
                "source_out_ms": 4000,
                "timeline_in_ms": 0,
                "timeline_out_ms": 4000,
                "transcript": "字幕c1",
            },
            {
                "clip_id": "transition_0001",
                "source_media_id": "transition_media_ab12cd34",
                "source_in_ms": 0,
                "source_out_ms": 500,
                "timeline_in_ms": 4000,
                "timeline_out_ms": 4500,
                "semantic_tags": ["ai_transition"],
                "ai_transition_pair": "c1->c2",
            },
            {
                "clip_id": "c2",
                "source_media_id": "m1",
                "source_in_ms": 4000,
                "source_out_ms": 8000,
                "timeline_in_ms": 4500,
                "timeline_out_ms": 8500,
                "transcript": "字幕c2",
            },
        ],
        "audio": {"bgm_ref": None, "voiceover": None},
        "subtitles": {"zh": "整段中文文案", "en": None},
        "options": {"enable_ai_transition": True, "enable_voiceover": False, "max_duration_ms": 35000},
        "method": "tts_align",
        "warnings": [],
        "ai_transitions": [
            {
                "from_clip_id": "c1",
                "to_clip_id": "c2",
                "duration_ms": 500,
                "provider": "minimax",
                "is_stub": False,
                "error": None,
                "video_path": "C:/outputs/t/transition_ab.mp4",
                "status": "generated",
            }
        ],
    }


def _draft():
    plan = CanonicalTimeline.model_validate(_timeline_with_transition())
    return canonical_to_draft(plan, canvas_w=1080, canvas_h=1920)


def _video_segments(draft: dict) -> list[dict]:
    return [
        s
        for tr in draft["tracks"]
        if tr.get("type") == "video"
        for s in (tr.get("segments") or [])
    ]


def test_transition_clip_becomes_video_material() -> None:
    draft = _draft()
    videos = draft["materials"]["videos"]
    # 3 个 clip → 3 条 video material(转场就是普通视频素材)
    assert len(videos) == 3
    trans = next(v for v in videos if v["media_id"] == "transition_media_ab12cd34")
    assert trans["path"].endswith("transition_ab.mp4")
    assert trans["source_in_ms"] == 0 and trans["source_out_ms"] == 500


def test_transition_segment_sits_between_originals_with_speed_one() -> None:
    draft = _draft()
    segments = _video_segments(draft)
    assert len(segments) == 3
    # 按时间轴起点排序后的中段就是转场段(500ms,微秒口径)
    by_start = sorted(segments, key=lambda s: s["target_timerange"]["start"])
    trans_seg = by_start[1]
    assert trans_seg["target_timerange"] == {"start": 4_000_000, "duration": 500_000}
    assert trans_seg["source_timerange"] == {"start": 0, "duration": 500_000}
    assert trans_seg["speed"] == 1.0  # 转场片段不变速
    # 原片段被顺序后移:c2 从 4500ms 起
    assert by_start[0]["target_timerange"]["start"] == 0
    assert by_start[2]["target_timerange"]["start"] == 4_500_000
    assert by_start[2]["target_timerange"]["duration"] == 4_000_000


def test_timeline_roundtrip_keeps_clip_order() -> None:
    """draft→canonical 反向不抛、clip 顺序保持(转场按普通视频片段回流)。"""
    plan = CanonicalTimeline.model_validate(_timeline_with_transition())
    draft = canonical_to_draft(plan, canvas_w=1080, canvas_h=1920)
    back = draft_to_canonical(draft, job_id="mapper-test")
    # ai_transition_pair 属 storyline 溯源字段,不要求在剪映原生结构里承载;
    # 反向只保证片段序列与源映射闭合
    assert [c.clip_id for c in back.clips] == ["c1", "transition_0001", "c2"]
