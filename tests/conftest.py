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
