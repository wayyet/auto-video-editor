"""可再生缓存清理执行器(创建台「清除缓存」按钮的后端实现)。

职责边界:
- 只做"遍历 `config.resolved_cache_paths()` 并逐项删除"这一件事;
  清理范围(含禁删约束注释)全部在 ``config.py`` 的缓存清单段维护;
- **不接图**(2026-11-04 R-10 决策:clean_cache 节点已移除,graph.invoke 永不
  自动清理);本模块仅供 ``web/api_console.py`` 的
  ``POST /api/console/cache/clear`` 手动触发,对应旧版
  ``nodes/node_01_clean_cache.py`` 的 Web 入口语义(去掉 WorkflowState 依赖);
- 破坏性 IO 的防线在调用方:同源校验 + CSRF + 冷却限流 + 活跃任务 409(端点),
  前端 confirm 弹窗(页面);单个路径删除失败不中断,异常吞掉进 ``errors``。
"""

from __future__ import annotations

import shutil

import config


def clean_cache() -> dict:
    """清空 ``config.resolved_cache_paths()`` 列出的可再生缓存。

    - 目录 → ``shutil.rmtree``,文件 → ``unlink``;
    - 调用瞬间已不存在的路径直接跳过(清单在调用时才展开,竞态窗口极小);
    - **所有 IO 异常吞掉**写入 ``errors`` 不中断(被占用的日志/.pyc 是常态,
      部分失败不应让其余缓存滞留);
    - 只回实际删除成功的路径(``cleaned_paths``),供端点统计条数。

    Returns:
        ``{"cleaned_paths": list[str], "errors": list[str]}``
    """
    cleaned: list[str] = []
    errors: list[str] = []
    for path in config.resolved_cache_paths():
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            else:
                continue  # 展开到删除之间被并发清掉:视为成功无事可做
            cleaned.append(str(path))
        except Exception as e:  # noqa: BLE001 - 故意吞掉所有 IO 异常
            errors.append(f"清理失败 {path}: {e}")
    return {"cleaned_paths": cleaned, "errors": errors}
