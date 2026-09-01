"""节点 7 护栏节点单测 — 帧对齐分配公式 + 条件边路由。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_07_speed_fit import (
    _frame_aligned_durations,
    route_after_speed_fit,
    speed_fit,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_draft_with_segments(durations_us: list[int], fps: int = 30) -> dict:
    """构造一段视频草稿,segments 各自带 target_timerange."""
    segments = []
    cursor = 0
    for i, dur in enumerate(durations_us):
        segments.append({
            "id": f"seg-{i + 1}",
            "target_timerange": {"start": cursor, "duration": dur},
        })
        cursor += dur
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": cursor,
        "materials": {},
        "tracks": [{"type": "video", "fps": fps, "segments": segments}],
    }


@pytest.fixture
def tmp_draft(tmp_path: Path) -> Path:
    """返回一份 60s 草稿的 draft_content.json 路径。"""
    draft = _make_draft_with_segments([10_000_000] * 6)  # 60s,6 段各 10s
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


@pytest.fixture
def tmp_draft_35s(tmp_path: Path) -> Path:
    """恰好 35s 草稿 — 节点 7 应判定已达标,不动。"""
    draft = _make_draft_with_segments([5_000_000] * 7)  # 35s
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 帧对齐分配公式
# ---------------------------------------------------------------------------
def test_frame_aligned_basic() -> None:
    """60s → 35s,3 段均分,总和不超 35s。"""
    src = [20_000_000, 20_000_000, 20_000_000]
    new = _frame_aligned_durations(src, 35_000_000, fps=30)
    assert len(new) == 3
    assert sum(new) <= 35_000_000


def test_frame_aligned_last_segment_absorbs_remainder() -> None:
    """末段吸收帧数余数 — 总和严格 ≤ 目标。"""
    src = [10_000_000, 10_000_000, 10_000_000]  # 30s
    new = _frame_aligned_durations(src, 35_000_000, fps=30)
    # 35s * 30 fps = 1050 frames;按比例 350/段,末段吸收余数
    assert sum(new) <= 35_000_000
    assert all(d > 0 for d in new)


def test_frame_aligned_single_segment() -> None:
    """单段:整段变速,末段独占。"""
    src = [60_000_000]
    new = _frame_aligned_durations(src, 35_000_000, fps=30)
    assert new[0] <= 35_000_000
    assert new[0] > 0


# ---------------------------------------------------------------------------
# speed_fit 节点函数
# ---------------------------------------------------------------------------
def test_speed_fit_under_target_no_change(tmp_draft_35s: Path) -> None:
    """35s 草稿:节点只递增 retry_counts,不写新 speed。"""
    state = {"draft_path": str(tmp_draft_35s), "retry_counts": {}, "status_log": [], "error_log": []}
    out = speed_fit(state)
    assert out["retry_counts"]["node_07"] == 1
    # 草稿未被改写(写入的内容应等价)
    draft_after = json.loads(tmp_draft_35s.read_text(encoding="utf-8"))
    assert draft_after["duration"] == 35_000_000


def test_speed_fit_compresses_60s_to_under_35s(tmp_draft: Path) -> None:
    """60s → 应变速压缩到 ≤ 35s,且 materials.speeds 非空。"""
    state = {"draft_path": str(tmp_draft), "retry_counts": {}, "status_log": [], "error_log": []}
    out = speed_fit(state)

    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert draft["duration"] <= 35_000_000
    assert draft["materials"]["speeds"], "speeds material 应被创建"
    # 每段应有 speed material 引用
    for seg in draft["tracks"][0]["segments"]:
        assert "speed" in seg
        refs = seg.get("extra_material_refs", [])
        assert any(
            any(sm.get("id") == ref for sm in draft["materials"]["speeds"])
            for ref in refs
        )
    assert out["retry_counts"]["node_07"] == 1


def test_speed_fit_idempotent(tmp_draft: Path) -> None:
    """第二次进入:src_total 已 ≤ target,只递增 retry,不重复变速。"""
    # 第一次跑完
    speed_fit({"draft_path": str(tmp_draft), "retry_counts": {}, "status_log": [], "error_log": []})
    speeds_count_after_first = len(json.loads(tmp_draft.read_text(encoding="utf-8"))["materials"].get("speeds", []))

    # 第二次
    out = speed_fit({"draft_path": str(tmp_draft), "retry_counts": {"node_07": 1}, "status_log": [], "error_log": []})
    speeds_count_after_second = len(json.loads(tmp_draft.read_text(encoding="utf-8"))["materials"].get("speeds", []))

    assert speeds_count_after_first == speeds_count_after_second  # 没追加新 speed
    assert out["retry_counts"]["node_07"] == 2  # 但 retry 计数 +1


# ---------------------------------------------------------------------------
# 条件边路由
# ---------------------------------------------------------------------------
def test_route_after_speed_fit_under_target(tmp_draft_35s: Path) -> None:
    """达标 → node_08_add_subtitles。"""
    state = {"draft_path": str(tmp_draft_35s)}
    assert route_after_speed_fit(state) == "node_08_add_subtitles"


def test_route_after_speed_fit_escalate(tmp_draft: Path) -> None:
    """未达标 + retry >= MAX_RETRY → escalate_guardrail_failure。"""
    state = {"draft_path": str(tmp_draft), "retry_counts": {"node_07": 3}}
    # 注意:此处 retry_counts["node_07"]=3,实际阈值由 NODE_07_MAX_RETRY=3 决定;3 >= 3 应升级
    assert route_after_speed_fit(state) == "escalate_guardrail_failure"


def test_route_after_speed_fit_self_loop(tmp_draft: Path) -> None:
    """未达标 + retry < MAX_RETRY → 自循环。"""
    state = {"draft_path": str(tmp_draft), "retry_counts": {"node_07": 1}}
    assert route_after_speed_fit(state) == "node_07_speed_fit"