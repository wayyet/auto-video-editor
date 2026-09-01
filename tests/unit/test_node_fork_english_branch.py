"""fork_draft_for_english_branch 单测 — 复制独立性、幂等、目录命名。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_fork_english_branch import (
    _compute_target_dir,
    _copy_snapshot,
    _fork,
    fork_draft_for_english_branch,
)


def _seed_snapshot(snap_dir: Path) -> Path:
    """预置 snapshot② 目录(含 draft_content.json)。"""
    snap_dir.mkdir(parents=True, exist_ok=True)
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"videos": [{"id": "v1"}]},
        "tracks": [{"type": "video", "segments": [{"id": "s1"}]}],
    }
    draft_file = snap_dir / "draft_content.json"
    draft_file.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return draft_file


def _state(snapshot2_path: str, session_id: str = "t1", **extra) -> dict:
    return {
        "session_id": session_id,
        "snapshot2_path": snapshot2_path,
        "status_log": [],
        "error_log": [],
        **extra,
    }


# ---------------------------------------------------------------------------
# 纯函数测试
# ---------------------------------------------------------------------------
def test_compute_target_dir_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目标目录名应包含 session_id + 时间戳,父目录 = drafts_root.parent。"""
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts" / "default"))
    target = _compute_target_dir({"session_id": "alpha"})
    assert target.parent == tmp_path / "drafts"
    assert target.name.startswith("en_branch_alpha_")
    assert target.name.endswith(tuple(str(i) for i in range(0, 10)))


def test_copy_snapshot_recursive(tmp_path: Path) -> None:
    """递归复制:子目录 + 文件都应被复制。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "draft_content.json").write_text("{}", encoding="utf-8")
    sub = src / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested", encoding="utf-8")

    dst = tmp_path / "dst"
    _copy_snapshot(src, dst)

    assert (dst / "draft_content.json").exists()
    assert (dst / "sub" / "nested.txt").read_text(encoding="utf-8") == "nested"


def test_fork_returns_target_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_fork 应返回含 draft_dir_en_branch 字段的 dict。"""
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts" / "default"))
    snap = tmp_path / "snapshots" / "snapshot2"
    _seed_snapshot(snap)

    state = _state(str(snap / "draft_content.json"))
    out = _fork(state)

    assert "draft_dir_en_branch" in out
    target = Path(out["draft_dir_en_branch"])
    assert target.exists()
    assert (target / "draft_content.json").exists()


def test_fork_independence_from_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """复制后再修改中文主线**不**影响 en_branch。"""
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts" / "default"))
    snap = tmp_path / "snapshots" / "snapshot2"
    _seed_snapshot(snap)

    state = _state(str(snap / "draft_content.json"))
    out = _fork(state)
    target = Path(out["draft_dir_en_branch"])

    # 修改 snapshot② 的 draft_content.json
    snap_draft = snap / "draft_content.json"
    modified = json.loads(snap_draft.read_text(encoding="utf-8"))
    modified["materials"]["videos"].append({"id": "v2-modified"})
    snap_draft.write_text(json.dumps(modified), encoding="utf-8")

    # en_branch 的内容应保持原样
    en_draft = json.loads((target / "draft_content.json").read_text(encoding="utf-8"))
    assert en_draft["materials"]["videos"] == [{"id": "v1"}]


def test_fork_idempotent_when_target_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目标目录已存在且含 draft_content.json → 复用,返回同一路径。"""
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts" / "default"))
    snap = tmp_path / "snapshots" / "snapshot2"
    _seed_snapshot(snap)

    state = _state(str(snap / "draft_content.json"))
    out1 = _fork(state)
    out2 = _fork(state)

    assert out1["draft_dir_en_branch"] == out2["draft_dir_en_branch"]


def test_fork_no_snapshot_returns_none(tmp_path: Path) -> None:
    """snapshot2_path 缺失 → 返回 draft_dir_en_branch=None,不抛异常。"""
    state = _state(snapshot2_path="")  # 空字符串
    out = _fork(state)
    assert out["draft_dir_en_branch"] is None


def test_fork_invalid_snapshot_returns_none(tmp_path: Path) -> None:
    """snapshot2_path 指向不存在的路径 → 返回 None。"""
    state = _state(snapshot2_path=str(tmp_path / "missing.json"))
    out = _fork(state)
    assert out["draft_dir_en_branch"] is None


# ---------------------------------------------------------------------------
# 节点函数测试
# ---------------------------------------------------------------------------
def test_node_returns_status_log_and_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fork_draft_for_english_branch 应返回 status_log 含完成打点(Week 4 delta-only)。"""
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts" / "default"))
    snap = tmp_path / "snapshots" / "snapshot2"
    _seed_snapshot(snap)

    state = _state(str(snap / "draft_content.json"), status_log=["before"])
    out = fork_draft_for_english_branch(state)

    # Week 4:节点只返回 delta(由 reducer 合并到 state)
    assert any(s.startswith("node_fork_english_branch_done:") for s in out["status_log"])
    assert out["draft_dir_en_branch"]
    # 原 state 不应被污染(无返回值不含 "before")
    assert "before" not in out["status_log"]