"""「对话框显示 LangGraph 当前运行节点」阶段 4:前端模块的 Node 单测(pytest 包装)。

前端逻辑在 ``web/static/workflow_chat_indicator.mjs`` —— 会话关联查询状态机、
node_id -> 中文名映射(Schema key 隔离)、§6.5 显示规则(单节点/并行/重试/等待/
终态/暂不可用)、SSE 事件接入 workflow_monitor 归约、Last-Event-ID 去重、
EventSource 生命周期(会话切换/thread 替换)。纯逻辑 + 假 DOM/fake
EventSource,不碰真浏览器,用 ``node --test`` 直接跑。

为什么要包一层 pytest
--------------------

与 ``test_workflow_monitor_frontend.py`` 同一套路:验收命令是
``pytest tests_openstoryline``,把 Node 用例纳入同一条命令。

Node 不在环境里时**跳过**(不判失败)。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent
NODE_TEST = OPENSTORYLINE_DIR / "web" / "tests" / "workflow_chat_indicator.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node,跳过前端单测")
def test_workflow_chat_indicator_frontend_unit_tests_pass():
    """对话页节点指示模块 Node 单测必须全绿。"""
    assert NODE_TEST.exists(), f"前端测试文件不存在: {NODE_TEST}"

    proc = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        cwd=str(OPENSTORYLINE_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )

    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        "前端单测失败:\n" + output[-4000:]
    )
    # 确认真的跑起来了(不是「0 个用例」也返回 0)。
    assert "pass 0" not in output, "前端单测一个都没跑:\n" + output[-2000:]
