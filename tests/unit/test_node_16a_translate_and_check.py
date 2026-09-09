"""node_16a_translate_and_check 单测 — Week 5 新节点。

覆盖:
- marker 已存在 → 跳过翻译,只写 state
- marker 不存在 → 调翻译 + 写 marker + 写 state
- asr_segments_zh 缺失 → 跳过(error_log)
- draft_dir_en_branch 缺失 → 跳过(error_log)
- layout 校验写 state(不触发 interrupt)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jy_common.translate_client import MockTranslateClient, set_default_client
from nodes.node_16a_translate_and_check import node_16a_translate_and_check


def _seed_en_branch(tmp_path: Path) -> Path:
    draft_dir = tmp_path / "en_branch"
    draft_dir.mkdir()
    draft = {
        "canvas_config": {},
        "materials": {
            "texts": [
                {"content": "你好", "target_timerange": {"start": 0, "duration": 2_000_000}},
                {"content": "世界", "target_timerange": {"start": 2_000_000, "duration": 2_000_000}},
            ]
        },
    }
    (draft_dir / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return draft_dir


def _state(asr_segs: list[dict], draft_dir: Path) -> dict:
    return {
        "asr_segments_zh": asr_segs,
        "draft_dir_en_branch": str(draft_dir),
        "status_log": [],
        "error_log": [],
    }


def _normal_asr() -> list[dict]:
    return [
        {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_zh": "你好"},
        {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_zh": "世界"},
    ]


def test_node_16a_happy_path_writes_state_and_srt(tmp_path: Path) -> None:
    """正常路径:翻译 + 写 marker + 写 state + 写 SRT(Week 5 调整:16a 也写 SRT)。"""
    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())
    state = _state(_normal_asr(), draft_dir)
    out = node_16a_translate_and_check(state)

    # state 字段应有值
    assert out.get("subtitle_segments_en")
    assert out.get("layout_issues") == [] or out.get("layout_issues") is None
    assert out.get("layout_issues_detected") is False
    assert "node_16a_translate_done" in out["status_log"]
    # 不应触发 interrupt(payload=None 不应存在)
    assert "interrupt" not in out
    # Week 5:正常路径下也写 SRT(原 node_16 行为)
    srt_path = out.get("subtitle_srt_path")
    assert srt_path
    assert Path(srt_path).exists()


def test_node_16a_marker_exists_skips_translate(tmp_path: Path) -> None:
    """marker 已存在 → 不调翻译客户端,直接读草稿。"""
    draft_dir = _seed_en_branch(tmp_path)
    marker_data = [
        {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_en": "PreExisting"},
        {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_en": "PreExisting2"},
    ]
    (draft_dir / "subtitle_en.json").write_text(
        json.dumps(marker_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    class _ShouldNotCall:
        def translate(self, segments):
            raise AssertionError("marker 存在时不应调翻译")

    set_default_client(_ShouldNotCall())
    state = _state(_normal_asr(), draft_dir)
    out = node_16a_translate_and_check(state)
    assert out.get("subtitle_segments_en")


def test_node_16a_no_asr_segments_skips(tmp_path: Path) -> None:
    """asr_segments_zh 缺失 → 跳过,error_log 有说明。"""
    state = {
        "asr_segments_zh": [],
        "draft_dir_en_branch": str(tmp_path),
        "status_log": [],
        "error_log": [],
    }
    out = node_16a_translate_and_check(state)
    assert "node_16a_translate_skipped" in out["status_log"]
    assert any("asr_segments_zh" in e for e in out["error_log"])


def test_node_16a_no_draft_dir_skips() -> None:
    """draft_dir_en_branch 缺失 → 跳过。"""
    state = {
        "asr_segments_zh": _normal_asr(),
        "draft_dir_en_branch": None,
        "status_log": [],
        "error_log": [],
    }
    out = node_16a_translate_and_check(state)
    assert "node_16a_translate_skipped" in out["status_log"]
    assert any("draft_dir_en_branch" in e for e in out["error_log"])


def test_node_16a_layout_issues_writes_state_without_interrupt(tmp_path: Path) -> None:
    """layout 异常时:写 layout_issues + layout_issues_detected=True,但不调 interrupt。"""
    draft_dir = _seed_en_branch(tmp_path)
    issues_to_inject = [{"index": 0, "reason": "text_too_wide", "width_px": 2000}]

    with patch(
        "nodes.node_16a_translate_and_check.validate_layout",
        return_value=issues_to_inject,
    ):
        set_default_client(MockTranslateClient())
        state = _state(_normal_asr(), draft_dir)
        out = node_16a_translate_and_check(state)

    assert out.get("layout_issues") == issues_to_inject
    assert out.get("layout_issues_detected") is True
    # 关键:不应有任何 interrupt 调用(纯函数侧不抛,LangGraph 层也不调)
    assert "node_16a_translate_done" in out["status_log"]
