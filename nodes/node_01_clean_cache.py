"""节点 1:clean_cache — 对应 /kuaishou-clean-cache 技能(附件 1.2 节)。

清理剪映、OpenStoryline 与系统临时目录。配置路径全部从 config 读取,
阶段 A 开工核实后填实。

单测要点(附件 1.2 节):
- 目标目录不存在时不报错(正常跳过)
- 目标目录存在但被占用时异常被捕获并写入 error_log,不中断流程
- cache_cleaned_paths 只包含实际清理成功的路径
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
