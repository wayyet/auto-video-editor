"""draft_ops.safe_write_guard 单测(Week 5 新增;对齐计划 §1.4 / §3.5)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from draft_ops.safe_write_guard import (
    check_jianying_not_running,
    cleanup_old_snapshots,
    restore_from_snapshot,
    snapshot_before_edit,
)


# ---------------------------------------------------------------------------
# check_jianying_not_running
# ---------------------------------------------------------------------------


def test_check_jianying_running_returns_true_when_proc_present() -> None:
    """注入 proc_query_fn 返回含 JianyingPro 的行 → True。"""

    def fake_query() -> list[str]:
        return [
            "JianyingPro.exe              12345 Console                    1    250,000 K",
        ]

    assert check_jianying_not_running(proc_query_fn=fake_query) is True


def test_check_jianying_not_running_returns_false_when_proc_absent() -> None:
    """proc_query_fn 返回空列表 → False。"""

    def fake_query() -> list[str]:
        return []

    assert check_jianying_not_running(proc_query_fn=fake_query) is False


def test_check_jianying_query_raises_returns_false() -> None:
    """proc_query_fn 抛异常 → 返回 False(不阻断)。"""

    def fake_query() -> list[str]:
        raise OSError("tasklist missing")

    assert check_jianying_not_running(proc_query_fn=fake_query) is False


def test_check_jianying_ignores_non_matching_lines() -> None:
    """proc_query_fn 返回不包含 JianyingPro 的行 → False。"""

    def fake_query() -> list[str]:
        return [
            "explorer.exe                 1234 Console                    1     12,345 K",
            "notepad.exe                  5678 Console                    1      4,567 K",
        ]

    assert check_jianying_not_running(proc_query_fn=fake_query) is False


def test_check_jianying_no_injection_does_not_crash() -> None:
    """不注入 proc_query_fn:实际跑 subprocess(可能没 JianyingPro),应返回 bool。"""
    # 在 Linux CI 上跑会走 pgrep(没装 pgrep 时返回 [] → False)。
    # 在 Windows 上跑会走 tasklist(可能也没装 JianyingPro → False)。
    # 不抛异常即可。
    result = check_jianying_not_running()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# snapshot_before_edit / restore_from_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_before_edit_copies_dir(tmp_path: Path) -> None:
    """draft_dir 存在 → 复制整目录到 .snapshots/<ts>/,内容一致。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text('{"a": 1}', encoding="utf-8")
    (draft_dir / "subtitle_en.json").write_text('[]', encoding="utf-8")

    snap = snapshot_before_edit(draft_dir, ts="2026-09-20T00-00-00Z")
    assert snap.exists()
    assert (snap / "draft_content.json").read_text(encoding="utf-8") == '{"a": 1}'
    assert (snap / "subtitle_en.json").read_text(encoding="utf-8") == "[]"


def test_snapshot_before_edit_with_injected_snapshot_fn(tmp_path: Path) -> None:
    """注入 snapshot_fn → 用 stub 跳过实际复制,只创建空目录。"""

    def stub(src: Path, dst: Path) -> None:
        dst.mkdir(parents=True, exist_ok=True)

    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text("{}", encoding="utf-8")

    snap = snapshot_before_edit(draft_dir, snapshot_fn=stub, ts="t1")
    assert snap.exists()
    assert snap.is_dir()
    # stub 不复制 → 快照目录里没有文件
    assert list(snap.iterdir()) == []


def test_snapshot_before_edit_missing_dir_returns_expected_path(tmp_path: Path) -> None:
    """draft_dir 不存在 → 仍创建预期快照路径(空目录),不抛。"""
    draft_dir = tmp_path / "does_not_exist"
    snap = snapshot_before_edit(draft_dir, ts="t2")
    assert snap.exists()
    assert snap == draft_dir / ".snapshots" / "t2"


def test_restore_from_snapshot_restores_files(tmp_path: Path) -> None:
    """snapshot 包含旧 draft_content.json → restore 后 draft_dir 内容与快照一致。"""
    draft_dir = tmp_path / "draft"
    snap_dir = draft_dir / ".snapshots" / "t1"
    snap_dir.mkdir(parents=True)

    # 快照里写旧值
    (snap_dir / "draft_content.json").write_text(
        '{"old": true}', encoding="utf-8"
    )
    # 当前 draft_dir 被污染(新值)
    (draft_dir / "draft_content.json").write_text(
        '{"new": true}', encoding="utf-8"
    )

    used = restore_from_snapshot(draft_dir)
    assert used == snap_dir
    assert (
        (draft_dir / "draft_content.json").read_text(encoding="utf-8")
        == '{"old": true}'
    )


def test_restore_from_snapshot_preserves_snapshots_dir(tmp_path: Path) -> None:
    """restore 不删 .snapshots 子目录本身(避免无限删快照 → 无法再次 restore)。"""
    draft_dir = tmp_path / "draft"
    snap_dir = draft_dir / ".snapshots" / "t1"
    snap_dir.mkdir(parents=True)
    (snap_dir / "draft_content.json").write_text("{}", encoding="utf-8")
    (draft_dir / "draft_content.json").write_text('{"polluted": 1}', encoding="utf-8")

    restore_from_snapshot(draft_dir)
    # .snapshots 目录还在
    assert (draft_dir / ".snapshots").exists()
    assert snap_dir.exists()


def test_restore_from_snapshot_picks_latest_when_unspecified(tmp_path: Path) -> None:
    """不指定 snapshot_dir → 取 .snapshots/ 下按字典序最后(== 时间序最新)一个。"""
    draft_dir = tmp_path / "draft"
    for ts in ("2026-09-20T00-00-00Z", "2026-09-20T00-00-01Z", "2026-09-20T00-00-02Z"):
        (draft_dir / ".snapshots" / ts).mkdir(parents=True)
        (draft_dir / ".snapshots" / ts / "draft_content.json").write_text(
            f'{{"ts": "{ts}"}}', encoding="utf-8"
        )
    (draft_dir / "draft_content.json").write_text('{"current": true}', encoding="utf-8")

    used = restore_from_snapshot(draft_dir)
    # 最新 = 字典序最大
    assert used.name == "2026-09-20T00-00-02Z"
    assert (
        (draft_dir / "draft_content.json").read_text(encoding="utf-8")
        == '{"ts": "2026-09-20T00-00-02Z"}'
    )


def test_restore_from_snapshot_no_snapshots_raises(tmp_path: Path) -> None:
    """无快照目录 → FileNotFoundError。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        restore_from_snapshot(draft_dir)


# ---------------------------------------------------------------------------
# cleanup_old_snapshots
# ---------------------------------------------------------------------------


def test_cleanup_old_snapshots_keeps_n(tmp_path: Path) -> None:
    """保留最近 N 个;旧的全删。"""
    draft_dir = tmp_path / "draft"
    for ts in ("t1", "t2", "t3", "t4", "t5", "t6"):
        (draft_dir / ".snapshots" / ts).mkdir(parents=True)

    deleted = cleanup_old_snapshots(draft_dir, keep=3)
    assert deleted == 3
    remaining = sorted(p.name for p in (draft_dir / ".snapshots").iterdir())
    assert remaining == ["t4", "t5", "t6"]


def test_cleanup_old_snapshots_no_op_when_under_limit(tmp_path: Path) -> None:
    """总数 ≤ keep → 不删。"""
    draft_dir = tmp_path / "draft"
    for ts in ("t1", "t2"):
        (draft_dir / ".snapshots" / ts).mkdir(parents=True)

    deleted = cleanup_old_snapshots(draft_dir, keep=5)
    assert deleted == 0
    remaining = sorted(p.name for p in (draft_dir / ".snapshots").iterdir())
    assert remaining == ["t1", "t2"]


def test_cleanup_old_snapshots_no_dir_returns_zero(tmp_path: Path) -> None:
    """draft_dir 没有 .snapshots → 返回 0,不抛。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    assert cleanup_old_snapshots(draft_dir) == 0
