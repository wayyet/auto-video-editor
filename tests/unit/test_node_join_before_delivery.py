"""join_before_delivery 单测 — 3 种 QA 场景。"""

from __future__ import annotations

from nodes.node_join_before_delivery import join_before_delivery


def _make_cover(ratio: str, en_path: str | None) -> dict:
    return {"ratio": ratio, "zh_path": f"/path/{ratio}_zh.png", "en_path": en_path}


def test_join_happy_path_no_issues() -> None:
    """3 张封面(含 en_path)+ 英文字幕 → 0 issues。"""
    state = {
        "covers": [
            _make_cover("9:16", "/p/9x16_en.png"),
            _make_cover("16:9", "/p/16x9_en.png"),
            _make_cover("4:3", "/p/4x3_en.png"),
        ],
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert out["join_qa_issues"] == []
    assert "join_before_delivery_done" in out["status_log"]


def test_join_missing_en_cover() -> None:
    """某张封面缺 en_path → issues 含 "封面英文版缺失"。"""
    state = {
        "covers": [
            _make_cover("9:16", "/p/9x16_en.png"),
            _make_cover("16:9", None),  # 缺
            _make_cover("4:3", "/p/4x3_en.png"),
        ],
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert any("封面英文版缺失" in i and "16:9" in i for i in out["join_qa_issues"])
    # error_log 也应有对应条目
    assert any("封面英文版缺失" in e for e in out["error_log"])


def test_join_missing_all_covers() -> None:
    """covers 为空 → issues 含 "封面缺失:需3种比例"。"""
    state = {
        "covers": [],
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert any("封面缺失" in i for i in out["join_qa_issues"])


def test_join_missing_subtitle() -> None:
    """英文字幕文件缺失 → issues 含 "英文字幕文件缺失"。"""
    state = {
        "covers": [
            _make_cover("9:16", "/p/9x16_en.png"),
            _make_cover("16:9", "/p/16x9_en.png"),
            _make_cover("4:3", "/p/4x3_en.png"),
        ],
        "subtitle_srt_path": None,
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert any("英文字幕文件缺失" in i for i in out["join_qa_issues"])


def test_join_preserves_existing_state() -> None:
    """汇合节点只返回 delta;上游字段(如 draft_path)由 LangGraph 自动保留。"""
    state = {
        "covers": [_make_cover("9:16", "/p/_en.png")],
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "status_log": ["prev"],
        "error_log": ["prev_err"],
        "draft_path": "/x/y.json",
    }
    out = join_before_delivery(state)
    # Week 4:delta-only 返回;LangGraph 运行时只更新返回的 keys
    assert "join_before_delivery_done" in out["status_log"]
    assert "join_qa_issues" in out
    # 注:delta 返回不含 draft_path 是预期的;LangGraph 合并时保留
    assert "draft_path" not in out  # 验证是 delta-only