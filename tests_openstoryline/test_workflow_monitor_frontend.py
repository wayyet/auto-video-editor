"""「工作流流程图前端实时化」:前端纯逻辑层的 Node 单测(pytest 包装)。

前端纯逻辑在 ``web/static/workflow_monitor.mjs`` —— §7.1 事件
归约状态机、``Last-Event-ID`` 补发去重、并行活动节点、重试 attempt 历史、
布局 key 隔离(workflow_id + mode + schema_version)、已走边投影、主图数据
归一,不碰 DOM,所以用 ``node --test`` 直接跑。

为什么要包一层 pytest
--------------------

与 ``test_media_upload_frontend.py`` 同一套路:验收命令是
``pytest tests_openstoryline``,把 Node 用例纳入同一条命令,前端和后端
一次跑完 —— 少一个「忘了跑前端测试」的缺口。

Node 不在环境里时**跳过**(不判失败):前端测试只是补充证据,不是主链路。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent
NODE_TEST = OPENSTORYLINE_DIR / "web" / "tests" / "workflow_monitor.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node,跳过前端单测")
def test_workflow_monitor_frontend_unit_tests_pass():
    """工作流监控前端纯逻辑层 Node 单测必须全绿。"""
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
