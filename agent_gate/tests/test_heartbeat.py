"""worker 心跳过期判定:``starting`` 与 ``running`` 必须用不同的阈值。

回归的真实 bug(2026-10-19):Windows 上 drive 刚拉起 worker 就把
``state="starting"`` 判成 ``crashed`` —— 因为 ``spawn_worker`` 先写一份
starting 的 status,而真正的 worker 进程还要好几秒才能 import 完 graph
并写出第一次心跳。判定用同一个 ``STALE_AFTER_S``(测试里调到 1.5s)时,
**正在正常启动**的 worker 被误判成崩溃 → drive 误报退出码 5。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_gate import service
from agent_gate.mailbox import Mailbox
from agent_gate.protocol import (
    STALE_AFTER_S,
    STARTING_STALE_AFTER_S,
    STATE_RUNNING,
    STATE_STARTING,
)


def _mailbox(tmp_path: Path) -> Mailbox:
    root = tmp_path / "gate"
    root.mkdir(parents=True, exist_ok=True)
    return Mailbox(root)


def _write(mb: Mailbox, state: str, heartbeat_age_s: float) -> None:
    """写一份心跳年龄为 ``heartbeat_age_s`` 的 status。

    时间戳必须用**微秒精度**(= :func:`agent_gate.protocol.utc_now` 的格式):
    早先这里用 ``time.strftime("%Y-%m-%dT%H:%M:%S")``,只有秒级精度,
    解析时会丢掉不足一秒的部分 —— 标称 0.5s 的心跳实际可能已经 1.4s 老,
    再叠加上跑用例的耗时,就会越过 1.5s 的阈值被误判成 crashed
    (2026-10-20 实测:机器负载一高就随机挂)。
    """
    ts = (datetime.now(timezone.utc) - timedelta(seconds=heartbeat_age_s)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    mb.write_status({
        "thread_id": "t1",
        "state": state,
        "pid": 1,
        "started_at": ts,
        "heartbeat_at": ts,
        "interrupts": [],
        "error": None,
    })


def test_starting_worker_is_not_judged_crashed_while_booting(tmp_path: Path) -> None:
    """starting 状态下,即使心跳有点旧也不能判成 crashed。"""
    mb = _mailbox(tmp_path)
    age = min(STALE_AFTER_S + 1.0, STARTING_STALE_AFTER_S - 1.0)
    _write(mb, STATE_STARTING, age)
    st = service.status_summary(mb, thread_id="t1")
    assert st["worker_state"] == STATE_STARTING
    assert st["next_action"] in {"wait", "answer_tasks"}


def test_starting_worker_still_crashes_after_grace_period(tmp_path: Path) -> None:
    """宽限期过了还没心跳 → 仍然是 crashed(不能无限等)。"""
    mb = _mailbox(tmp_path)
    _write(mb, STATE_STARTING, STARTING_STALE_AFTER_S + 1.0)
    st = service.status_summary(mb, thread_id="t1")
    assert st["worker_state"] == "crashed"
    assert st["next_action"] == "restart_worker"


def test_running_worker_uses_the_tighter_threshold(tmp_path: Path) -> None:
    """running 仍用 STALE_AFTER_S —— 别把崩溃检测一起放宽了。"""
    mb = _mailbox(tmp_path)
    _write(mb, STATE_RUNNING, STALE_AFTER_S + 1.0)
    st = service.status_summary(mb, thread_id="t1")
    assert st["worker_state"] == "crashed"

    mb2 = _mailbox(tmp_path / "b")
    # 留出足够的余量:阈值 1.5s,这里用 0.1s 的心跳 —— 机器负载高时
    # 「写完 status → 读 status」本身就要几十毫秒,不能贴着阈值写。
    _write(mb2, STATE_RUNNING, min(STALE_AFTER_S - 1.0, 0.1))
    st2 = service.status_summary(mb2, thread_id="t1")
    assert st2["worker_state"] == STATE_RUNNING


def test_starting_grace_is_wider_than_running_threshold() -> None:
    """starting 的宽限期必须严格大于 running 的过期阈值。"""
    assert STARTING_STALE_AFTER_S > STALE_AFTER_S
