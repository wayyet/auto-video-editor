"""node_08b_add_subtitles_en 单测 — 空输入跳过语义(B1 ASR 移除后英文线安全放行)。

B1 ASR 移除后 ``subtitle_segments_en`` 为空成为英文分支的**常态输入**
(16a 无源字幕 → 正常 skip → 08b 收到空段)。空段必须是预期跳过而非错误;
目录/草稿文件缺失仍是真实配置故障,保持 error_log 语义。
"""

from __future__ import annotations

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
