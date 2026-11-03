"""worker 状态名必须来自 ``agent_gate.protocol``,不许用 ``done``。

背景
----
agent_gate 的 **worker 状态** 是协议,不是随便起的字符串:
``starting`` / ``running`` / ``waiting_human`` / ``completed`` / ``failed`` / ``crashed``。
**没有** ``done`` —— 用错了就等于跟 worker 对不上话。

注意别混淆两个命名空间
----------------------
``agent_gate/drive.py`` 里确实有 ``"done"``,但那是 **outcome / next_action**
(任务层面的「做完了」),**不是** worker 状态。两者**不能**混用:
任务 done 之后 worker 状态是 ``completed``。

迁移说明(单流水线架构 2026-11-04 阶段2):自
``tests_openstoryline/test_mcode_status_constants.py`` 迁入 —— 本组测试
只依赖保留模块 ``agent_gate.protocol`` / ``drive.py``;原文件中扫描旧
mcode 源文件的 6 个用例随 ``src/open_storyline/`` 删除(见 git 历史)。
"""

from __future__ import annotations

from pathlib import Path

from agent_gate import protocol

REPO_ROOT = Path(__file__).resolve().parents[2]

#: worker 状态的合法取值(来自协议)。
_WORKER_STATES = {
    "starting",
    "running",
    "waiting_human",
    "completed",
    "failed",
    "crashed",
}


def test_status_uses_completed_constant():
    """完成状态 = ``STATE_COMPLETED``;人工关卡 = ``STATE_WAITING_HUMAN``。"""
    assert protocol.STATE_COMPLETED == "completed"
    assert protocol.STATE_WAITING_HUMAN == "waiting_human"
    assert protocol.STATE_FAILED == "failed"
    assert protocol.STATE_CRASHED == "crashed"

    # 完成态与人工关卡态必须不同 —— 否则监控端分不清「做完了」和「要人接手」
    assert protocol.STATE_COMPLETED != protocol.STATE_WAITING_HUMAN


def test_protocol_has_no_done_state():
    """协议里**没有** ``done``。"""
    state_values = {
        value
        for name, value in vars(protocol).items()
        if name.startswith("STATE_") and isinstance(value, str)
    }
    assert "done" not in state_values
    assert not any(name == "STATE_DONE" for name in vars(protocol))
    assert "done" not in protocol.TERMINAL_STATES
    assert state_values <= _WORKER_STATES, f"协议里出现了预期外的状态: {state_values}"


def test_done_outcome_token_is_not_a_worker_state():
    """``drive.py`` 的 outcome token ``"done"`` **不是** worker 状态。

    两者是不同命名空间:任务 done 之后 worker 状态是 ``completed``。
    这条断言把两者的边界钉住,防止将来被「统一」成一个值。
    """
    drive_path = REPO_ROOT / "agent_gate" / "drive.py"
    text = drive_path.read_text(encoding="utf-8")
    assert '"done"' in text, "drive.py 的 outcome token 变了,请同步更新本测试"

    # outcome token 绝不能出现在 worker 状态集合里
    assert "done" not in protocol.TERMINAL_STATES
    assert protocol.STATE_COMPLETED == "completed"
