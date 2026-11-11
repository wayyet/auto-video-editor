"""PTAT 物化单测(2026-11-12 noop 治理阶段2b,设计文档 §2.4)。

覆盖:generated 条目物化为 SourceMedia+Clip、时间轴顺序重排、速度/单调不变量、
预算吸收(不静默拉长)、装不下时从最靠后的转场开始放弃、不相邻/产物丢失不物化、
失败回退基础时间线。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nodes.storyline import node_plan_timeline_ai_transition as ptat_mod
from nodes.storyline.node_plan_timeline_ai_transition import (
    _materialize,
    _shrink,
    storyline_plan_timeline_ai_transition_node,
)
from storyline.contract import CanonicalTimeline


def _video_file(tmp_path: Path, name: str = "t.mp4") -> str:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake mp4")
    return str(p)


def _base_timeline(*, budget: int | None = 35000, n_clips: int = 3, span: int = 4000) -> dict:
    clips = [
        {
            "clip_id": f"c{i + 1}",
            "source_media_id": "m1",
            "source_in_ms": i * span,
            "source_out_ms": (i + 1) * span,
            "timeline_in_ms": i * span,
            "timeline_out_ms": (i + 1) * span,
            "transcript": f"字幕c{i + 1}",
        }
        for i in range(n_clips)
    ]
    options: dict[str, Any] = {"enable_ai_transition": False, "enable_voiceover": True}
    if budget is not None:
        options["max_duration_ms"] = budget
    return {
        "schema_version": "1.0",
        "job_id": "unit",
        "created_at_ms": 1,
        "source_media": [
            {"media_id": "m1", "file_uri": "file:///C:/a.mp4", "duration_ms": 60000}
        ],
        "clips": clips,
        "audio": {"bgm_ref": None, "voiceover": None},
        "subtitles": {"zh": "整段中文文案", "en": None},
        "options": options,
        "method": "tts_align",
        "warnings": [],
    }


def _item(a: str, b: str, *, duration_ms: int = 500, video: str | None = "x",
          status: str = "generated", **over: Any) -> dict:
    d = {
        "from_clip_id": a,
        "to_clip_id": b,
        "duration_ms": duration_ms,
        "provider": "minimax",
        "is_stub": False,
        "error": None,
        "artifact": video,
        "video_path": video,
        "status": status,
        "cache_hit": False,
    }
    d.update(over)
    return d


# ---------------------------------------------------------------------------
# 基础物化
# ---------------------------------------------------------------------------
def test_materializes_generated_item_between_adjacent_clips(tmp_path: Path) -> None:
    video = _video_file(tmp_path)
    base = _base_timeline()
    merged, warnings = _materialize(base, [_item("c1", "c2", duration_ms=500, video=video)])  # type: ignore[arg-type]
    assert merged is not None and warnings == []

    clips = merged["clips"]
    assert [c["clip_id"] for c in clips] == ["c1", "transition_0001", "c2", "c3"]
    t = clips[1]
    assert t["source_in_ms"] == 0 and t["source_out_ms"] == 500
    assert t["ai_transition_pair"] == "c1->c2"
    assert t["semantic_tags"] == ["ai_transition"]
    # 时间轴顺序重排:0-4000 / 4000-4500 / 4500-8500 / 8500-12500(总长被吸收策略
    # 保持 ≤ 预算;35000 宽裕 → 不吸收,直接顺排)
    assert [c["timeline_in_ms"] for c in clips] == [0, 4000, 4500, 8500]
    assert clips[-1]["timeline_out_ms"] == 12500
    # 新媒体条目
    assert len(merged["source_media"]) == 2
    new_media = merged["source_media"][1]
    assert new_media["media_id"] == t["source_media_id"]
    assert new_media["media_id"].startswith("transition_media_")
    assert new_media["file_uri"].startswith("file:///")
    assert new_media["duration_ms"] == 500
    assert merged["options"]["enable_ai_transition"] is True
    # 审计清单保留原条目;契约校验通过
    assert merged["ai_transitions"][0]["from_clip_id"] == "c1"
    CanonicalTimeline.model_validate(merged)
    # 原片段 transcript 不丢
    assert [c.get("transcript") for c in clips if not c["clip_id"].startswith("transition_")] == [
        "字幕c1", "字幕c2", "字幕c3",
    ]


def test_degraded_items_stay_audit_only(tmp_path: Path) -> None:
    video = _video_file(tmp_path)
    base = _base_timeline()
    items = [
        _item("c1", "c2", status="degraded", video=None),
        _item("c2", "c3", duration_ms=600, video=video),
    ]
    merged, warnings = _materialize(base, items)  # type: ignore[arg-type]
    assert merged is not None
    # 只有 c2->c3 被物化(转场片段插在 c2 与 c3 之间)
    ids = [c["clip_id"] for c in merged["clips"]]
    assert "transition_0001" in ids
    pairs = [c["ai_transition_pair"] for c in merged["clips"] if c.get("ai_transition_pair")]
    assert pairs == ["c2->c3"]
    assert merged["options"]["enable_ai_transition"] is True
    # 审计清单保留 degraded 条目
    assert merged["ai_transitions"][0]["status"] == "degraded"


def test_missing_video_file_is_not_materialized(tmp_path: Path) -> None:
    base = _base_timeline()
    merged, _warnings = _materialize(
        base, [_item("c1", "c2", video=str(tmp_path / "ghost.mp4"))]  # type: ignore[arg-type]
    )
    assert merged is None  # 全部不可物化 → 走审计-only


def test_non_adjacent_pair_not_materialized_with_warning(tmp_path: Path) -> None:
    video = _video_file(tmp_path)
    base = _base_timeline()
    merged, warnings = _materialize(
        base, [_item("c1", "c3", duration_ms=500, video=video)]  # type: ignore[arg-type]
    )
    assert merged is None  # 唯一条目不相邻 → 无可物化
    assert any("不相邻" in w for w in warnings)


# ---------------------------------------------------------------------------
# 预算吸收(不静默拉长成片)
# ---------------------------------------------------------------------------
def test_budget_absorption_keeps_total_within_max(tmp_path: Path) -> None:
    video = _video_file(tmp_path)
    base = _base_timeline(budget=12500)  # 底 12000 + 2000 转场 → 必须吸收 1500
    merged, warnings = _materialize(base, [_item("c1", "c2", duration_ms=2000, video=video)])  # type: ignore[arg-type]
    assert merged is not None and warnings == []
    total = merged["clips"][-1]["timeline_out_ms"]
    assert total <= 12500
    # 逐段不低于吸收下限,速度不变量仍成立
    for c in merged["clips"]:
        span = c["timeline_out_ms"] - c["timeline_in_ms"]
        assert span >= 200
    CanonicalTimeline.model_validate(merged)  # 单调 + 速度不变量兜底


def test_budget_too_tight_drops_latest_transition_first(tmp_path: Path) -> None:
    video = _video_file(tmp_path)
    base = _base_timeline(budget=9000, n_clips=4)  # 底 16000;3×3000 装不下
    items = [
        _item("c1", "c2", duration_ms=3000, video=video),
        _item("c2", "c3", duration_ms=3000, video=video),
        _item("c3", "c4", duration_ms=3000, video=video),
    ]
    merged, warnings = _materialize(base, items)  # type: ignore[arg-type]
    assert merged is not None
    total = merged["clips"][-1]["timeline_out_ms"]
    assert total <= 9000
    placed = [c for c in merged["clips"] if c.get("ai_transition_pair")]
    assert len(placed) == 2, "最靠后的转场先被放弃"
    assert any("被放弃" in w for w in warnings)
    CanonicalTimeline.model_validate(merged)


def test_no_budget_option_relays_without_absorption(tmp_path: Path) -> None:
    video = _video_file(tmp_path)
    base = _base_timeline(budget=None)
    merged, _ = _materialize(base, [_item("c1", "c2", duration_ms=800, video=video)])  # type: ignore[arg-type]
    assert merged is not None
    total = merged["clips"][-1]["timeline_out_ms"]
    assert total == 12000 + 800  # 无上限 → 不吸收,直接加


# ---------------------------------------------------------------------------
# _shrink 均摊
# ---------------------------------------------------------------------------
def test_shrink_proportional_with_floor() -> None:
    spans, taken = _shrink([4000, 4000, 4000], 1500, 200)
    assert taken == 1500
    assert sum(spans) == 10500
    assert all(s >= 200 for s in spans)


def test_shrink_caps_at_floor_when_insufficient() -> None:
    spans, taken = _shrink([300, 300], 1000, 200)
    assert taken == 200  # 只能抽出 2×(300-200)
    assert spans == [200, 200]


# ---------------------------------------------------------------------------
# 节点级:失败回退 / 审计-only
# ---------------------------------------------------------------------------
def _plan_state(tmp_path: Path, items: list[dict], base: dict) -> dict:
    out = tmp_path / "storyline"
    out.mkdir(parents=True, exist_ok=True)
    plan = out / "timeline_plan.json"
    plan.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    artifact = out / "ai_transition.json"
    artifact.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    return {
        "session_id": "unit",
        "storyline_outputs_root": str(tmp_path),
        "status_log": [],
        "error_log": [],
        "storyline_timeline_plan": str(plan),
        "storyline_ai_transition_artifact": str(artifact),
    }


def test_node_materialization_writes_plan_and_keeps_base(tmp_path: Path) -> None:
    video = _video_file(tmp_path)
    base = _base_timeline()
    state = _plan_state(tmp_path, [_item("c1", "c2", duration_ms=500, video=video)], base)
    out = storyline_plan_timeline_ai_transition_node(state)
    assert out["status_log"] == ["storyline_plan_timeline_ai_transition_done"]
    new_path = Path(out["storyline_timeline_plan"])
    assert new_path.name == "timeline_plan_ai.json"
    merged = json.loads(new_path.read_text(encoding="utf-8"))
    assert any(c.get("ai_transition_pair") == "c1->c2" for c in merged["clips"])
    # 基础时间线原封不动
    assert json.loads(Path(state["storyline_timeline_plan"]).read_text(encoding="utf-8")) == base


def test_node_audit_only_sets_enable_false(tmp_path: Path) -> None:
    base = _base_timeline()
    state = _plan_state(tmp_path, [_item("c1", "c2", status="degraded", video=None)], base)
    out = storyline_plan_timeline_ai_transition_node(state)
    merged = json.loads(Path(out["storyline_timeline_plan"]).read_text(encoding="utf-8"))
    assert merged["options"]["enable_ai_transition"] is False
    assert merged["clips"] == base["clips"]  # 未物化,clips 不变
    assert len(merged["ai_transitions"]) == 1


def test_node_materialize_crash_keeps_base_and_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    video = _video_file(tmp_path)
    base = _base_timeline()

    def boom(_base: dict, _items: list[dict]) -> dict:
        raise RuntimeError("物化炸了")

    monkeypatch.setattr(ptat_mod, "_materialize", boom)
    state = _plan_state(tmp_path, [_item("c1", "c2", duration_ms=500, video=video)], base)
    out = storyline_plan_timeline_ai_transition_node(state)
    assert out["status_log"] == ["storyline_plan_timeline_ai_transition_failed"]
    assert "storyline_timeline_plan" not in out
    assert any("[storyline:plan_timeline_ai_transition]" in e for e in out["error_log"])
    assert not (tmp_path / "storyline" / "timeline_plan_ai.json").exists()
