"""F3:运行生命周期状态机。

``spawn_worker`` 的 ``mode`` 必须必填且语义互斥:

============  ==========================================  ==========================
mode          允许的前置条件                                违反时的 code
============  ==========================================  ==========================
start         status.json 不存在 **且** 旁边没有检查点        already_used
continue_after_crash   状态 ∈ {crashed, failed} 且有检查点   not_restartable / no_checkpoint
resume        状态 == waiting_human 且 checkpoint 匹配      not_waiting_human / bad_checkpoint
============  ==========================================  ==========================

外加保留的第 4 条独立检查:starting / running 时一律 already_running。
"""
from __future__ import annotations

import pytest

from agent_gate import runner, service
from agent_gate.mailbox import Mailbox
from agent_gate.protocol import (
    STATE_COMPLETED,
    STATE_CRASHED,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_STARTING,
    STATE_WAITING_HUMAN,
    utc_now,
)

GRAPH = "toy_graph:build_graph"


def _status(tmp_path, thread="job1", state=STATE_RUNNING, *, interrupts=None, pid=None):
    mb = Mailbox(runner.gate_root(tmp_path, thread))
    mb.write_status({"thread_id": thread, "state": state, "pid": pid,
                     "started_at": utc_now(), "heartbeat_at": utc_now(),
                     "interrupts": interrupts or [], "error": None})
    return mb


def _cur(tmp_path, thread="job1"):
    mb = Mailbox(runner.gate_root(tmp_path, thread))
    return service.status_summary(mb, thread_id=thread)


# ------------------------------------------------------------------ check_mode 纯函数
def test_start_ok_on_clean_thread(tmp_path):
    assert runner.check_mode(mode="start", cur=_cur(tmp_path), thread_id="job1",
                             has_checkpoint=False) is None


def test_start_rejected_when_status_exists(tmp_path):
    _status(tmp_path, state=STATE_COMPLETED)
    bad = runner.check_mode(mode="start", cur=_cur(tmp_path), thread_id="job1",
                            has_checkpoint=False)
    assert bad["code"] == "already_used"


def test_start_rejected_when_checkpoint_exists(tmp_path):
    """status 被清掉了,但检查点还在 —— 仍然不许当新运行喂初始 state。"""
    bad = runner.check_mode(mode="start", cur=_cur(tmp_path), thread_id="job1",
                            has_checkpoint=True)
    assert bad["code"] == "already_used"


def test_start_rejected_when_running(tmp_path):
    _status(tmp_path, state=STATE_RUNNING)
    bad = runner.check_mode(mode="start", cur=_cur(tmp_path), thread_id="job1",
                            has_checkpoint=False)
    assert bad["code"] == "already_running"


def test_start_rejected_when_starting(tmp_path):
    _status(tmp_path, state=STATE_STARTING)
    bad = runner.check_mode(mode="start", cur=_cur(tmp_path), thread_id="job1",
                            has_checkpoint=False)
    assert bad["code"] == "already_running"


def test_continue_ok_when_crashed(tmp_path):
    _status(tmp_path, state=STATE_CRASHED)
    assert runner.check_mode(mode="continue_after_crash", cur=_cur(tmp_path),
                             thread_id="job1", has_checkpoint=True) is None


def test_continue_ok_when_failed(tmp_path):
    _status(tmp_path, state=STATE_FAILED)
    assert runner.check_mode(mode="continue_after_crash", cur=_cur(tmp_path),
                             thread_id="job1", has_checkpoint=True) is None


def test_continue_rejected_when_not_crashed(tmp_path):
    _status(tmp_path, state=STATE_RUNNING)
    bad = runner.check_mode(mode="continue_after_crash", cur=_cur(tmp_path),
                            thread_id="job1", has_checkpoint=True)
    assert bad["code"] == "already_running"        # running 先被第 4 条独立检查拦下


def test_continue_rejected_when_completed(tmp_path):
    _status(tmp_path, state=STATE_COMPLETED)
    bad = runner.check_mode(mode="continue_after_crash", cur=_cur(tmp_path),
                            thread_id="job1", has_checkpoint=True)
    assert bad["code"] == "not_restartable"


def test_continue_rejected_without_checkpoint(tmp_path):
    _status(tmp_path, state=STATE_CRASHED)
    bad = runner.check_mode(mode="continue_after_crash", cur=_cur(tmp_path),
                            thread_id="job1", has_checkpoint=False)
    assert bad["code"] == "no_checkpoint"


def test_resume_ok_when_waiting_human(tmp_path):
    _status(tmp_path, state=STATE_WAITING_HUMAN, interrupts=[{"checkpoint": "①"}])
    assert runner.check_mode(mode="resume", cur=_cur(tmp_path), thread_id="job1",
                             resume_values={"①": True}) is None


def test_resume_rejected_when_not_waiting_human(tmp_path):
    _status(tmp_path, state=STATE_RUNNING)
    bad = runner.check_mode(mode="resume", cur=_cur(tmp_path), thread_id="job1",
                            resume_values={"①": True})
    assert bad["code"] == "already_running"

    _status(tmp_path, state=STATE_FAILED)
    assert runner.check_mode(mode="resume", cur=_cur(tmp_path), thread_id="job1",
                             resume_values={"①": True})["code"] == "not_waiting_human"


def test_resume_rejected_when_checkpoint_unknown(tmp_path):
    _status(tmp_path, state=STATE_WAITING_HUMAN, interrupts=[{"checkpoint": "①"}])
    bad = runner.check_mode(mode="resume", cur=_cur(tmp_path), thread_id="job1",
                            resume_values={"②": True})
    assert bad["code"] == "bad_checkpoint"


def test_unknown_mode_rejected(tmp_path):
    bad = runner.check_mode(mode="whatever", cur=_cur(tmp_path), thread_id="job1")
    assert bad["code"] == "bad_mode"


# ------------------------------------------------------------------ spawn_worker 层
def test_spawn_start_twice_is_rejected(tmp_path, monkeypatch):
    """已 completed 的 thread_id 不能重跑 —— 重跑必须换新 ID。"""
    monkeypatch.setenv("AGENT_GATE_CKPT_DIR", str(tmp_path / "ckpt"))
    _status(tmp_path, state=STATE_COMPLETED)
    res = runner.spawn_worker(thread_id="job1", outputs_root=tmp_path, graph_factory=GRAPH,
                              mode="start", initial_state_file=tmp_path / "x.json")
    assert res["ok"] is False and res["code"] == "already_used"


def test_spawn_start_needs_input(tmp_path):
    res = runner.spawn_worker(thread_id="job1", outputs_root=tmp_path, graph_factory=GRAPH,
                              mode="start")
    assert res["ok"] is False and res["code"] == "need_input"


def test_spawn_resume_needs_values(tmp_path):
    _status(tmp_path, state=STATE_WAITING_HUMAN, interrupts=[{"checkpoint": "①"}])
    res = runner.spawn_worker(thread_id="job1", outputs_root=tmp_path, graph_factory=GRAPH,
                              mode="resume")
    assert res["ok"] is False and res["code"] == "need_input"


def test_spawn_continue_cannot_carry_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_GATE_CKPT_DIR", str(tmp_path / "ckpt"))
    _status(tmp_path, state=STATE_CRASHED)
    res = runner.spawn_worker(thread_id="job1", outputs_root=tmp_path, graph_factory=GRAPH,
                              mode="continue_after_crash",
                              initial_state_file=tmp_path / "x.json")
    assert res["ok"] is False and res["code"] == "bad_mode"


def test_spawn_continue_without_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_GATE_CKPT_DIR", str(tmp_path / "ckpt"))
    _status(tmp_path, state=STATE_CRASHED)
    res = runner.spawn_worker(thread_id="job1", outputs_root=tmp_path, graph_factory=GRAPH,
                              mode="continue_after_crash")
    assert res["ok"] is False and res["code"] == "no_checkpoint"


def test_checkpoint_path_follows_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_GATE_CKPT_DIR", str(tmp_path / "ckpt"))
    assert runner.checkpoint_path("job1") == tmp_path / "ckpt" / "job1.sqlite"
    with pytest.raises(Exception):
        runner.checkpoint_path("../evil")


def test_cli_resume_requires_human_terminal(tmp_path, monkeypatch):
    """resume 在非交互终端下仍然拒绝 —— 状态机之外的第 5 道锁。"""
    from agent_gate import cli

    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    code = cli.main(["resume", "--thread-id", "job1", "--outputs-root", str(tmp_path),
                     "--checkpoint", "①"])
    assert code == 1
