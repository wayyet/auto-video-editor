"""单测:``fill_clip_transcripts``(2026-10 ASR 旁支 → ``Clip.transcript`` 回填)。

锁定规则(见 ``storyline_capabilities/timeline_transcript.py`` 顶部说明):
粗剪优先 / 重叠最多的 clip 拿文字 / 不重复分配 / 不覆盖已有 / 素材归属 / 永不抛 / 只写文字。
"""
from __future__ import annotations

import copy

import pytest

from storyline.contract import CanonicalTimeline
from storyline_capabilities.timeline_transcript import fill_clip_transcripts


def _timeline(*ranges: tuple[int, int], media: str = "m1") -> dict:
    """按给定源范围造一条时间线(同一素材;timeline 依次首尾相接)。"""
    clips = []
    t = 0
    for i, (a, b) in enumerate(ranges, 1):
        clips.append(
            {
                "clip_id": f"c{i}",
                "source_media_id": media,
                "source_in_ms": a,
                "source_out_ms": b,
                "timeline_in_ms": t,
                "timeline_out_ms": t + (b - a),
            }
        )
        t += b - a
    return {
        "schema_version": "1.0",
        "job_id": "unit",
        "created_at_ms": 1,
        "source_media": [
            {"media_id": media, "file_uri": "file:///C:/a.mp4", "duration_ms": 600000}
        ],
        "clips": clips,
    }


def _seg(text: str, start: int, end: int, **extra) -> dict:
    return {"text": text, "start": start, "end": end, **extra}


def _texts(tl: dict) -> list:
    return [c.get("transcript") for c in tl["clips"]]


def test_prefers_rough_cut_over_raw_asr() -> None:
    tl = _timeline((0, 4000))
    n = fill_clip_transcripts(
        tl,
        rough_cut={"segments": [_seg("精剪后的句子", 500, 3000)]},
        asr={"segments": [_seg("原始 啊 句子", 500, 3000)]},
    )
    assert n == 1
    assert _texts(tl) == ["精剪后的句子"]


def test_falls_back_to_raw_asr_when_rough_cut_has_no_usable_segment() -> None:
    tl = _timeline((0, 4000))
    n = fill_clip_transcripts(
        tl,
        rough_cut={"segments": [_seg("", 0, 1000), _seg("坏段", 3000, 3000)]},
        asr={"segments": [_seg("原始句子", 500, 3000)]},
    )
    assert n == 1
    assert _texts(tl) == ["原始句子"]


def test_segment_goes_to_clip_with_max_overlap() -> None:
    # clip1=[0,4000) clip2=[4000,8000);段 [3000,7000) 与 clip1 重叠 1000,与 clip2 重叠 3000
    tl = _timeline((0, 4000), (4000, 8000))
    fill_clip_transcripts(tl, asr={"segments": [_seg("跨界句", 3000, 7000)]})
    assert _texts(tl) == [None, "跨界句"]


def test_tie_goes_to_earlier_clip_and_segment_is_never_duplicated() -> None:
    # 段 [3000,5000) 与两个 clip 各重叠 1000 → 平局,给更靠前的 clip1
    tl = _timeline((0, 4000), (4000, 8000))
    n = fill_clip_transcripts(tl, asr={"segments": [_seg("平局句", 3000, 5000)]})
    assert n == 1
    assert _texts(tl) == ["平局句", None]


def test_multiple_segments_in_one_clip_join_in_start_order_with_single_space() -> None:
    tl = _timeline((0, 10000))
    fill_clip_transcripts(
        tl,
        asr={"segments": [_seg("第三句", 6000, 7000), _seg("第一句", 0, 1000), _seg("第二句", 2000, 3000)]},
    )
    assert _texts(tl) == ["第一句 第二句 第三句"]


def test_existing_transcript_is_not_overwritten() -> None:
    tl = _timeline((0, 4000), (4000, 8000))
    tl["clips"][0]["transcript"] = "人工写的"
    n = fill_clip_transcripts(
        tl,
        asr={"segments": [_seg("机器的", 500, 3000), _seg("第二段", 4500, 7000)]},
    )
    assert n == 1  # 只有 clip2 被回填
    assert _texts(tl) == ["人工写的", "第二段"]


def test_segment_outside_every_clip_is_dropped() -> None:
    tl = _timeline((0, 4000))
    n = fill_clip_transcripts(tl, asr={"segments": [_seg("片尾", 9000, 9500)]})
    assert n == 0
    assert _texts(tl) == [None]


def test_touching_but_not_overlapping_is_not_assigned() -> None:
    tl = _timeline((0, 4000))
    n = fill_clip_transcripts(tl, asr={"segments": [_seg("边界", 4000, 5000)]})
    assert n == 0


def test_media_id_on_segment_selects_matching_clip_only() -> None:
    tl = _timeline((0, 4000))
    # 再加一个来自 m2 的 clip,源范围与 m1 的 clip 相同
    tl["source_media"].append(
        {"media_id": "m2", "file_uri": "file:///C:/b.mp4", "duration_ms": 600000}
    )
    tl["clips"].append(
        {
            "clip_id": "c2",
            "source_media_id": "m2",
            "source_in_ms": 0,
            "source_out_ms": 4000,
            "timeline_in_ms": 4000,
            "timeline_out_ms": 8000,
        }
    )
    fill_clip_transcripts(tl, asr={"segments": [_seg("来自二号素材", 500, 3000, media_id="m2")]})
    assert _texts(tl) == [None, "来自二号素材"]


def test_document_level_media_id_is_honoured() -> None:
    tl = _timeline((0, 4000), media="m1")
    n = fill_clip_transcripts(tl, asr={"media_id": "m9", "segments": [_seg("别的素材", 500, 3000)]})
    assert n == 0  # m9 不在时间线里
    assert _texts(tl) == [None]


def test_no_media_id_and_several_media_is_skipped() -> None:
    tl = _timeline((0, 4000))
    tl["source_media"].append(
        {"media_id": "m2", "file_uri": "file:///C:/b.mp4", "duration_ms": 600000}
    )
    tl["clips"].append(
        {
            "clip_id": "c2",
            "source_media_id": "m2",
            "source_in_ms": 0,
            "source_out_ms": 4000,
            "timeline_in_ms": 4000,
            "timeline_out_ms": 8000,
        }
    )
    n = fill_clip_transcripts(tl, asr={"segments": [_seg("不知道是谁的", 500, 3000)]})
    assert n == 0  # 无法判断归属,宁可不填
    assert _texts(tl) == [None, None]


def test_only_text_is_written_everything_else_is_untouched() -> None:
    tl = _timeline((0, 4000), (4000, 8000))
    before = copy.deepcopy(tl)
    fill_clip_transcripts(tl, asr={"segments": [_seg("甲", 500, 3000), _seg("乙", 4500, 7000)]})
    for c in tl["clips"]:
        c.pop("transcript", None)
    assert tl == before
    # 回填之后仍然能过契约校验
    tl2 = copy.deepcopy(before)
    fill_clip_transcripts(tl2, asr={"segments": [_seg("甲", 500, 3000)]})
    CanonicalTimeline.model_validate(tl2)


@pytest.mark.parametrize(
    "timeline,rough,asr",
    [
        (None, None, None),
        ({}, None, None),
        ({"clips": None}, None, {"segments": []}),
        ({"clips": []}, None, {"segments": [_seg("x", 0, 1)]}),
        ({"clips": ["不是 dict"]}, None, {"segments": [_seg("x", 0, 1)]}),
        (_timeline((0, 4000)), "不是 dict", 123),
        (_timeline((0, 4000)), {"segments": "不是 list"}, None),
        (_timeline((0, 4000)), {"segments": [None, 5, "x", {}]}, None),
        (_timeline((0, 4000)), None, {"segments": [_seg("x", True, 9)]}),  # bool 不是毫秒
        (_timeline((0, 4000)), None, {"segments": [_seg("x", "abc", 9)]}),
        (_timeline((0, 4000)), None, {"segments": [_seg("x", 5, 5)]}),  # end <= start
        (_timeline((0, 4000)), None, {"segments": [_seg("   ", 0, 1000)]}),  # 空文本
    ],
)
def test_bad_inputs_never_raise_and_fill_nothing(timeline, rough, asr) -> None:
    assert fill_clip_transcripts(timeline, rough_cut=rough, asr=asr) == 0


def test_float_and_string_millis_are_accepted() -> None:
    tl = _timeline((0, 4000))
    n = fill_clip_transcripts(tl, asr={"segments": [_seg("数字字符串", "500", 3000.0)]})
    assert n == 1
    assert _texts(tl) == ["数字字符串"]
