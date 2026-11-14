"""2026-11-14 创建台「快进速度(倍)」单测 —— 规划器智能保同步语义。

语义(用户拍板):``storyline_targets.speed_multiplier = N`` > 1 时每段至少
N 倍速:
- 第一优先:在同一素材内向后扩 ``source_out`` 提速度 —— 段时长/时间戳/
  配音/总时长全部不动(快进的本质:同样时间展示更多内容);
- 素材余量不足时:TTS(配音)段保原速并写 warning(绝不音画错位),
  非 TTS 段由 ``_apply_source_span_caps`` 抬高下限(≥ max(0.5, N))压短兜底;
- 默认 1.0 / 缺省 = 不启用,行为与旧版逐字节一致(回归锁)。

锁定点:
- ``timeline_planner.TimeLine.distribute``:扩 span / 抬高下限 / 兼容态;
- ``plan_timeline_pro._media_durations``:media_id 兜底键与 normalize 一致;
- 契约不变量:扩 span 后 source_out ≤ media duration、速度下限仍通过;
- mapper:扩 span 后派生 speed ≥ N 落草稿。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from storyline.contract import CanonicalTimeline  # noqa: E402
from storyline.mapper import canonical_to_draft  # noqa: E402
from storyline_capabilities.plan_timeline_pro import _media_durations  # noqa: E402
from storyline_capabilities.timeline_planner import TimeLine  # noqa: E402


def _clips(*spans: tuple[int, int]) -> list[dict]:
    return [
        {
            "clip_id": f"clip_{i + 1:04d}",
            "media_id": "media_0001",
            "source_in_ms": a,
            "source_out_ms": b,
        }
        for i, (a, b) in enumerate(spans)
    ]


def _span(clip: dict) -> int:
    return int(clip["source_out_ms"]) - int(clip["source_in_ms"])


def _slot(clip: dict) -> int:
    return int(clip["timeline_out_ms"]) - int(clip["timeline_in_ms"])


# ===========================================================================
# TTS(配音)模式:扩 span 保同步
# ===========================================================================
def test_tts_widen_spans_keeps_slots_and_total() -> None:
    """慢段有素材余量:span 扩到 N×slot,段时长/总时长不变,派生速度 ≥ N。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=[{"group_id": "g1", "clips": _clips((0, 4000), (4000, 5000))}],
        tts_res=[{"voiceover_id": "v1", "duration": 8000}],
        bgm=None,
        targets={"target_duration_ms": 8000, "speed_multiplier": 2.0},
        media_durations={"media_0001": 20000},
    )
    c1, c2 = res["clips"]
    # 段时长与总时长完全不动(配音同步的根基)
    assert res["total_ms"] == 8000
    assert (_slot(c1), _slot(c2)) == (4000, 4000)
    # c1:span 4000 → 需要 8000;素材 0..4000 后被 c2 占据?否 —— c2 是"素材内
    # 下一个 clip",c1 上界 = c2.source_in = 4000,无空隙 → 保原速 + 告警
    assert _span(c1) == 4000
    assert any("clip_0001" in w for w in res["warnings"])
    # c2:media 内最后一个 clip,可吃到素材尾(20000)→ 扩到 2×4000 = 8000
    assert _span(c2) == 8000
    assert _span(c2) / _slot(c2) >= 2.0 - 1e-9


def test_tts_widen_only_needed_amount() -> None:
    """扩 span 只扩到刚好 ceil(N×slot),不多吃素材。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=[{"group_id": "g1", "clips": _clips((4000, 5000))}],
        tts_res=[{"voiceover_id": "v1", "duration": 1000}],
        bgm=None,
        targets={"target_duration_ms": 1000, "speed_multiplier": 2.0},
        media_durations={"media_0001": 60000},
    )
    (c,) = res["clips"]
    assert _slot(c) == 1000
    assert _span(c) == 2000  # ceil(2.0 × 1000),不是吃满素材尾
    assert _span(c) / _slot(c) >= 2.0 - 1e-9


def test_tts_no_room_keeps_speed_and_warns() -> None:
    """素材无余量:配音段保原速(段/span 都不动)+ warning,绝不压短配音段。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=[{"group_id": "g1", "clips": _clips((0, 4000))}],
        tts_res=[{"voiceover_id": "v1", "duration": 4000}],
        bgm=None,
        targets={"target_duration_ms": 4000, "speed_multiplier": 2.0},
        media_durations={"media_0001": 4000},  # 素材只有 4s,无扩展空间
    )
    (c,) = res["clips"]
    assert _slot(c) == 4000 and _span(c) == 4000
    assert res["total_ms"] == 4000
    assert res["warnings"], "无余量达到快进下限时必须告警(保原速护配音)"


def test_tts_missing_media_durations_still_safe() -> None:
    """media_durations 缺失(联调直调 distribute):按无余量处理,不崩不扩尾段。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=[{"group_id": "g1", "clips": _clips((0, 4000))}],
        tts_res=[{"voiceover_id": "v1", "duration": 4000}],
        bgm=None,
        targets={"target_duration_ms": 4000, "speed_multiplier": 2.0},
    )
    (c,) = res["clips"]
    assert _slot(c) == 4000 and _span(c) == 4000
    assert res["warnings"]


# ===========================================================================
# 非 TTS(等比)模式:扩 span 优先,不足压短兜底
# ===========================================================================
def test_scale_mode_floor_shrinks_slots_as_fallback() -> None:
    """等比模式:扩不了 span 的段由抬高下限(max(0.5, N))压短,速度 ≥ N。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=[{"group_id": "g1", "clips": _clips((0, 3000), (3000, 6000))}],
        tts_res=[],
        bgm=None,
        targets={"target_duration_ms": 6000, "speed_multiplier": 2.0},
        media_durations={"media_0001": 6000},  # c1 无空隙、c2 已顶到素材尾
    )
    c1, c2 = res["clips"]
    # c1 上界 = c2.source_in(无空隙)→ 保不了 2x,由 caps 压短到 1500ms
    assert _span(c1) / _slot(c1) >= 2.0 - 1e-9
    assert _slot(c1) == 1500
    # c2 顶到素材尾,同样压短
    assert _span(c2) / _slot(c2) >= 2.0 - 1e-9


def test_scale_mode_floor_total_shorter_than_target_is_accepted() -> None:
    """压短兜底导致总长 < 目标时长属预期(素材不足以同时满足两约束),不注水。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=[{"group_id": "g1", "clips": _clips((0, 3000))}],
        tts_res=[],
        bgm=None,
        targets={"target_duration_ms": 6000, "speed_multiplier": 2.0},
        media_durations={"media_0001": 3000},
    )
    (c,) = res["clips"]
    assert res["total_ms"] == 1500  # 3000ms 素材 @ 2x
    assert _span(c) / _slot(c) >= 2.0 - 1e-9


# ===========================================================================
# 默认/非法值:行为与旧版一致(回归锁)
# ===========================================================================
@pytest.mark.parametrize("raw", [None, 1.0, 1, 0.5, "abc", ""])
def test_default_or_invalid_multiplier_no_behavior_change(raw) -> None:
    """缺省/1.0/非法值一律视为不启用:输出与无该键时完全一致。"""
    baseline = TimeLine().distribute(
        groups=[{"group_id": "g1", "clips": _clips((0, 4000), (4000, 5000))}],
        tts_res=[{"voiceover_id": "v1", "duration": 8000}],
        bgm=None,
        targets={"target_duration_ms": 8000},
        media_durations={"media_0001": 20000},
    )
    out = TimeLine().distribute(
        groups=[{"group_id": "g1", "clips": _clips((0, 4000), (4000, 5000))}],
        tts_res=[{"voiceover_id": "v1", "duration": 8000}],
        bgm=None,
        targets={"target_duration_ms": 8000, "speed_multiplier": raw}
        if raw is not None
        else {"target_duration_ms": 8000},
        media_durations={"media_0001": 20000},
    )
    assert out == baseline


def test_widen_respects_next_clip_boundary() -> None:
    """扩展硬上界 = 同素材内下一个 clip 的 source_in,绝不吞并别人已选区间。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=[{"group_id": "g1", "clips": _clips((0, 2000), (20000, 22000))}],
        tts_res=[{"voiceover_id": "v1", "duration": 2000, "group_id": "g1"}] * 1,
        bgm=None,
        targets={"target_duration_ms": 2000, "speed_multiplier": 2.0},
        media_durations={"media_0001": 60000},
    )
    # 两 clip 各分 1000ms slot;clip_0001 上界 = clip_0002.source_in = 20000
    c1, c2 = res["clips"]
    assert _slot(c1) == 1000 and _slot(c2) == 1000
    assert int(c1["source_out_ms"]) <= 20000
    assert int(c2["source_in_ms"]) == 20000  # 后段取材区间不被前段吞并


# ===========================================================================
# 契约 / mapper:扩 span 后的计划全链合法
# ===========================================================================
def test_widened_plan_passes_contract_and_mapper_speed() -> None:
    """扩 span 的计划过 CanonicalTimeline 不变量,mapper 派生 speed ≥ N 落草稿。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=[{"group_id": "g1", "clips": _clips((10000, 12000))}],
        tts_res=[{"voiceover_id": "v1", "duration": 2000}],
        bgm=None,
        targets={"target_duration_ms": 2000, "speed_multiplier": 2.0},
        media_durations={"media_0001": 31000},
    )
    plan_dict = {
        "schema_version": "1.0",
        "job_id": "j-speed",
        "created_at_ms": 0,
        "source_media": [
            {
                "media_id": "media_0001",
                "file_uri": "file:///C:/media/0001.mov",
                "duration_ms": 31000,
                "media_type": "video",
            }
        ],
        "clips": [
            {
                "clip_id": c["clip_id"],
                "source_media_id": "media_0001",
                "source_in_ms": int(c["source_in_ms"]),
                "source_out_ms": int(c["source_out_ms"]),
                "timeline_in_ms": int(c["timeline_in_ms"]),
                "timeline_out_ms": int(c["timeline_out_ms"]),
            }
            for c in res["clips"]
        ],
        "audio": {},
        "subtitles": {},
        "options": {},
    }
    plan = CanonicalTimeline.model_validate(plan_dict)  # 不抛 = 不变量通过
    clip = plan.clips[0]
    source_ms = clip.source_out_ms - clip.source_in_ms
    target_ms = clip.timeline_out_ms - clip.timeline_in_ms
    assert source_ms / target_ms >= 2.0 - 1e-9
    draft = canonical_to_draft(plan)
    seg = draft["tracks"][0]["segments"][0]
    assert seg["speed"] >= 2.0 - 1e-9


# ===========================================================================
# plan_timeline_pro._media_durations
# ===========================================================================
def test_media_durations_ops_shape_and_fallback() -> None:
    """运维形状顶层/metadata 时长都识别;media_id 兜底与 normalize 一致。"""
    out = _media_durations(
        [
            {"media_id": "m_a", "duration_ms": 5000},
            {"media_id": "m_b", "metadata": {"duration_ms": 6000}},
            {"path": "/x.mp4", "metadata": {"duration_ms": 7000}},  # id 兜底 media_0003
            {"media_id": "m_d"},  # 无时长 → 不入表
            "not-a-dict",
        ]
    )
    assert out == {"m_a": 5000, "m_b": 6000, "media_0003": 7000}


def test_media_durations_empty_inputs() -> None:
    assert _media_durations(None) == {}
    assert _media_durations([]) == {}
