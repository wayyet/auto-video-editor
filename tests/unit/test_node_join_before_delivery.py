"""join_before_delivery 单测 — 3 种 QA 场景。"""

from __future__ import annotations

import json
from pathlib import Path

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


# ---------------------------------------------------------------------------
# K2=B:英文草稿尾链四项检查
# ---------------------------------------------------------------------------
def _ok_covers() -> list[dict]:
    return [
        _make_cover("9:16", "/p/9x16_en.png"),
        _make_cover("16:9", "/p/16x9_en.png"),
        _make_cover("4:3", "/p/4x3_en.png"),
    ]


def _en_draft(**materials_and_tracks) -> dict:
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": materials_and_tracks.get("materials", {}),
        "tracks": materials_and_tracks.get("tracks", []),
    }


def _write_en(tmp_path: Path, draft: dict) -> Path:
    en_dir = tmp_path / "en_branch"
    en_dir.mkdir(parents=True, exist_ok=True)
    f = en_dir / "draft_content.json"
    f.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return en_dir


def test_join_en_draft_complete_no_issues(tmp_path: Path) -> None:
    """英文草稿尾链齐全(转场/特效/贴纸/BGM 音量)→ 无新增 issue。"""
    en_dir = _write_en(tmp_path, _en_draft(
        materials={
            "transitions": [{"id": "transition-1"}],
            "video_effects": [{"id": "vfx-default"}],
            "stickers": [{"id": "sticker-t1"}],
            "audios": [{"id": "audio-bgm-1", "volume": 0.35}],
        },
        tracks=[{"type": "audio", "segments": [{"id": "audio-bgm-1"}]}],
    ))
    state = {
        "covers": _ok_covers(),
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "draft_path": str(tmp_path / "zh" / "draft_content.json"),
        "draft_dir_en_branch": str(en_dir),
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert out["join_qa_issues"] == [], out["join_qa_issues"]


def test_join_en_draft_missing_tail_chain_items(tmp_path: Path) -> None:
    """英文草稿转场/特效/贴纸全空 → 报 3 条 issue。"""
    en_dir = _write_en(tmp_path, _en_draft())
    state = {
        "covers": _ok_covers(),
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "draft_path": str(tmp_path / "zh" / "draft_content.json"),
        "draft_dir_en_branch": str(en_dir),
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    joined = " ".join(out["join_qa_issues"])
    assert "英文草稿转场缺失" in joined
    assert "英文草稿视频特效缺失" in joined
    assert "英文草稿贴纸缺失" in joined


def test_join_en_draft_audio_track_without_volume(tmp_path: Path) -> None:
    """英文草稿有音轨但没写音量 → 报"音量未写入"。"""
    en_dir = _write_en(tmp_path, _en_draft(
        materials={"audios": [{"id": "audio-bgm-1"}]},
        tracks=[{"type": "audio", "segments": [{"id": "audio-bgm-1"}]}],
    ))
    state = {
        "covers": _ok_covers(),
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "draft_path": str(tmp_path / "zh" / "draft_content.json"),
        "draft_dir_en_branch": str(en_dir),
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert any("音量未写入" in i for i in out["join_qa_issues"]), out["join_qa_issues"]


def test_join_en_draft_missing_file_is_reported(tmp_path: Path) -> None:
    """英文草稿目录指向不存在的路径 → 报"英文分支草稿缺失"。"""
    state = {
        "covers": _ok_covers(),
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "draft_path": str(tmp_path / "zh" / "draft_content.json"),
        "draft_dir_en_branch": str(tmp_path / "nope"),
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert any("英文分支草稿缺失" in i for i in out["join_qa_issues"]), out["join_qa_issues"]


def test_join_skips_en_checks_when_no_en_branch() -> None:
    """从未走英文分支(draft_dir_en_branch 为空)→ 整段跳过,不产生 issues。"""
    state = {
        "covers": _ok_covers(),
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "draft_path": "/x/y.json",
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert out["join_qa_issues"] == []