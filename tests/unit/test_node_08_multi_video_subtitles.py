"""多视频字幕映射单测(2026-10 多选导入)。

多视频时,ASR 段的时间是**源视频内**的时间,必须按草稿 clip 的
``source_timerange → target_timerange`` 映射到**成片时间轴**,否则字幕会整体错位。

覆盖:
1. ``_collect_source_spans``:从草稿抽出「源路径 → 落位区间」。
2. ``_map_asr_to_target``:落在 clip 内 → 线性映射;部分重叠 → 先裁再映射;
   完全在 clip 外 → 丢弃。
3. ``_multi_video_asr_segments``:逐源 ASR → 合并 → 按成片时间排序。
"""
from __future__ import annotations

from nodes.node_08_add_subtitles import (
    _collect_source_spans,
    _map_asr_to_target,
    _multi_video_asr_segments,
)


def _draft_two_sources() -> dict:
    """两个源视频各 1 个 clip 的草稿。

    - a.mov:源内 [0s, 4s) → 成片 [0s, 4s)
    - b.mov:源内 [1s, 5s) → 成片 [4s, 8s)
    """
    return {
        "materials": {
            "videos": [
                {"id": "video-1", "path": "E:/media/a.mov", "media_id": "media-1"},
                {"id": "video-2", "path": "E:/media/b.mov", "media_id": "media-2"},
            ],
        },
        "tracks": [
            {
                "type": "video",
                "segments": [
                    {
                        "id": "segment-1",
                        "material_id": "video-1",
                        "source_timerange": {"start": 0, "duration": 4_000_000},
                        "target_timerange": {"start": 0, "duration": 4_000_000},
                    },
                    {
                        "id": "segment-2",
                        "material_id": "video-2",
                        "source_timerange": {"start": 1_000_000, "duration": 4_000_000},
                        "target_timerange": {"start": 4_000_000, "duration": 4_000_000},
                    },
                ],
            },
            {"type": "text", "segments": []},
        ],
    }


def test_collect_source_spans_reads_material_and_timeranges() -> None:
    """草稿 → 每个源路径一条落位区间(源内 + 成片两套时间)。"""
    spans = _collect_source_spans(_draft_two_sources())
    assert set(spans.keys()) == {"E:/media/a.mov", "E:/media/b.mov"}
    assert spans["E:/media/a.mov"] == [{
        "source_start_us": 0,
        "source_duration_us": 4_000_000,
        "target_start_us": 0,
        "target_duration_us": 4_000_000,
    }]
    assert spans["E:/media/b.mov"][0]["source_start_us"] == 1_000_000
    assert spans["E:/media/b.mov"][0]["target_start_us"] == 4_000_000


def test_map_inside_clip_maps_linearly() -> None:
    """落在 clip 内 → 按比例线性映射到成片时间轴。"""
    spans = _collect_source_spans(_draft_two_sources())["E:/media/b.mov"]
    # 源内 [2s, 3s) → clip 源区间 [1s, 5s) → 成片 [4s, 8s)
    # 比例: (2-1)/4 = 0.25 → 4s + 0.25*4s = 5s;  (3-1)/4 = 0.5 → 4s + 2s = 6s
    mapped = _map_asr_to_target(2_000_000, 3_000_000, spans)
    assert mapped is not None
    assert mapped == (5_000_000, 6_000_000)


def test_map_partial_overlap_is_clipped() -> None:
    """只部分重叠 → 先裁到 clip 区间再映射(不整条丢掉)。"""
    spans = _collect_source_spans(_draft_two_sources())["E:/media/b.mov"]
    # 源内 [0s, 2s):一半在 clip 外 → 裁成 [1s, 2s) → 成片 [4s, 5s)
    mapped = _map_asr_to_target(0, 2_000_000, spans)
    assert mapped is not None
    assert mapped == (4_000_000, 5_000_000)


def test_map_outside_all_clips_is_dropped() -> None:
    """完全落在任何 clip 之外 → 返回 None(丢弃)。"""
    spans = _collect_source_spans(_draft_two_sources())["E:/media/b.mov"]
    # 源内 [20s, 21s) —— clip 只覆盖 [1s, 5s)
    assert _map_asr_to_target(20_000_000, 21_000_000, spans) is None


def test_multi_video_asr_segments_merges_and_sorts(monkeypatch) -> None:
    """逐源 ASR → 合并 → 全部落在成片时间轴上,且按时间排序。"""
    calls: list[str] = []

    def _fake_call_asr2s(video_path: str) -> list[dict]:
        calls.append(video_path)
        if video_path == "E:/media/a.mov":
            return [{"text": "A段", "start_s": 0.0, "end_s": 2.0}]
        return [{"text": "B段", "start_s": 2.0, "end_s": 3.0}]

    import nodes.node_08_add_subtitles as mod

    monkeypatch.setattr(mod, "call_asr2s", _fake_call_asr2s)

    spans = _collect_source_spans(_draft_two_sources())
    segs = _multi_video_asr_segments(spans)

    assert sorted(calls) == ["E:/media/a.mov", "E:/media/b.mov"]
    assert len(segs) == 2
    # a.mov 的 ASR [0,2) 落在 clip [0,4) → 成片 [0,2)
    # b.mov 的 ASR [2,3) 落在 clip 源区间 [1,5) → 成片 [4+ (2-1)/4*4 = 5s, 4 + (3-1)/4*4 = 6s]
    by_text = {s["text"]: s for s in segs}
    assert by_text["A段"]["start_s"] == 0.0
    assert by_text["A段"]["end_s"] == 2.0
    assert by_text["B段"]["start_s"] == 5.0
    assert by_text["B段"]["end_s"] == 6.0
    # 按成片时间排序
    assert segs[0]["start_s"] <= segs[1]["start_s"]
