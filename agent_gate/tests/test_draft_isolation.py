"""D11:每个 thread 的剪映草稿目录必须隔离。

真实 bug(2026-10-20):agent_gate 从不设 ``AUTO_VIDEO_EDITOR_DRAFT_DIR``,
而项目的 ``config.resolve_draft_dir()`` 在没有这个环境变量、state 里也没有
``_draft_dir_override`` 时会回落到 ``<cwd>/drafts/default`` ——
**所有 thread 共用同一个目录**。

后果:
1. 两个并发运行互相覆盖草稿(剪映里看到的是谁的草稿全看运气);
2. 草稿落在 ``outputs/`` 之外,项目自己的 ``outputs/{job_id}/`` 隔离失效;
3. 2026-10-19 的 ``v006`` 之所以拿到正确的 ``outputs/v006/drafts/``,
   是因为操作员**手工**设了环境变量 —— 文档里没写,换个终端就没了。

修法:worker 起跑前把 ``AUTO_VIDEO_EDITOR_DRAFT_DIR`` 指到
``<outputs_root>/<thread_id>/drafts``;操作员显式设了就不覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_gate import runner
from agent_gate.safety import UnsafeIdentifier, UnsafePath, safe_draft_dir

GRAPH = "toy_graph:build_graph"


# ------------------------------------------------------------------ safe_draft_dir
def test_draft_dir_is_per_thread(tmp_path):
    """两个 thread 拿到两个不同的草稿目录,都在 outputs 之内。"""
    a = safe_draft_dir(tmp_path, "job1")
    b = safe_draft_dir(tmp_path, "job2")
    assert a == (tmp_path.resolve() / "job1" / "drafts")
    assert b == (tmp_path.resolve() / "job2" / "drafts")
    assert a != b
    assert a.resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize("bad", ["../x", "..\\x", "/tmp/x", "C:\\Windows\\x", "",
                                 "a" * 65, "has space", "中文id", "a/b"])
def test_draft_dir_rejects_bad_thread_id(tmp_path, bad):
    with pytest.raises((UnsafeIdentifier, UnsafePath)):
        safe_draft_dir(tmp_path, bad)


# ------------------------------------------------------------------ worker 落位
def test_isolate_draft_dir_sets_and_reports_ownership(tmp_path, monkeypatch):
    """_isolate_draft_dir:没设过时设成本 thread 的目录,并报告「是我们设的」。"""
    monkeypatch.delenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", raising=False)
    path, prev, set_by_us = runner._isolate_draft_dir(tmp_path, "job1")
    try:
        assert path == (tmp_path.resolve() / "job1" / "drafts")
        assert prev is None
        assert set_by_us is True
        assert os.environ["AUTO_VIDEO_EDITOR_DRAFT_DIR"] == str(path)
    finally:
        os.environ.pop("AUTO_VIDEO_EDITOR_DRAFT_DIR", None)


def test_isolate_draft_dir_respects_operator_override(tmp_path, monkeypatch):
    """操作员显式设了就不覆盖 —— agent_gate 不越权改人的显式意图。"""
    explicit = str(tmp_path / "my-own-drafts")
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", explicit)
    path, prev, set_by_us = runner._isolate_draft_dir(tmp_path, "job1")
    assert path == Path(explicit)
    assert prev == explicit
    assert set_by_us is False
    assert os.environ["AUTO_VIDEO_EDITOR_DRAFT_DIR"] == explicit


def test_two_threads_get_two_draft_dirs(tmp_path, monkeypatch):
    """连着跑两个 thread,各自的草稿目录互不干扰。"""
    monkeypatch.delenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", raising=False)
    monkeypatch.setenv("AGENT_GATE_CKPT_DIR", str(tmp_path / "ckpt"))

    p1, _, _ = runner._isolate_draft_dir(tmp_path, "job1")
    os.environ.pop("AUTO_VIDEO_EDITOR_DRAFT_DIR", None)
    p2, _, _ = runner._isolate_draft_dir(tmp_path, "job2")

    assert p1 != p2
    assert p1.name == "drafts" and p2.name == "drafts"
    assert p1.parent.name == "job1" and p2.parent.name == "job2"
    os.environ.pop("AUTO_VIDEO_EDITOR_DRAFT_DIR", None)


def test_draft_dir_lands_inside_outputs_root(tmp_path):
    """草稿目录必须在 outputs 之内 —— 不能跑到 outputs 外面去。"""
    d = runner.draft_dir(tmp_path, "job1")
    assert d.resolve().is_relative_to(tmp_path.resolve())
    assert d.parent.name == "job1"
    assert d.name == "drafts"


def test_worker_does_not_leak_draft_env_into_caller(tmp_path, monkeypatch):
    """worker 跑完必须把环境变量还原 —— 否则同进程后续用例会被污染。"""
    monkeypatch.delenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", raising=False)
    monkeypatch.setenv("AGENT_GATE_CKPT_DIR", str(tmp_path / "ckpt"))
    runner.run_worker(thread_id="job1", outputs_root=tmp_path, graph_factory=GRAPH,
                      initial_state={"session_id": "job1"}, backend="stub")
    assert "AUTO_VIDEO_EDITOR_DRAFT_DIR" not in os.environ


def test_worker_keeps_operator_draft_env(tmp_path, monkeypatch):
    """操作员设的草稿目录,worker 跑完必须原样保留。"""
    explicit = str(tmp_path / "my-own-drafts")
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", explicit)
    monkeypatch.setenv("AGENT_GATE_CKPT_DIR", str(tmp_path / "ckpt"))
    runner.run_worker(thread_id="job1", outputs_root=tmp_path, graph_factory=GRAPH,
                      initial_state={"session_id": "job1"}, backend="stub")
    assert os.environ["AUTO_VIDEO_EDITOR_DRAFT_DIR"] == explicit
