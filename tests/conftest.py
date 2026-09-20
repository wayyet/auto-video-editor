"""共享 pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让 `import config` 等能解析到工作目录
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def workspace_root() -> Path:
    return ROOT


@pytest.fixture
def tmp_draft_dir(tmp_path: Path) -> Path:
    """为节点 5 与加密检测/原子写入测试准备的临时剪映草稿目录。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    return draft_dir


@pytest.fixture
def initial_state() -> dict:
    """构造一个最小可用的初始 WorkflowState。"""
    return {
        "session_id": "test-session",
        "video_input_path": str(ROOT / "tests" / "fixtures" / "30s.mp4"),
        "error_log": [],
    }


class _StubProc:
    """subprocess.Popen 的最小桩,模拟启动成功并返回固定 pid。"""

    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid
        self.stdout = None
        self.stderr = None


@pytest.fixture
def stub_proc():
    return _StubProc()


# ---------------------------------------------------------------------------
# 2026-09 迁移后:让 Week 3/4/5 既有集成测试不感知关卡⓪(直接旁路 interrupt,
# 调用 _post_resume)。test_interrupt_resume.py 显式依赖关卡⓪ 行为,不旁路。
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _skip_checkpoint0_for_pre_migration_tests(request, monkeypatch):
    """对 Week 3/4/5 既有集成测试 autouse 跳过 checkpoint0_storyline_plan。

    判定:测试文件路径包含 ``test_interrupt_resume`` 时不旁路(那个文件
    显式覆盖关卡⓪/①/②/③ 行为)。

    注意:graph.py 用 ``from X import Y`` 绑定的是 graph 模块本地的 Y,
    不是 ``nodes.X.Y``。所以要同时 monkeypatch graph 模块的本地引用。
    """
    fspath = str(getattr(request, "fspath", "") or "")
    if "test_interrupt_resume" in fspath:
        return  # 显式测关卡行为,不旁路
    try:
        import nodes.node_checkpoint0_storyline_plan as cp0
        import graph as _graph
    except ImportError:
        return
    if not hasattr(cp0, "checkpoint0_wait_storyline_plan"):
        return
    passthrough = lambda state: cp0._post_resume(state)
    # 同时 patch 两处(graph.py 通过 ``from X import Y`` 拿到了 graph 内的本地引用)
    monkeypatch.setattr(cp0, "checkpoint0_wait_storyline_plan", passthrough)
    if hasattr(_graph, "checkpoint0_wait_storyline_plan"):
        monkeypatch.setattr(_graph, "checkpoint0_wait_storyline_plan", passthrough)
