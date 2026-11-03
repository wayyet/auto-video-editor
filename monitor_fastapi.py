"""独立工作流监控服务(单流水线架构 2026-11-04,阶段1)。

职责边界(评审修订版计划 §4.4)
------------------------------
- **只读**:查询运行快照、SSE 事件流、图拓扑、node-map;不启动、不恢复、
  不修改任何运行状态。工作流触发只有两条路径:直接 CLI
  (``scripts/run_workflow.py``)与 ``agent_gate``(MCP / drive)。
- 依赖:主 ``.venv``(fastapi / uvicorn,已在 requirements.txt + pyproject.toml 声明)。

启动
----
    .venv/Scripts/python.exe -m uvicorn monitor_fastapi:app --host 127.0.0.1 --port 7860

路由
----
- ``GET /``                 监控首页(web/monitor.html;SSE 事件流 + 节点状态)
- ``GET /node-map``         工作流节点图页(web/node_map/node_map.html)
- ``/node_map``             node_map 静态资源(含 workflow.json 子流程视图)
- ``/static``               web/static(workflow_monitor.mjs 等,页面共用)
- ``/api/workflow/*``       只读监控 API(web/api_workflow.py router)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from web.api_workflow import _REPO_ROOT, router

_WEB_DIR = _REPO_ROOT / "web"
_MONITOR_HTML = _WEB_DIR / "monitor.html"
_NODE_MAP_DIR = _WEB_DIR / "node_map"
_NODE_MAP_HTML = _NODE_MAP_DIR / "node_map.html"
_STATIC_DIR = _WEB_DIR / "static"

app = FastAPI(
    title="auto-video-editor workflow monitor",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/", include_in_schema=False)
async def index():
    """监控首页(只读;刷新 = 重新 GET,绝不触发工作流)。"""
    if not _MONITOR_HTML.exists():
        return Response(
            "monitor.html not found. Put it under ./web/monitor.html",
            media_type="text/plain",
            status_code=404,
        )
    return FileResponse(_MONITOR_HTML, media_type="text/html")


@app.get("/node-map", include_in_schema=False)
async def node_map():
    """工作流节点图页(与 agent_fastapi 时代的路由名保持一致)。"""
    if not _NODE_MAP_HTML.exists():
        return Response(
            "node_map.html not found. Put it under ./web/node_map/node_map.html",
            media_type="text/plain",
            status_code=404,
        )
    return FileResponse(_NODE_MAP_HTML, media_type="text/html")


app.include_router(router)

if _NODE_MAP_DIR.is_dir():
    app.mount("/node_map", StaticFiles(directory=str(_NODE_MAP_DIR)), name="node_map")

if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
