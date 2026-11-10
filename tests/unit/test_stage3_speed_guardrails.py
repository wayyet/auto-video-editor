"""2026-11-11 模糊修复阶段3单测 —— 逐片段时长/速度契约与护栏。

实机事故(console-64da836e2f3f):BGM beat-align 把 0.883s 的源片段铺成
11.593s 目标区间,segment/material 的 speed 仍是 1.0 —— 停帧冒充有效画面。
本文件锁定四处共用速度语义(speed = 源跨度/目标跨度)的护栏行为:

- ``timeline_planner``:逐段上限(源跨度/MIN_CLIP_SPEED)+ 溢出重分配;
- ``contract.CanonicalTimeline``:速度不变量(CLIP_SPEED_INVALID 硬失败);
- ``qa_gate``:逐段一致性复查(SOURCE_RANGE_INVALID / CLIP_SPEED_INVALID);
- ``mapper``:真实 speed,不再硬编码 1.0;
- ``node_07_speed_fit``:拉长护栏 + 按源跨度计算变速;
- ``split_shots``:max-shot 平衡分段,不再二次切出极短残尾。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
from storyline.contract import CanonicalTimeline, ContractInvalid
from storyline.mapper import canonical_to_draft
from storyline_capabilities.split_shots import _balanced_max_shot_spans
from storyline_capabilities.timeline_planner import TimeLine


# ===========================================================================
# timeline_planner:逐段上限 + 重分配
# ===========================================================================
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


_BG = {"bgm_ref": "bgm_x", "duration_ms": 120000, "beat_period_ms": 1000}


def test_planner_short_clip_not_stretched_to_13x() -> None:
    """实机事故回归:883ms 源不得被分配 11.593s 目标(beat-align 均分)。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=_groups((0, 30000), (30000, 30883)),
        tts_res=[],
        bgm=dict(_BG),
        targets={"target_duration_ms": 35000},
    )
    short = res["clips"][1]
    target_ms = short["timeline_out_ms"] - short["timeline_in_ms"]
    cap = int(883 / config.STORYLINE_MIN_CLIP_SPEED)
    assert target_ms <= cap, f"短源片段目标 {target_ms}ms 超过上限 {cap}ms"
    derived = 883 / target_ms
    assert derived >= config.STORYLINE_MIN_CLIP_SPEED - 1e-6


def test_planner_redistributes_excess_to_headroom() -> None:
    """短段被钳掉的时长重分配给有余量的段;总时长不受短源拖垮(在余量内)。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=_groups((0, 30000), (30000, 30883)),
        tts_res=[],
        bgm=dict(_BG),
        targets={"target_duration_ms": 35000},
    )
    clips = res["clips"]
    assert clips[0]["timeline_in_ms"] == 0
    assert clips[1]["timeline_in_ms"] == clips[0]["timeline_out_ms"], "重分配后时间轴必须无缝"
    assert clips[-1]["timeline_out_ms"] == res["total_ms"]


def test_planner_no_water_when_source_insufficient() -> None:
    """总素材不足目标时长:接受总长短于计划并告警,不注水。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=_groups((0, 4000)),
        tts_res=[],
        bgm=dict(_BG),
        targets={"target_duration_ms": 35000},
    )
    # 4000ms 源、阈值 0.5 → 上限 8000ms,远低于 35000 目标
    assert res["total_ms"] <= int(4000 / config.STORYLINE_MIN_CLIP_SPEED)
    assert any("素材不足" in w or "不足" in w for w in res["warnings"]), res["warnings"]


def test_planner_guard_disabled_passthrough(monkeypatch) -> None:
    """MIN_CLIP_SPEED <= 0(联调开关)→ 行为与旧版一致,不做钳制。"""
    monkeypatch.setattr(config, "STORYLINE_MIN_CLIP_SPEED", 0.0)
    tl = TimeLine()
    res = tl.distribute(
        groups=_groups((0, 30000), (30000, 30883)),
        tts_res=[],
        bgm=dict(_BG),
        targets={"target_duration_ms": 35000},
    )
    # 无护栏:三段均分 ~11.59s,短段被拉长(旧行为)
    assert res["clips"][1]["timeline_out_ms"] - res["clips"][1]["timeline_in_ms"] > 10000


def test_planner_slow_motion_allowed_at_cap() -> None:
    """允许慢动作:目标 = 源跨度/阈值 时 speed 恰好等于阈值,合法。"""
    tl = TimeLine()
    res = tl.distribute(
        groups=_groups((0, 4000)),
        tts_res=[],
        bgm=dict(_BG),
        targets={"target_duration_ms": 35000},
    )
    clip = res["clips"][0]
    target_ms = clip["timeline_out_ms"] - clip["timeline_in_ms"]
    assert 4000 / target_ms >= config.STORYLINE_MIN_CLIP_SPEED - 1e-6


# ===========================================================================
# contract:速度不变量(CLIP_SPEED_INVALID)
# ===========================================================================
def _plan_dict(*spans: tuple[int, int, int, int]) -> dict:
    """(src_in, src_out, tl_in, tl_out) 序列 → 最小 canonical dict。"""
    return {
        "schema_version": "1.0",
        "job_id": "j-contract",
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
                "clip_id": f"clip_{i + 1:04d}",
                "source_media_id": "media_0001",
                "source_in_ms": si,
                "source_out_ms": so,
                "timeline_in_ms": ti,
                "timeline_out_ms": to,
            }
            for i, (si, so, ti, to) in enumerate(spans)
        ],
        "audio": {},
        "subtitles": {},
        "options": {},
    }


def test_contract_rejects_stretched_clip() -> None:
    """0.883s 源拉到 11.593s:speed 0.076 < 0.5 → 契约违例硬失败。"""
    with pytest.raises(Exception) as exc_info:
        CanonicalTimeline.model_validate(
            _plan_dict((0, 30000, 0, 11593), (30000, 30883, 11593, 23186))
        )
    assert "CLIP_SPEED_INVALID" in str(exc_info.value)


def test_contract_accepts_consistent_clips() -> None:
    """源/目标一致(speed 1.0)与允许范围内的慢动作都放行。"""
    CanonicalTimeline.model_validate(
        _plan_dict(
            (0, 10000, 0, 10000),      # speed 1.0
            (10000, 30000, 10000, 36000),  # 20s 源 → 26s 目标,speed ≈0.77 ≥0.5
        )
    )


def test_contract_threshold_disabled_allows_stretch(monkeypatch) -> None:
    """阈值关闭(联调):不做速度校验,旧行为放行。"""
    monkeypatch.setattr(config, "STORYLINE_MIN_CLIP_SPEED", 0.0)
    CanonicalTimeline.model_validate(
        _plan_dict((0, 883, 0, 11593)),
    )


# ===========================================================================
# mapper:真实 speed
# ===========================================================================
def test_mapper_writes_derived_speed() -> None:
    plan = CanonicalTimeline.model_validate(
        _plan_dict((0, 10000, 0, 10000), (10000, 30000, 10000, 16000))
    )
    draft = canonical_to_draft(plan)
    seg1, seg2 = draft["tracks"][0]["segments"]
    assert seg1["speed"] == 1.0
    assert seg2["speed"] == pytest.approx(20000 / 6000)


def test_mapper_speed_feeds_native_converter() -> None:
    """真实 speed 必须透传到原生 segment 与 speed material(native 34 键)。"""
    from storyline.native_draft import storyline_to_native

    plan = CanonicalTimeline.model_validate(_plan_dict((0, 8000, 0, 16000)))
    draft, _sidecar = storyline_to_native(canonical_to_draft(plan))
    seg = draft["tracks"][0]["segments"][0]
    assert seg["speed"] == pytest.approx(0.5)
    speeds = draft["materials"]["speeds"]
    ref = next(r for r in seg["extra_material_refs"] if any(sm["id"] == r for sm in speeds))
    assert next(sm for sm in speeds if sm["id"] == ref)["speed"] == pytest.approx(0.5)


# ===========================================================================
# node_07_speed_fit:拉长护栏 + 按源跨度变速
# ===========================================================================
def _write_stretch_draft(tmp_path, src_us: int, tgt_us: int, speed: float = 1.0):
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": tgt_us,
        "materials": {},
        "tracks": [
            {
                "type": "video",
                "fps": 30,
                "segments": [
                    {
                        "id": "seg-1",
                        "speed": speed,
                        "source_timerange": {"start": 0, "duration": src_us},
                        "target_timerange": {"start": 0, "duration": tgt_us},
                    }
                ],
            }
        ],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft), encoding="utf-8")
    return p


def test_node_07_clamps_stretched_segment(tmp_path) -> None:
    """883ms 源铺 11.593s:node_07 钳回 源/阈值 上限并写真实速度 0.5。"""
    from nodes.node_07_speed_fit import speed_fit

    src, tgt = 883_000, 11_593_000
    p = _write_stretch_draft(tmp_path, src, tgt)
    state = {"draft_path": str(p), "retry_counts": {}, "status_log": [], "error_log": []}
    speed_fit(state)

    draft = json.loads(p.read_text(encoding="utf-8"))
    seg = draft["tracks"][0]["segments"][0]
    cap = int(src / config.STORYLINE_MIN_CLIP_SPEED)
    assert seg["target_timerange"]["duration"] == cap
    assert seg["speed"] == pytest.approx(src / cap)  # = 0.5
    assert draft["duration"] == cap
    assert draft["materials"]["speeds"][0]["speed"] == pytest.approx(0.5)


def test_node_07_fixes_speed_within_allowed_stretch(tmp_path) -> None:
    """允许范围内的慢动作段(10s 源 → 15s 目标):目标不动,速度纠偏为 0.667。"""
    from nodes.node_07_speed_fit import speed_fit

    src, tgt = 10_000_000, 15_000_000
    p = _write_stretch_draft(tmp_path, src, tgt, speed=1.0)
    state = {"draft_path": str(p), "retry_counts": {}, "status_log": [], "error_log": []}
    speed_fit(state)

    draft = json.loads(p.read_text(encoding="utf-8"))
    seg = draft["tracks"][0]["segments"][0]
    assert seg["target_timerange"]["duration"] == tgt, "允许慢动作范围内的目标时长不动"
    assert seg["speed"] == pytest.approx(src / tgt)


def test_node_07_compression_uses_source_duration(tmp_path) -> None:
    """压缩变速的分子是源跨度:30s 源 → 10s 目标应得 speed 3.0(旧版按 target 算成 2.0)。"""
    from nodes.node_07_speed_fit import speed_fit

    # 单段 30s 源、20s 目标(speed 1.5 矛盾声明),目标总长 10s → 压到 10s
    p = _write_stretch_draft(tmp_path, 30_000_000, 20_000_000, speed=1.5)
    state = {
        "draft_path": str(p),
        "retry_counts": {},
        "status_log": [],
        "error_log": [],
        "storyline_targets": {"target_duration_ms": 10000},
    }
    speed_fit(state)

    draft = json.loads(p.read_text(encoding="utf-8"))
    seg = draft["tracks"][0]["segments"][0]
    assert seg["target_timerange"]["duration"] <= 10_000_000
    assert seg["speed"] == pytest.approx(30_000_000 / seg["target_timerange"]["duration"])
    assert seg["speed"] > 2.5, "必须按源跨度 30s 计算,而不是 target 20s"


# ===========================================================================
# qa_gate:逐段一致性复查
# ===========================================================================
def _write_timeline_plan(tmp_path, spans) -> str:
    import json

    p = tmp_path / "timeline_plan.json"
    p.write_text(json.dumps(_plan_dict(*spans)), encoding="utf-8")
    return str(p)


def test_qa_gate_blocks_speed_invalid_plan(tmp_path) -> None:
    """逐段速度违例:contract 在加载时即硬失败,qa 落 storyline_qa_failed。"""
    from nodes.storyline.qa_gate import storyline_qa_gate_node

    path = _write_timeline_plan(tmp_path, [(0, 883, 0, 11593)])
    state = {"storyline_timeline_plan": path, "status_log": [], "error_log": []}
    out = storyline_qa_gate_node(state)
    assert "storyline_qa_failed" in out["status_log"]
    assert out["storyline_error_code"] == "CONTRACT_INVALID"
    assert any("CLIP_SPEED_INVALID" in e for e in out["error_log"])


def test_qa_gate_blocks_source_range_overflow(tmp_path) -> None:
    from nodes.storyline.qa_gate import storyline_qa_gate_node

    path = _write_timeline_plan(tmp_path, [(0, 40000, 0, 40000)])  # 媒体仅 31s
    state = {"storyline_timeline_plan": path, "status_log": [], "error_log": []}
    out = storyline_qa_gate_node(state)
    assert "storyline_qa_failed" in out["status_log"]
    assert out["storyline_error_code"] == "CONTRACT_INVALID"
    assert any("exceeds media duration_ms" in e for e in out["error_log"])


def test_qa_gate_healthy_plan_passes(tmp_path) -> None:
    from nodes.storyline.qa_gate import storyline_qa_gate_node

    path = _write_timeline_plan(tmp_path, [(0, 30000, 0, 30000)])
    state = {"storyline_timeline_plan": path, "status_log": [], "error_log": []}
    out = storyline_qa_gate_node(state)
    assert any(str(t).startswith("storyline_qa_done") for t in out["status_log"])


# ===========================================================================
# split_shots:平衡分段
# ===========================================================================
class TestBalancedMaxShotSpans:
    def test_real_accident_no_short_tail(self) -> None:
        """实机事故回归:30.883s 按 30s 上限切成两段近似均分,不再出现 883ms 尾。"""
        spans = _balanced_max_shot_spans(30883, max_shot_ms=30000, min_shot_ms=2000)
        assert len(spans) == 2
        assert spans[0] == (0, 15442)
        assert spans[1] == (15442, 30883)
        for s, e in spans:
            assert e - s >= 2000

    def test_span_within_max_single(self) -> None:
        assert _balanced_max_shot_spans(30000, max_shot_ms=30000, min_shot_ms=2000) == [(0, 30000)]

    def test_contiguous_and_full_coverage(self) -> None:
        spans = _balanced_max_shot_spans(69000, max_shot_ms=30000, min_shot_ms=2000)
        assert spans[0][0] == 0
        assert spans[-1][1] == 69000
        for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
            assert e1 == s2, "切分必须无缝"

    def test_all_within_max_shot(self) -> None:
        for s, e in _balanced_max_shot_spans(69000, max_shot_ms=30000, min_shot_ms=2000):
            assert e - s <= 30000

    def test_tiny_span_allows_single_short(self) -> None:
        """总长不足两段 min_shot:允许单短段(调用方以 method 标记原因)。"""
        assert _balanced_max_shot_spans(1500, max_shot_ms=30000, min_shot_ms=2000) == [(0, 1500)]

    def test_clips_from_cuts_end_to_end(self) -> None:
        """端到端:30883ms 单切点序列 → 2 段,无 <2000ms 残尾。"""
        from storyline_capabilities.split_shots import _clips_from_cuts

        clips = _clips_from_cuts(
            [0, 30883],
            video_path="x.mov",
            method="transnet",
            method_max_split="transnet_max_split",
            min_shot_ms=2000,
            max_shot_ms=30000,
        )
        assert len(clips) == 2
        for c in clips:
            assert c["timeline_out_ms"] - c["timeline_in_ms"] >= 2000
        assert clips[-1]["source_out_ms"] == 30883
