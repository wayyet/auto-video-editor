"""P1-0 基线:原始 ``storyline_plan → mapper → draft_content`` 路径防回归(第六期)。

固定当前 mapper 行为,作为 assembly 前移重构(P1-1~P1-6)的回归地基:
1. fixture 可过 ``CanonicalTimeline.model_validate``(契约合法);
2. mapper 确定性:同一 plan 重复执行输出逐字节一致;
3. 结构摘要稳定:无改动 plan 复跑,视频轨/音轨摘要不变;
4. 引用完整性检查能识别缺失素材、悬空引用与重复 ID;
5. 中英字幕与音轨字段(provider/stub/path/duration)留档断言。
"""

from __future__ import annotations

import copy
import json

import pytest

from storyline.contract import CanonicalTimeline
from storyline.mapper import canonical_to_draft
from tests.support.draft_influence import (
    assert_draft_refs_intact,
    draft_audio_materials,
    draft_audio_summary,
    draft_video_summary,
    load_canonical_sample,
    load_canonical_sample_model,
)


@pytest.fixture
def plan_model() -> CanonicalTimeline:
    return load_canonical_sample_model()


@pytest.fixture
def draft(plan_model: CanonicalTimeline) -> dict:
    return canonical_to_draft(plan_model)


# ---------------------------------------------------------------------------
# 1. fixture 契约合法性
# ---------------------------------------------------------------------------
def test_fixture_passes_canonical_validation() -> None:
    plan = load_canonical_sample_model()
    assert plan.schema_version == "1.0"
    assert len(plan.clips) == 3
    assert {s.media_id for s in plan.source_media} == {"media_0001", "media_0002"}


# ---------------------------------------------------------------------------
# 2. mapper 确定性(同一素材计划重复执行,结果稳定)
# ---------------------------------------------------------------------------
def test_mapper_deterministic_rerun(plan_model: CanonicalTimeline) -> None:
    first = canonical_to_draft(plan_model)
    second = canonical_to_draft(plan_model)
    assert json.dumps(first, sort_keys=True, ensure_ascii=False) == json.dumps(
        second, sort_keys=True, ensure_ascii=False
    )


def test_mapper_structure_stable_on_unchanged_plan(draft: dict) -> None:
    """无改动 plan 复跑 mapper,视频轨/音轨摘要不发生非预期结构变化。"""
    plan_model = CanonicalTimeline.model_validate(load_canonical_sample())
    rerun = canonical_to_draft(plan_model)

    assert draft_video_summary(rerun) == draft_video_summary(draft)
    assert draft_audio_summary(rerun) == draft_audio_summary(draft)


# ---------------------------------------------------------------------------
# 3. 视频轨基线形状(clip 顺序 / 源入出点 / 时长)
# ---------------------------------------------------------------------------
def test_video_track_baseline_shape(draft: dict) -> None:
    summary = draft_video_summary(draft)
    assert [s["segment_id"] for s in summary] == ["segment-1", "segment-2", "segment-3"]
    assert [s["material_id"] for s in summary] == ["video-1", "video-2", "video-3"]
    # 时间轴落位与 fixture clips 一致(微秒)
    assert [(s["target_start_us"], s["target_end_us"]) for s in summary] == [
        (0, 4_000_000),
        (4_000_000, 8_000_000),
        (8_000_000, 12_000_000),
    ]
    # 源区间逐一对应(clip_0002 源自 media_0002 的 1000..5000ms)
    assert [(s["source_start_us"], s["source_end_us"]) for s in summary] == [
        (0, 4_000_000),
        (1_000_000, 5_000_000),
        (8_000_000, 12_000_000),
    ]
    # 源跨度 == 目标跨度 → speed 恒 1.0
    assert all(s["speed"] == 1.0 for s in summary)
    # 字幕文本随段留档
    assert [s["transcript"] for s in summary] == ["开场镜头", "中段镜头", "收尾镜头"]


# ---------------------------------------------------------------------------
# 4. 音轨基线形状(BGM 铺满全片;配音按组首/组末 clip;空 path 不物化)
# ---------------------------------------------------------------------------
def test_audio_track_baseline_shape(draft: dict) -> None:
    audio = {s["segment_id"]: s for s in draft_audio_summary(draft)}
    # BGM 从 0 铺到 clips[-1].timeline_out_ms
    assert audio["segment-bgm-1"]["target_start_us"] == 0
    assert audio["segment-bgm-1"]["target_end_us"] == 12_000_000
    # voiceover_0001 组(clip_0001+clip_0002)→ 0..8000ms
    assert audio["segment-voiceover-0001"]["target_start_us"] == 0
    assert audio["segment-voiceover-0001"]["target_end_us"] == 8_000_000
    # voiceover_0002 path 空 → 不物化 segment
    assert "segment-voiceover-0002" not in audio


def test_audio_material_fields_zh_en(draft: dict) -> None:
    """中英分支所需的音轨字段留档:role/provider/stub/ref + storyline_meta 字幕。"""
    audios = draft_audio_materials(draft)
    by_role = {}
    for m in audios:
        by_role.setdefault(m.get("role"), []).append(m)
    # BGM material
    assert by_role["bgm"][0]["id"] == "bgm-1"
    assert by_role["bgm"][0]["ref"] == "bgm_0001"
    # voiceover material 携带英文分支镜像(check_audio_mirror)所需字段
    vo = by_role["voiceover"][0]
    assert vo["id"] == "voiceover-0001"
    assert vo["ref"] == "C:/autovideo_fixture/vo_0001.wav"
    assert vo["provider"] == "stub"
    assert vo["stub"] is True
    assert vo["voiceover_id"] == "voiceover_0001"
    assert vo["group_id"] == "group_0001"
    # storyline_meta 留档 zh/en 字幕
    meta = draft["storyline_meta"]
    assert meta["subtitles"]["zh"].startswith("开场镜头")
    assert meta["subtitles"]["en"].startswith("Opening shot")


# ---------------------------------------------------------------------------
# 5. 引用完整性:正常稿通过;缺素材 / 悬空 / 重复 三类均被识别
# ---------------------------------------------------------------------------
def test_refs_intact_on_baseline(draft: dict) -> None:
    assert_draft_refs_intact(draft)


def test_refs_detect_missing_material(draft: dict) -> None:
    broken = copy.deepcopy(draft)
    broken["materials"]["videos"].pop(0)  # segment-1 的 video-1 消失
    with pytest.raises(AssertionError, match="video-1"):
        assert_draft_refs_intact(broken)


def test_refs_detect_dangling_extra_material_ref(draft: dict) -> None:
    broken = copy.deepcopy(draft)
    broken["tracks"][0]["segments"][0]["extra_material_refs"] = ["video-999"]
    with pytest.raises(AssertionError, match="video-999"):
        assert_draft_refs_intact(broken)


def test_refs_detect_duplicate_material_id(draft: dict) -> None:
    broken = copy.deepcopy(draft)
    dup = copy.deepcopy(broken["materials"]["videos"][0])
    broken["materials"]["videos"].append(dup)  # 同一 id 出现两次
    with pytest.raises(AssertionError, match="重复"):
        assert_draft_refs_intact(broken)


def test_refs_detect_duplicate_segment_id(draft: dict) -> None:
    broken = copy.deepcopy(draft)
    broken["tracks"][0]["segments"][1]["id"] = broken["tracks"][0]["segments"][0]["id"]
    with pytest.raises(AssertionError, match="重复"):
        assert_draft_refs_intact(broken)
