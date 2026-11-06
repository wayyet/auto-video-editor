"""共享 pytest fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 让 `import config` 等能解析到工作目录
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 2026-11-06 阶段 1(人工关卡与剪映草稿导入):默认关闭"发布注册到剪映草稿库"。
# 测试进程若带着 session_id 跑图(node_05 发布 / fork 注册),会把测试草稿写进
# **本机真实剪映草稿库**(%LOCALAPPDATA%);发布注册属生产路径,测试一律显式
# 开启(monkeypatch config 或设 JY_DRAFT_PUBLISH_ENABLED=1)。
os.environ.setdefault("JY_DRAFT_PUBLISH_ENABLED", "0")


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
