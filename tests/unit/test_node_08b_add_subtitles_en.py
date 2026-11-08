"""node_08b_add_subtitles_en 单测 — 空输入跳过语义(B1 ASR 移除后英文线安全放行)。

B1 ASR 移除后 ``subtitle_segments_en`` 为空成为英文分支的**常态输入**
(16a 无源字幕 → 正常 skip → 08b 收到空段)。空段必须是预期跳过而非错误;
目录/草稿文件缺失仍是真实配置故障,保持 error_log 语义。
"""

from __future__ import annotations

import json
from pathlib import Path

from nodes.node_08b_add_subtitles_en import add_subtitles_en


def test_node_08b_empty_segments_skips_without_error(tmp_path: Path) -> None:
    """``subtitle_segments_en`` 为空 → 只记 skip 状态,不写 error_log、不抛异常。"""
    state = {"draft_dir_en_branch": str(tmp_path), "status_log": [], "error_log": []}
    out = add_subtitles_en(state)

    assert "node_08b_skipped_no_en_segments" in (out.get("status_log") or [])
    assert not out.get("error_log")


def test_node_08b_missing_segments_key_skips_without_error(tmp_path: Path) -> None:
    """state 完全没有 ``subtitle_segments_en`` 键 → 与空列表同路径,正常跳过。"""
    state = {"draft_dir_en_branch": str(tmp_path), "status_log": [], "error_log": []}
    out = add_subtitles_en(state)

    assert "node_08b_skipped_no_en_segments" in (out.get("status_log") or [])
    assert not out.get("error_log")


def test_node_08b_missing_draft_dir_is_config_error() -> None:
    """``draft_dir_en_branch`` 缺失 → 属配置错误,保持 error_log 语义。"""
    state = {"status_log": [], "error_log": []}
    out = add_subtitles_en(state)

    assert any("draft_dir_en_branch" in e for e in out["error_log"])


def test_node_08b_missing_draft_file_is_error(tmp_path: Path) -> None:
    """英文分支目录存在但草稿文件缺失 → error_log(真实故障)。"""
    en_branch = tmp_path / "en_branch"
    en_branch.mkdir()
    state = {
        "draft_dir_en_branch": str(en_branch),
        "subtitle_segments_en": [{"index": 0, "start_ms": 0, "end_ms": 1_000, "text_en": "Hi"}],
        "status_log": [],
        "error_log": [],
    }
    out = add_subtitles_en(state)

    assert any("英文分支草稿不存在" in e for e in out["error_log"])


# ---------------------------------------------------------------------------
# P4(决定 2 选 A):storyline 推荐样式叠加到英文字幕(唯一真实落草稿的通道)
# ---------------------------------------------------------------------------
def _write_en_draft(tmp_path: Path) -> Path:
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {},
        "tracks": [{"type": "video", "segments": []}],
    }
    p = tmp_path / "en_branch" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _segments() -> list[dict]:
    return [
        {"index": 0, "start_ms": 0, "end_ms": 1_500, "text_en": "Hello"},
        {"index": 1, "start_ms": 1_500, "end_ms": 3_000, "text_en": "World"},
    ]


def test_node_08b_applies_storyline_style_to_en_texts(tmp_path: Path) -> None:
    """有推荐 → 字号/颜色/位置真实落草稿(渲染路径已核实),状态日志记说明。"""
    from nodes.node_08_add_subtitles import _JIANYING_DEFAULT_STYLE

    en_file = _write_en_draft(tmp_path)
    plan = {
        "default": {
            "font_en": "Roboto-Bold.ttf",
            "size": 48,
            "color": "#FF0000",
            "position": "top",
        }
    }
    state = {
        "draft_dir_en_branch": str(en_file.parent),
        "subtitle_segments_en": _segments(),
        "storyline_text_style_plan": plan,
        "status_log": [],
        "error_log": [],
    }
    out = add_subtitles_en(state)

    assert "node_08b_add_subtitles_en_done" in out["status_log"]
    assert any("[node_08b] 字幕样式:" in entry for entry in out["status_log"])

    draft = json.loads(en_file.read_text(encoding="utf-8"))
    texts = draft["materials"]["texts"]
    assert len(texts) == 2
    style = texts[0]["style"]
    assert style["size"] == 10.0  # 48/24 = 2.0 → 触顶夹取
    assert style["transform_y"] == 0.8
    assert style["color"] == [1.0, 0.0, 0.0]
    assert style["bold"] == _JIANYING_DEFAULT_STYLE["bold"]
    # 原生 content JSON 同步了 size(渲染路径)
    content = json.loads(texts[0]["content"])
    assert content["styles"][0]["size"] == 10.0


def test_node_08b_without_plan_keeps_default_style(tmp_path: Path) -> None:
    """无推荐 → 与既有行为逐字段一致(默认样式),无样式说明日志。"""
    from nodes.node_08_add_subtitles import _JIANYING_DEFAULT_STYLE

    en_file = _write_en_draft(tmp_path)
    state = {
        "draft_dir_en_branch": str(en_file.parent),
        "subtitle_segments_en": _segments(),
        "status_log": [],
        "error_log": [],
    }
    out = add_subtitles_en(state)

    draft = json.loads(en_file.read_text(encoding="utf-8"))
    style = draft["materials"]["texts"][0]["style"]
    assert style["size"] == _JIANYING_DEFAULT_STYLE["size"]
    assert style["color"] == _JIANYING_DEFAULT_STYLE["color"]
    assert style["transform_y"] == _JIANYING_DEFAULT_STYLE["transform_y"]
    assert not any("[node_08b] 字幕样式:" in e for e in out["status_log"])


def test_node_08b_font_unavailable_note_only(tmp_path: Path) -> None:
    """推荐字体资源库不存在 → 只记 note,样式不变。"""
    en_file = _write_en_draft(tmp_path)
    plan = {
        "default": {
            "font_en": "NoSuchFont-Bold.ttf",
            "size": 24,
            "color": "#FFFFFF",
            "position": "bottom",
        }
    }
    state = {
        "draft_dir_en_branch": str(en_file.parent),
        "subtitle_segments_en": _segments(),
        "storyline_text_style_plan": plan,
        "status_log": [],
        "error_log": [],
    }
    out = add_subtitles_en(state)

    draft = json.loads(en_file.read_text(encoding="utf-8"))
    style = draft["materials"]["texts"][0]["style"]
    assert style["size"] == 5.0 and style["transform_y"] == -0.8
    assert any("NoSuchFont-Bold.ttf" in e and "不存在" in e for e in out["status_log"])
