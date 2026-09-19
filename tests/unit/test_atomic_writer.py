"""draft_ops.atomic_writer 单测(附件 2.5 节)。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from draft_ops.atomic_writer import (
    atomic_write_draft,
    atomic_write_draft_pair,
    safe_write_draft,
)


def test_atomic_write_happy_path(tmp_path: Path) -> None:
    """正常写入后文件内容正确,无残留临时文件。"""
    draft_file = tmp_path / "draft_content.json"
    content = {"canvas_config": {"width": 1080, "height": 1920}, "tracks": []}
    atomic_write_draft(draft_file, content)

    assert draft_file.exists()
    loaded = json.loads(draft_file.read_text(encoding="utf-8"))
    assert loaded == content
    # 不应残留临时文件
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".draft_tmp_")]
    assert leftovers == []


def test_atomic_write_replace_failure_keeps_target_intact(tmp_path: Path) -> None:
    """os.replace 前抛异常 → 目标未被污染,临时文件已清理。"""
    draft_file = tmp_path / "draft_content.json"
    # 预置一个旧版本
    original = {"old": True}
    draft_file.write_text(json.dumps(original), encoding="utf-8")

    content = {"new": True}

    with patch("draft_ops.atomic_writer.os.replace", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            atomic_write_draft(draft_file, content)

    # 目标应保留旧内容
    loaded = json.loads(draft_file.read_text(encoding="utf-8"))
    assert loaded == original
    # 临时文件应已被清理
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".draft_tmp_")]
    assert leftovers == []


def test_atomic_write_rejects_non_serializable_content(tmp_path: Path) -> None:
    """content 不可 JSON 序列化 → TypeError,不写任何文件。"""
    draft_file = tmp_path / "draft_content.json"
    with pytest.raises(TypeError):
        atomic_write_draft(draft_file, {"bad": set([1, 2, 3])})
    assert not draft_file.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".draft_tmp_")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# atomic_write_draft_pair — 剪映 5.9+ 双写
# ---------------------------------------------------------------------------


def test_atomic_write_draft_pair_happy_path(tmp_path: Path) -> None:
    """双写:content 与 info 两个文件均存在,且字节相同(序列化 indent=2)。"""
    draft_dir = tmp_path / "draft"
    content = {
        "canvas_config": {"width": 1080, "height": 1920},
        "tracks": [{"id": "video-1"}],
        "duration": 35_000_000,
    }

    atomic_write_draft_pair(draft_dir, content)

    content_file = draft_dir / "draft_content.json"
    info_file = draft_dir / "draft_info.json"
    assert content_file.exists()
    assert info_file.exists()

    # 两个文件内容应完全一致(序列化字节相同)
    assert content_file.read_bytes() == info_file.read_bytes()
    # 内容可被解析回 dict,且等于传入的 content
    assert json.loads(content_file.read_text(encoding="utf-8")) == content
    assert json.loads(info_file.read_text(encoding="utf-8")) == content
    # 无残留临时文件
    leftovers = [p for p in draft_dir.iterdir() if p.name.startswith(".draft_tmp_")]
    assert leftovers == []


def test_atomic_write_draft_pair_creates_dir_if_missing(tmp_path: Path) -> None:
    """draft_dir 不存在 → 自动创建(同 atomic_write_draft 行为)。"""
    draft_dir = tmp_path / "nested" / "draft"
    assert not draft_dir.exists()

    atomic_write_draft_pair(draft_dir, {"a": 1})

    assert draft_dir.exists()
    assert (draft_dir / "draft_content.json").exists()
    assert (draft_dir / "draft_info.json").exists()


def test_atomic_write_draft_pair_uses_independent_tmpfiles(tmp_path: Path) -> None:
    """两个文件应**各自独立** mkstemp + os.replace(不共用临时文件)。"""
    draft_dir = tmp_path / "draft"
    seen_tmp_paths: list[str] = []

    real_mkstemp = __import__("tempfile").mkstemp

    def tracking_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        seen_tmp_paths.append(path)
        return fd, path

    with patch("draft_ops.atomic_writer.tempfile.mkstemp", side_effect=tracking_mkstemp):
        atomic_write_draft_pair(draft_dir, {"x": 1})

    # 双写应至少调用过 2 次 mkstemp,且临时路径互不相同
    assert len(seen_tmp_paths) >= 2
    assert len(set(seen_tmp_paths)) >= 2


def test_atomic_write_draft_pair_second_failure_keeps_first(tmp_path: Path) -> None:
    """content 写成功 + info 写失败 → content 已新值,info 保持旧值;无残留临时文件。

    ``atomic_write_draft_pair`` 只做"尽力双写";verify + restore 由上层
    ``safe_write_draft`` 负责(测安全回退见 ``test_safe_write_draft.py``)。
    """
    draft_dir = tmp_path / "draft"
    content_file = draft_dir / "draft_content.json"
    info_file = draft_dir / "draft_info.json"
    # 预置 info 文件为旧值
    info_file.parent.mkdir(parents=True, exist_ok=True)
    info_file.write_text(json.dumps({"old": "info"}), encoding="utf-8")

    # 让 info_file 的 os.replace 抛 OSError — 仅影响 info_file 的 rename
    real_replace = __import__("os").replace
    call_count = {"n": 0}

    def selective_replace(src, dst):
        call_count["n"] += 1
        if Path(dst).name == "draft_info.json":
            raise OSError("simulated info write failure")
        return real_replace(src, dst)

    with patch("draft_ops.atomic_writer.os.replace", side_effect=selective_replace):
        with pytest.raises(OSError, match="simulated info write failure"):
            atomic_write_draft_pair(draft_dir, {"new": True})

    # content_file 已写入新值(因为 content 在前)
    assert json.loads(content_file.read_text(encoding="utf-8")) == {"new": True}
    # info_file 保持旧值(因为 replace 失败)
    assert json.loads(info_file.read_text(encoding="utf-8")) == {"old": "info"}
    # 无残留临时文件
    leftovers = [p for p in draft_dir.iterdir() if p.name.startswith(".draft_tmp_")]
    assert leftovers == []


def test_atomic_write_draft_pair_rejects_non_serializable_content(tmp_path: Path) -> None:
    """content 不可 JSON 序列化 → TypeError,不创建任何文件。"""
    draft_dir = tmp_path / "draft"
    with pytest.raises(TypeError):
        atomic_write_draft_pair(draft_dir, {"bad": set([1, 2, 3])})
    assert not (draft_dir / "draft_content.json").exists()
    assert not (draft_dir / "draft_info.json").exists()


# ---------------------------------------------------------------------------
# safe_write_draft — Week 5 顶层入口
# ---------------------------------------------------------------------------


def test_safe_write_draft_happy_path(tmp_path: Path) -> None:
    """完整流程通过:proc 检测 + 快照 + 双写 + verify + 时长索引同步。"""
    draft_dir = tmp_path / "draft"
    content = {"duration": 35_000_000, "tracks": [{"id": "v1"}]}

    result = safe_write_draft(
        draft_dir,
        content,
        duration_us=35_000_000,
        proc_checker=lambda: False,
        snapshotter=lambda d: d / ".snapshots" / "t1" or (d / ".snapshots" / "t1").mkdir(parents=True) or d / ".snapshots" / "t1",
        verifier=lambda d: (True, "ok"),
        duration_updater=lambda d, du: {"draft_meta": True, "root_meta": False},
    )

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["reason"] == "ok"
    assert result["jianying_running"] is False
    # 双文件均存在且一致
    cf = draft_dir / "draft_content.json"
    inf = draft_dir / "draft_info.json"
    assert cf.exists() and inf.exists()
    assert cf.read_bytes() == inf.read_bytes()


def test_safe_write_draft_jianying_running_does_not_block(tmp_path: Path) -> None:
    """剪映在跑 → 告警标记 jianying_running=True,但流程不阻断,仍正常写入。"""
    draft_dir = tmp_path / "draft"

    result = safe_write_draft(
        draft_dir,
        {"a": 1},
        proc_checker=lambda: True,  # 模拟剪映在跑
        snapshotter=lambda d: d / ".snapshots" / "t2" or (d / ".snapshots" / "t2").mkdir(parents=True) or d / ".snapshots" / "t2",
        verifier=lambda d: (True, "ok"),
    )

    assert result["ok"] is True
    assert result["jianying_running"] is True
    assert (draft_dir / "draft_content.json").exists()


def test_safe_write_draft_verify_failure_triggers_restore_and_raises(tmp_path: Path) -> None:
    """verify 失败 → restorer 被调用(整目录回退)+ raise RuntimeError。"""
    draft_dir = tmp_path / "draft"
    # 预置旧内容(快照场景)
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / "draft_content.json").write_text(
        '{"old": true}', encoding="utf-8"
    )

    restore_calls: list[Path] = []

    def fake_restorer(d: Path) -> Path:
        restore_calls.append(d)
        # 模拟回退:把 draft_content.json 恢复成旧值
        (d / "draft_content.json").write_text('{"old": true}', encoding="utf-8")
        return d / ".snapshots" / "restored"

    def fake_snapshotter(d: Path) -> Path:
        snap = d / ".snapshots" / "snap"
        snap.mkdir(parents=True, exist_ok=True)
        return snap

    with pytest.raises(RuntimeError, match="safe_write_draft 校验失败"):
        safe_write_draft(
            draft_dir,
            {"new": True},
            proc_checker=lambda: False,
            snapshotter=fake_snapshotter,
            restorer=fake_restorer,
            verifier=lambda d: (False, "draft_content_info_mismatch"),
        )

    # restorer 被调用了一次
    assert len(restore_calls) == 1
    # draft_content.json 已被回退到旧值(因为 verify 失败 → content 写入但随后回退)
    # 实际行为:先 atomic_write_draft_pair 写入 new=True,然后 verify fail → restorer 把文件改回 old
    assert (
        json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
        == {"old": True}
    )


def test_safe_write_draft_no_duration_index_when_not_specified(tmp_path: Path) -> None:
    """未传 duration_us → 不调用 duration_updater;返回 duration_index=None。"""
    draft_dir = tmp_path / "draft"
    duration_calls: list[tuple[Path, int]] = []

    def fake_duration_updater(d: Path, du: int) -> dict[str, bool]:
        duration_calls.append((d, du))
        return {"draft_meta": True, "root_meta": True}

    result = safe_write_draft(
        draft_dir,
        {"a": 1},
        proc_checker=lambda: False,
        snapshotter=lambda d: d / ".snapshots" / "t3" or (d / ".snapshots" / "t3").mkdir(parents=True) or d / ".snapshots" / "t3",
        verifier=lambda d: (True, "ok"),
        duration_updater=fake_duration_updater,
    )

    assert result["duration_index"] is None
    assert duration_calls == []


def test_safe_write_draft_calls_duration_updater_when_specified(tmp_path: Path) -> None:
    """传 duration_us → duration_updater 被调用,返回值被透传。"""
    draft_dir = tmp_path / "draft"
    seen: list[tuple[Path, int]] = []

    def fake_duration_updater(d: Path, du: int) -> dict[str, bool]:
        seen.append((d, du))
        return {"draft_meta": True, "root_meta": True}

    result = safe_write_draft(
        draft_dir,
        {"a": 1},
        duration_us=99_000_000,
        proc_checker=lambda: False,
        snapshotter=lambda d: d / ".snapshots" / "t4" or (d / ".snapshots" / "t4").mkdir(parents=True) or d / ".snapshots" / "t4",
        verifier=lambda d: (True, "ok"),
        duration_updater=fake_duration_updater,
    )

    assert result["duration_index"] == {"draft_meta": True, "root_meta": True}
    assert seen == [(draft_dir, 99_000_000)]


def test_safe_write_draft_propagates_oserror(tmp_path: Path) -> None:
    """atomic_write_draft_pair 内部 OSError → 透传,不走 verify/restore。"""
    draft_dir = tmp_path / "draft"

    def fake_snapshotter(d: Path) -> Path:
        snap = d / ".snapshots" / "x"
        snap.mkdir(parents=True, exist_ok=True)
        return snap

    real_pair = atomic_write_draft_pair
    with patch(
        "draft_ops.atomic_writer.atomic_write_draft",
        side_effect=OSError("simulated write failure"),
    ):
        with pytest.raises(OSError, match="simulated write failure"):
            safe_write_draft(
                draft_dir,
                {"a": 1},
                proc_checker=lambda: False,
                snapshotter=fake_snapshotter,
                verifier=lambda d: (True, "ok"),  # 不应被调用
            )
    # 真实 pair 函数没被调用(被 patched 替换);这个测试只是确保 OSError 透传
    assert True  # placeholder,真正的断言是上面那个 pytest.raises
