"""节点 1:clean_cache — 对应 /kuaishou-clean-cache 技能(附件 1.2 节)。

清理剪映、OpenStoryline、FireRed-OpenStoryline、auto-video-editor 自身
的可再生缓存(技能「一类·常规再生缓存」清单)。所有路径来源:

- ``config.CACHE_PATHS_TO_CLEAN``:简单路径(直接 Path)。
- ``config.CACHE_GLOB_SPECS``:递归/通配 spec(``__pycache__``、``tmp`` 子目录、
  ``.DS_Store``、剪映草稿 ``.bak`` / ``.backup``、runtime 日志等)。
- ``config.resolved_cache_paths()``:把上述两类 spec 扁平化为当下磁盘上的
  ``list[Path]``。

调用时机(2026-09 计划):
- graph.invoke **不**自动触发本节点(START 边已剥离,见 graph.py 与
  ``tests/integration/test_no_autoclean_cache.py``)。
- 仅由 FireRed-OpenStoryline Web UI 的「清理缓存」按钮通过
  ``POST /api/system/clean-cache`` 端点显式调用。

单测要点(附件 1.2 节):
- 目标目录不存在时不报错(正常跳过)
- 目标目录存在但被占用时异常被捕获并写入 error_log,不中断流程
- cache_cleaned_paths 只包含实际清理成功的路径
- 禁止清理剪映 ``User Data\\Cache`` / ``User Data\\Log``(技能三类·禁删项)
- ``__pycache__`` 递归须排除 ``.venv/`` 与 ``venv/``
- ``tmp/`` 根下 4 个模板 JSON 不被删除(只动子目录)
"""

from __future__ import annotations

import shutil
from pathlib import Path

from config import resolved_cache_paths
from state import WorkflowState


def clean_cache(state: WorkflowState) -> dict:
    cleaned: list[str] = []
    errors = list(state.get("error_log", []) or [])
    for path in resolved_cache_paths():
        try:
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                cleaned.append(str(path))
        except Exception as e:  # noqa: BLE001 - 故意吞掉所有 IO 异常
            errors.append(f"[node_01] 清理失败 {path}: {e}")
    return {
        **state,
        "cache_cleaned": True,
        "cache_cleaned_paths": cleaned,
        "error_log": errors,
    }


def clean_cache_paths(paths: list[Path], state: WorkflowState) -> dict:
    """测试/扩展入口:允许显式传入路径列表(覆盖 config 中的全局值)。"""
    cleaned: list[str] = []
    errors = list(state.get("error_log", []) or [])
    for path in paths:
        try:
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                cleaned.append(str(path))
        except Exception as e:  # noqa: BLE001
            errors.append(f"[node_01] 清理失败 {path}: {e}")
    return {
        **state,
        "cache_cleaned": True,
        "cache_cleaned_paths": cleaned,
        "error_log": errors,
    }
