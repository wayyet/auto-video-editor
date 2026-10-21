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
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_gate import protocol

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent
REPO_ROOT = OPENSTORYLINE_DIR.parent
PKG_DIR = OPENSTORYLINE_DIR / "src" / "open_storyline"

#: mcode 接入涉及、且会产出 / 消费状态的源文件(全部要扫「done」字面量)。
_SUBJECT_FILES = [
    PKG_DIR / "mcode_chat_model.py",
    PKG_DIR / "mcode_media.py",
    PKG_DIR / "agent.py",
    OPENSTORYLINE_DIR / "agent_fastapi.py",
]

#: 本轮**新增 / 改动**的 mcode 接入文件 —— 额外要求:不许写死 worker 状态字面量。
_NEW_MCODE_FILES = [
    PKG_DIR / "mcode_chat_model.py",
    PKG_DIR / "mcode_media.py",
    PKG_DIR / "agent.py",
]

#: worker 状态的合法取值(来自协议)。
_WORKER_STATES = {
    "starting",
    "running",
    "waiting_human",
    "completed",
    "failed",
    "crashed",
}


# --------------------------------------------------------------------------
# 协议本身
# --------------------------------------------------------------------------

def test_status_uses_completed_constant():
    """完成状态 = ``STATE_COMPLETED``;人工关卡 = ``STATE_WAITING_HUMAN``。"""
    assert protocol.STATE_COMPLETED == "completed"
    assert protocol.STATE_WAITING_HUMAN == "waiting_human"
    assert protocol.STATE_FAILED == "failed"
    assert protocol.STATE_CRASHED == "crashed"

    # 完成态与人工关卡态必须不同 —— 否则网页端分不清「做完了」和「要人接手」
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


# --------------------------------------------------------------------------
# 源文件扫描
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", _SUBJECT_FILES, ids=lambda p: p.name)
def test_source_files_do_not_use_done_literal(path: Path):
    """mcode 相关源文件里不许把 ``done`` 当状态名写死。"""
    assert path.is_file(), f"文件不存在: {path}"
    text = path.read_text(encoding="utf-8")

    hits = re.findall(r"""["']done["']""", text)
    assert not hits, (
        f"{path.name} 里出现了字面量状态名 'done'({len(hits)} 处)。"
        "worker 状态名一律从 agent_gate.protocol 导入常量。"
    )


@pytest.mark.parametrize("path", _NEW_MCODE_FILES, ids=lambda p: p.name)
def test_new_mcode_files_do_not_hardcode_worker_status(path: Path):
    """新增的 mcode 文件里,worker 状态要么不出现,要么来自协议常量。"""
    text = path.read_text(encoding="utf-8")

    hits = []
    for state in sorted(_WORKER_STATES):
        if re.search(rf"""["']{state}["']""", text):
            hits.append(state)
    if not hits:
        pytest.skip(f"{path.name} 没写死任何 worker 状态字面量")

    imports_from_protocol = (
        "from agent_gate.protocol import" in text
        or "from agent_gate import protocol" in text
        or "import agent_gate.protocol" in text
    )
    assert imports_from_protocol, (
        f"{path.name} 写死了 worker 状态字面量 {hits},但没有从 agent_gate.protocol 导入常量。"
    )
