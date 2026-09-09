"""node_checkpoint3_layout_review 单测 — Week 5 关卡③ 节点(纯 interrupt)。

覆盖:
- interrupt payload 含 checkpoint="③" / step=16 / legacy_id / issues / draft_dir
- resume 后从草稿读修正结果,写 subtitle_segments_en + checkpoint3_triggered=True
- 完全无副作用:interrupt 之前/之后都不写 marker / 不调翻译
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nodes.node_checkpoint3_layout_review import node_checkpoint3_layout_review


def _seed_en_branch_with_fix(tmp_path: Path) -> Path:
    """预置 en_branch 草稿(模拟人工已修正 layout)。"""
    draft_dir = tmp_path / "en_branch"
    draft_dir.mkdir()
    draft = {
        "canvas_config": {},
        "materials": {
            "texts": [
                # 故意写短文本以避开 layout 异常
                {"content": "Hi", "target_timerange": {"start": 0, "duration": 2_000_000}},
                {"content": "World", "target_timerange": {"start": 2_000_000, "duration": 2_000_000}},
            ]
        },
    }
    (draft_dir / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return draft_dir


def _state(draft_dir: Path, *, with_issues: bool = True) -> dict:
    issues = (
        [{"index": 0, "reason": "text_too_wide", "width_px": 2000, "max_width_px": 1500}]
        if with_issues
        else []
    )
    return {
        "layout_issues": issues,
        "layout_issues_detected": with_issues,
        "draft_dir_en_branch": str(draft_dir),
        "status_log": [],
        "error_log": [],
    }


def test_checkpoint3_interrupt_payload_structure(tmp_path: Path) -> None:
    """interrupt payload 含完整字段。"""
    draft_dir = tmp_path / "en_branch"
    draft_dir.mkdir()
    captured: list[Any] = []

    def fake_interrupt(payload: Any) -> None:
        captured.append(payload)
        # 不抛,模拟 LangGraph resume 已发生
        return None

    with patch("nodes.node_checkpoint3_layout_review.interrupt", side_effect=fake_interrupt):
        state = _state(draft_dir, with_issues=True)
        out = node_checkpoint3_layout_review(state)

    assert len(captured) == 1
    p = captured[0]
    assert p["checkpoint"] == "③"
    assert p["legacy_id"] == "checkpoint3_layout_review"
    assert p["step"] == 16
    assert "英文字幕排版异常" in p["reason"]
    assert p["issues"] == state["layout_issues"]
    assert p["draft_dir"] == str(draft_dir)
    # resume 后字段
    assert out["checkpoint3_triggered"] is True
    assert "checkpoint3_resumed" in out["status_log"]


def test_checkpoint3_resume_writes_srt(tmp_path: Path) -> None:
    """resume 后:从草稿读修正后的 segments 并写 SRT(Week 5 修正版)。

    Week 5 设计:本节点不写 ``subtitle_segments_en``(由 16a 写,避免 LastValue
    并发写冲突);只写 SRT + checkpoint3_triggered=True + status_log。
    """
    draft_dir = _seed_en_branch_with_fix(tmp_path)
    captured: list[Any] = []

    def fake_interrupt(payload: Any) -> None:
        captured.append(payload)
        return None

    with patch("nodes.node_checkpoint3_layout_review.interrupt", side_effect=fake_interrupt):
        state = _state(draft_dir, with_issues=True)
        out = node_checkpoint3_layout_review(state)

    # 关键:不应写 subtitle_segments_en(避免与 16a 在同 tick 冲突)
    assert "subtitle_segments_en" not in out
    assert out["checkpoint3_triggered"] is True
    # SRT 应被写入并基于草稿当前内容
    srt_path = out.get("subtitle_srt_path")
    assert srt_path
    assert Path(srt_path).exists()
    content = Path(srt_path).read_text(encoding="utf-8")
    assert "Hi" in content or "World" in content


def test_checkpoint3_writes_srt_on_resume(tmp_path: Path) -> None:
    """resume 后写 SRT(Week 5 设计:16a 不写 SRT,checkpoint3 resume 后写)。"""
    draft_dir = _seed_en_branch_with_fix(tmp_path)
    with patch("nodes.node_checkpoint3_layout_review.interrupt", lambda payload: None):
        state = _state(draft_dir, with_issues=True)
        out = node_checkpoint3_layout_review(state)
    srt_path = out.get("subtitle_srt_path")
    assert srt_path
    assert Path(srt_path).exists()
    content = Path(srt_path).read_text(encoding="utf-8")
    assert "Hi" in content or "World" in content


def test_checkpoint3_resume_reads_draft(tmp_path: Path) -> None:
    """同 test_checkpoint3_resume_writes_srt — 旧名保留以便测试发现。

    Week 5 设计调整:checkpoint3 不再写 subtitle_segments_en(避免与 16a
    LastValue 并发写冲突),改为只写 SRT。本测试验证 SRT 内容来自草稿。
    """
    draft_dir = _seed_en_branch_with_fix(tmp_path)
    captured: list[Any] = []

    def fake_interrupt(payload: Any) -> None:
        captured.append(payload)
        return None

    with patch("nodes.node_checkpoint3_layout_review.interrupt", side_effect=fake_interrupt):
        state = _state(draft_dir, with_issues=True)
        out = node_checkpoint3_layout_review(state)

    # 不写 subtitle_segments_en
    assert "subtitle_segments_en" not in out
    # SRT 含修正后的草稿内容
    srt = Path(out["subtitle_srt_path"]).read_text(encoding="utf-8")
    assert "Hi" in srt or "World" in srt


def test_checkpoint3_no_side_effects_when_no_draft_dir(tmp_path: Path) -> None:
    """无 draft_dir 时:仍 trigger interrupt,但 resume 后 SRT 不写。"""
    captured: list[Any] = []

    def fake_interrupt(payload: Any) -> None:
        captured.append(payload)
        return None

    with patch("nodes.node_checkpoint3_layout_review.interrupt", side_effect=fake_interrupt):
        state = {
            "layout_issues": [{"index": 0, "reason": "text_too_wide"}],
            "layout_issues_detected": True,
            "draft_dir_en_branch": None,
            "status_log": [],
            "error_log": [],
        }
        out = node_checkpoint3_layout_review(state)

    assert len(captured) == 1
    assert out["checkpoint3_triggered"] is True
    # 不应写 subtitle_segments_en
    assert "subtitle_segments_en" not in out
    # SRT 不应被写(没有 draft_dir)
    assert out.get("subtitle_srt_path") is None
