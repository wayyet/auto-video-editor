"""「工作流流程图前端实时化」:前端纯逻辑层的 Node 单测(pytest 包装)。

前端纯逻辑在 ``web/static/workflow_monitor.mjs`` —— §7.1 事件
归约状态机、``Last-Event-ID`` 补发去重、并行活动节点、重试 attempt 历史、
布局 key 隔离(workflow_id + mode + schema_version)、已走边投影、主图数据
归一,不碰 DOM,所以用 ``node --test`` 直接跑。

为什么要包一层 pytest
--------------------

验收命令是 ``pytest tests``,把 Node 用例纳入同一条命令,前端和后端
一次跑完 —— 少一个「忘了跑前端测试」的缺口。

Node 不在环境里时**跳过**(不判失败):前端测试只是补充证据,不是主链路。

迁移说明(单流水线架构 2026-11-04 阶段1):自 ``tests_openstoryline/`` 迁入
—— 监控前端属保留流水线;路径改为按仓库根推导(原文件以 tests_openstoryline
所在目录为基准)。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE_TEST = REPO_ROOT / "web" / "tests" / "workflow_monitor.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node,跳过前端单测")
def test_workflow_monitor_frontend_unit_tests_pass():
    """工作流监控前端纯逻辑层 Node 单测必须全绿。"""
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
        "前端单测失败:\n" + output[-4000:]
    )
    # 确认真的跑起来了(不是「0 个用例」也返回 0)。
    assert "pass 0" not in output, "前端单测一个都没跑:\n" + output[-2000:]


SSE_CONTRACT_NODE_TEST = REPO_ROOT / "web" / "tests" / "sse_frame_contract.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node,跳过前端单测")
def test_sse_frame_contract_node_tests_pass():
    """SSE 帧契约(前端一侧):真实帧经 eventFromSse 还原后必须被 applyEvent 正确归约。

    契约样本 web/tests/fixtures/sse_frames.txt 由后端
    tests/unit/monitoring/test_sse_frame_contract.py 生成并校验(同一份文件喂给
    两侧,后端改帧格式或前端改读取逻辑都会让对应一侧变红)。
    """
    assert SSE_CONTRACT_NODE_TEST.exists(), f"前端测试文件不存在: {SSE_CONTRACT_NODE_TEST}"

    proc = subprocess.run(
        ["node", "--test", str(SSE_CONTRACT_NODE_TEST)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )

    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, "SSE 帧契约前端用例失败:\n" + output[-4000:]
    assert "pass 0" not in output, "SSE 帧契约前端用例一个都没跑:\n" + output[-2000:]


LAYOUT_NODE_TEST = REPO_ROOT / "web" / "tests" / "workflow_layout.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node,跳过前端单测")
def test_workflow_layout_node_tests_pass():
    """纵向有向图布局纯模块(第5期阶段1)的 Node 单测必须全绿。

    workflow_layout.mjs 只做布局计算(反馈边识别/Kahn 分层/barycenter 层内
    排序/坐标分配/缓存 key),不碰 DOM —— node --test 直跑。
    """
    assert LAYOUT_NODE_TEST.exists(), f"前端测试文件不存在: {LAYOUT_NODE_TEST}"

    proc = subprocess.run(
        ["node", "--test", str(LAYOUT_NODE_TEST)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )

    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, "布局模块前端用例失败:\n" + output[-4000:]
    assert "pass 0" not in output, "布局模块前端用例一个都没跑:\n" + output[-2000:]
