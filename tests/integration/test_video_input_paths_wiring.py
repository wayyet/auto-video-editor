"""集成:多视频源路径的**接线**是否贯通(2026-10 多选导入;阶段3 auto-only 版)。

链路(阶段3 后,旧 ``node_04a`` 网页握手已删除):
入口层写 ``state["video_input_paths"]`` → 下游
``nodes/storyline/_common.py::resolve_video_input_paths`` 拿到 N 个源视频。

入口层写入者:
- 直接 CLI:``scripts/run_workflow.py`` 的 ``_default_initial_state``
  (``--video-input-paths`` 逗号分隔;``video_input_path`` 仍取第一个,保持兼容);
- Agent 路径:``agent_gate`` run_start 载荷(见 ``agent_gate/cli.py``)。

同时守住**单视频零行为变更**:只给 1 个时,``video_input_paths`` 长度 1,
且与旧行为(``video_input_path`` 单值)等价。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _ROOT / "scripts" / "run_workflow.py"


def _load_runner():
    """按路径加载 ``scripts/run_workflow.py``(scripts/ 不是包)。"""
    spec = importlib.util.spec_from_file_location("run_workflow_script", _RUNNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_multi_paths_wire_video_input_paths() -> None:
    """``--video-input-paths`` 传 3 个路径 → state 列表长 3,下游读到 3 个。"""
    from nodes.storyline._common import resolve_video_input_paths

    runner = _load_runner()
    state = runner._default_initial_state("job-wire", "E:/media/a.mov,E:/media/b.mov,E:/media/c.mov")

    assert state["video_input_paths"] == [
        "E:/media/a.mov", "E:/media/b.mov", "E:/media/c.mov"
    ]
    assert resolve_video_input_paths(state) == [
        "E:/media/a.mov", "E:/media/b.mov", "E:/media/c.mov"
    ]
    # 单值字段仍指向第一个(下游单值消费方不受影响)
    assert state["video_input_path"] == "E:/media/a.mov"


def test_cli_single_path_keeps_legacy_behavior() -> None:
    """只给 1 个路径 → video_input_paths 长度 1,且值 == video_input_path。"""
    from nodes.storyline._common import resolve_video_input_paths

    runner = _load_runner()
    state = runner._default_initial_state("job-one", "E:/media/a.mp4")

    assert state["video_input_paths"] == ["E:/media/a.mp4"]
    assert state["video_input_path"] == "E:/media/a.mp4"
    assert resolve_video_input_paths(state) == ["E:/media/a.mp4"]


def test_cli_no_extra_paths_falls_back_to_default_media() -> None:
    """不传 ``--video-input-paths`` → 不写 video_input_paths 键,单值回退仍可用。"""
    from nodes.storyline._common import resolve_video_input_paths

    runner = _load_runner()
    state = runner._default_initial_state("job-default")

    assert "video_input_paths" not in state
    # 默认素材取仓库 fixture(或 inputs/),至少解析不出错
    paths = resolve_video_input_paths(state)
    assert paths == ([state["video_input_path"]] if state["video_input_path"] else [])
