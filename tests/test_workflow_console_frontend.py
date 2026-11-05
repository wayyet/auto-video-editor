"""「工作流创建台前端」纯逻辑层的 Node 单测(pytest 包装,第二期阶段 3)。

前端纯逻辑在 ``web/static/workflow_console.mjs``:文件预校验/去重、提交参数
校验、multipart FormData 契约、提交状态机、错误码稳定提示;并与
``workflow_monitor.mjs`` 共用同一套 SSE 事件归约(applyEvent)。

运行方式与 workflow_monitor.test.mjs 相同:``node --test``,pytest 包装后
``pytest tests`` 一条命令即可把前端+后端跑完;Node 缺失时跳过(不判失败)。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE_TEST = REPO_ROOT / "web" / "tests" / "workflow_console.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node,跳过前端单测")
def test_workflow_console_frontend_unit_tests_pass():
    """工作流创建台前端纯逻辑层 Node 单测必须全绿。"""
    assert NODE_TEST.exists(), f"前端测试文件不存在: {NODE_TEST}"

    proc = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )

    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        "创建台前端单测失败:\n" + output[-4000:]
    )
    # 确认真的跑起来了(不是「0 个用例」也返回 0)。
    assert "pass 0" not in output, "创建台前端单测一个都没跑:\n" + output[-2000:]
