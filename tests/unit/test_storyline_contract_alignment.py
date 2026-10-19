"""D9 回归单测:生产端 plan_timeline_pro 的输出必须能过 CanonicalTimeline 校验。

背景(2026-10-19 P8 实跑):
``storyline_capabilities.plan_timeline_pro`` 产出的 dict 与
``storyline.contract.CanonicalTimeline``(``extra="forbid"``)字段集不一致,
导致 ``qa_gate`` / ``join_storyline`` / ``node_05`` 三处校验全挂,
剪映草稿永远生成不出来(draft_path=None → node_07 崩)。

本文件锁住三件事:
1. 生产端输出**必须**能过契约校验(用 load_media 的真实形状做输入)。
2. ``subtitles.zh`` 是**文本**(narration),不是样式 dict。
3. 契约的严格性没丢:真正未知的字段仍然被拒。
"""

from __future__ import annotations

from typing import Any

import pytest

from storyline.contract import CanonicalTimeline
from storyline_capabilities.plan_timeline_pro import (
    normalize_source_media,
    plan_timeline_pro,
)


# ---------------------------------------------------------------------------
# Fixtures:复刻 load_media / group_clips / generate_script 的真实产物形状
# ---------------------------------------------------------------------------
def _load_media_output() -> list[dict[str, Any]]:
    """``load_media`` 的真实形状(运维形状:path / metadata / orig_path / orig_md5)。"""
    return [
        {
            "media_id": "media_0001",
            "path": "E:\\videos\\input.mp4",
            "file_uri": "file:///E:/videos/input.mp4",
            "media_type": "video",
            "metadata": {
                "duration_ms": 12000,
                "width": 1080,
                "height": 1920,
                "fps": 30.0,
                "has_audio": True,
                "audio_sample_rate_hz": 44100,
            },
            "orig_path": "E:\\videos\\input.mp4",
            "orig_md5": None,
        }
    ]


def _groups() -> list[dict[str, Any]]:
    return [
        {
            "group_id": "group_0001",
            "clips": [
                {
                    "clip_id": "clip_0001",
                    "media_id": "media_0001",
                    "source_in_ms": 0,
                    "source_out_ms": 3000,
                },
                {
                    "clip_id": "clip_0002",
                    "media_id": "media_0001",
                    "source_in_ms": 3000,
                    "source_out_ms": 6000,
                },
            ],
            "captions": ["画面一", "画面二"],
        }
    ]


def _script() -> dict[str, Any]:
    return {
        "group_scripts": [
            {
                "group_id": "group_0001",
                "narration": "这是一段测试旁白。",
                "summary": "测试",
                "lang": "zh",
            }
        ],
        "lang": "zh",
    }


def _text_style() -> dict[str, Any]:
    style = {"font_zh": "SourceHanSansCN-Bold.otf", "size": 24, "color": "#FFFFFF"}
    return {"items": [{"group_id": "group_0001", **style}], "default": style}


def _transition() -> dict[str, Any]:
    return {
        "items": [
            {
                "group_id": "group_0001",
                "transition_in": "fade_in",
                "transition_out": "fade_out",
            }
        ]
    }


def _build(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "groups": _groups(),
        "voiceover": {
            "voiceover": [
                {"voiceover_id": "voiceover_0001", "group_id": "group_0001", "duration": 6500}
            ]
        },
        "group_scripts": _script(),
        "bgm_selection": {"bgm_ref": "bgm_story_warm"},
        "transition_plan": _transition(),
        "text_style_plan": _text_style(),
        "targets": {"target_duration_ms": 35000},
        "source_media": _load_media_output(),
        "job_id": "t1",
    }
    kwargs.update(overrides)
    return plan_timeline_pro(**kwargs)


# ---------------------------------------------------------------------------
# 1. 生产端输出必须过契约校验
# ---------------------------------------------------------------------------
def test_plan_timeline_pro_output_passes_canonical_contract() -> None:
    """D9 核心回归:真实 load_media 形状 → 产出必须能过 CanonicalTimeline。"""
    timeline = _build()
    canonical = CanonicalTimeline.model_validate(timeline)
    assert canonical.job_id == "t1"
    assert len(canonical.clips) == 2
    assert canonical.source_media[0].duration_ms == 12000
    assert canonical.source_media[0].file_uri == "file:///E:/videos/input.mp4"


def test_contract_still_rejects_unknown_fields() -> None:
    """契约的严格性不能因为修 D9 就被放宽(未知字段仍要拒)。"""
    timeline = _build()
    timeline["totally_unknown_field"] = 1
    with pytest.raises(Exception):
        CanonicalTimeline.model_validate(timeline)


# ---------------------------------------------------------------------------
# 2. subtitles.zh 是文本,不是样式 dict
# ---------------------------------------------------------------------------
def test_subtitles_zh_is_narration_text() -> None:
    timeline = _build()
    assert timeline["subtitles"]["zh"] == "这是一段测试旁白。"
    assert isinstance(timeline["subtitles"]["zh"], str)


def test_subtitles_zh_is_none_without_script() -> None:
    """没有 script 产物时字幕文本为空,不能退化成样式 dict。"""
    timeline = _build(group_scripts=None)
    assert timeline["subtitles"]["zh"] is None


def test_subtitle_style_lands_on_clips_not_in_subtitles() -> None:
    """样式改挂到 clips[*].subtitle_style(default 兜底)。"""
    timeline = _build()
    assert timeline["clips"][0]["subtitle_style"]["size"] == 24
    assert "subtitle_style" not in timeline["subtitles"]


# ---------------------------------------------------------------------------
# 3. source_media 整形
# ---------------------------------------------------------------------------
def test_normalize_source_media_drops_operational_fields() -> None:
    out = normalize_source_media(_load_media_output())
    assert out == [
        {
            "media_id": "media_0001",
            "file_uri": "file:///E:/videos/input.mp4",
            "duration_ms": 12000,
            "media_type": "video",
            "width": 1080,
            "height": 1920,
            "fps": 30.0,
        }
    ]


def test_normalize_source_media_derives_file_uri_from_path() -> None:
    out = normalize_source_media([{"media_id": "m1", "path": "E:\\a\\b.mp4"}])
    assert out[0]["file_uri"] == "file:///E:/a/b.mp4"


def test_normalize_source_media_duration_falls_back_to_clip_range() -> None:
    """素材没报时长时,用引用它的 clip 的 source_out_ms 顶上(否则不变量必挂)。"""
    out = normalize_source_media(
        [{"media_id": "m1", "file_uri": "file:///E:/a.mp4"}],
        clips=[{"source_media_id": "m1", "source_out_ms": 4200}],
    )
    assert out[0]["duration_ms"] == 4200


def test_normalize_source_media_unknown_media_type_is_downgraded() -> None:
    out = normalize_source_media(
        [{"media_id": "m1", "file_uri": "file:///E:/a.mp4", "media_type": "weird"}]
    )
    assert out[0]["media_type"] == "unknown"


# ---------------------------------------------------------------------------
# 4. 契约新增字段本身可用
# ---------------------------------------------------------------------------
def test_contract_accepts_new_optional_fields() -> None:
    """Clip.voiceover_id / transition_* / subtitle_style 与 Timeline.method/warnings。"""
    timeline = _build()
    assert timeline["clips"][0]["voiceover_id"] == "voiceover_0001"
    assert timeline["clips"][0]["transition_in"] == "fade_in"
    canonical = CanonicalTimeline.model_validate(timeline)
    assert canonical.method is not None
    assert canonical.warnings == []


def test_subtitles_zh_rejects_dict() -> None:
    """回归防护:subtitles.zh 塞 dict 必须被契约拒(避免再被样式污染)。"""
    timeline = _build()
    timeline["subtitles"]["zh"] = {"size": 24}
    with pytest.raises(Exception):
        CanonicalTimeline.model_validate(timeline)
