"""beat_align 总时长封顶回归(2026-11-08 实机任务 console-9b8f05ce5096)。

背景:BGM 库条目(bgm_meta_ref.yaml)是**元数据参考**(path 可为空、真实
音频未必存在),`_distribute_by_beats` 旧实现把 BGM duration_ms 当时间轴
总长的唯一权威 —— bgm_upbeat_energetic(120000ms / 470ms)→ 255×470=
119850ms,单 clip 全吃;素材 30s @ speed 1.0 铺满 2 分钟,剪映时间轴
1:59:26,播到 30s 就没画面,而 qa_gate 重试 3 次结果不变(deterministic)
按设计强制放行。回归点:总时长必须被 min(target, Σ素材跨度) 封顶,
且封顶后仍落在整数节拍点上。
"""
from __future__ import annotations

import pytest

from storyline_capabilities.timeline_planner import TimeLine


def _groups(*source_spans: tuple[int, int]) -> list[dict]:
    return [
        {
            "group_id": "group_0001",
            "clips": [
                {
                    "clip_id": f"clip_{i + 1:04d}",
                    "media_id": "media_0001",
                    "source_in_ms": a,
                    "source_out_ms": b,
                }
                for i, (a, b) in enumerate(source_spans)
            ],
        }
    ]


_BG_UPBEAT = {"bgm_ref": "bgm_upbeat_energetic", "duration_ms": 120000,
              "beat_period_ms": 470}


def test_beats_capped_to_target_and_source() -> None:
    """实机复现:30s 素材 + 120s/470ms BGM → 旧版 119850ms,现必须 ≤30000
    且向内对齐到整数节拍(63×470=29610),不再产生超目标 warning。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=_groups((0, 30000)),
        tts_res=[],
        bgm=dict(_BG_UPBEAT),
        targets={"target_duration_ms": 35000},
    )
    assert res["method"] == "beat_align"
    assert res["total_ms"] == 29610, f"应封顶到 63×470=29610,实际 {res['total_ms']}"
    assert res["clips"][0]["timeline_out_ms"] == 29610
    assert not any("exceeds target" in w for w in res["warnings"]), (
        f"封顶后不应再超目标: {res['warnings']}"
    )


def test_beats_cap_takes_min_of_target_and_source() -> None:
    """素材比目标短:cap 取 Σ素材跨度(20000 < 35000)→ 42×470=19740。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=_groups((0, 20000)),
        tts_res=[],
        bgm=dict(_BG_UPBEAT),
        targets={"target_duration_ms": 35000},
    )
    assert res["total_ms"] == 19740
    assert res["total_ms"] <= 20000


def test_beats_within_cap_unchanged() -> None:
    """BGM 时长本身在 cap 内:行为与旧版完全一致(不做多余截断)。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=_groups((0, 30000)),
        tts_res=[],
        bgm={"duration_ms": 8000, "beat_period_ms": 500},
        targets={"target_duration_ms": 35000},
    )
    assert res["total_ms"] == 8000


def test_beats_multi_clip_capped_and_contiguous() -> None:
    """2 个 clip 共 30s 素材:cap 后总长 29610,均分且时间轴无缝。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=_groups((0, 15000), (15000, 30000)),
        tts_res=[],
        bgm=dict(_BG_UPBEAT),
        targets={"target_duration_ms": 35000},
    )
    clips = res["clips"]
    assert len(clips) == 2
    assert res["total_ms"] == 29610
    assert clips[0]["timeline_in_ms"] == 0
    assert clips[0]["timeline_out_ms"] == clips[1]["timeline_in_ms"]
    assert clips[-1]["timeline_out_ms"] == res["total_ms"]
    for c in clips:
        assert c["timeline_out_ms"] - c["timeline_in_ms"] >= 500  # min_clip


def test_scale_path_still_targets_budget() -> None:
    """无 BGM 的兜底路径不受影响:仍等比到 target。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=_groups((0, 30000)),
        tts_res=[],
        bgm=None,
        targets={"target_duration_ms": 35000},
    )
    assert res["method"] == "scaled_to_target"
    assert res["total_ms"] == 35000
