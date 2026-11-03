"""监控 API 只读化守卫(单流水线架构 2026-11-04 阶段1)。

评审修订版计划 §4.4 / R-04 / R-05 / R-22:

- R-05:``repo_root()`` 必须解析到仓库根(旧实现 ``parents[2]`` 指到仓库
  **父**目录 —— 2026-11-01 扁平化前的路径遗留);
- R-04:监控 router 只读 —— 无 ``POST /runs``、无 chat-sessions 查询、
  无任何 POST/写端点;保留 GET /graph、/runs/{tid}、/runs/{tid}/events、
  /runs/{tid}/resume-status;
- ``monitor_fastapi.app`` 独立可导入,且挂载监控页与静态资源。
"""

from __future__ import annotations

from pathlib import Path

from web import api_workflow


def test_repo_root_resolves_to_repo_root():
    """R-05:仓库根 = tests/ 的父目录;不得再指到仓库父目录。"""
    expected = Path(__file__).resolve().parents[2]
    assert api_workflow.repo_root() == expected
    assert api_workflow._REPO_ROOT == expected


def _method_path_pairs(router) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for route in router.routes:
        for method in getattr(route, "methods", ()) or ():
            pairs.append((method, route.path))
    return pairs


def test_monitor_router_is_read_only():
    """R-04:监控只读 —— 无启动端点、无 chat-session 查询、无 POST。"""
    routes = set(_method_path_pairs(api_workflow.router))

    assert ("POST", "/api/workflow/runs") not in routes, "监控不得承担工作流启动"
    assert not any("chat-sessions" in path for _, path in routes), "聊天会话关联查询已下线"
    assert not any(method != "GET" for method, _ in routes), "监控 router 只允许 GET"

    assert ("GET", "/api/workflow/graph") in routes
    assert ("GET", "/api/workflow/runs/{thread_id}") in routes
    assert ("GET", "/api/workflow/runs/{thread_id}/events") in routes
    assert ("GET", "/api/workflow/runs/{thread_id}/resume-status") in routes


def test_monitor_app_is_importable_and_mounts_pages():
    """独立监控 app:根页 / 节点图路由与 node_map、static 静态挂载齐备。

    注:FastAPI 0.162 ``include_router`` 为惰性挂载(import 时 app.routes
    只有 `_IncludedRouter` 占位),router 端点的只读守卫由
    :func:`test_monitor_router_is_read_only` 在 router 上直接断言。
    """
    import monitor_fastapi

    paths = {route.path for route in monitor_fastapi.app.routes if hasattr(route, "path")}
    assert "/" in paths
    assert "/node-map" in paths
    assert "/node_map" in paths
    assert "/static" in paths
