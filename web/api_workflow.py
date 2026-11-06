"""工作流监控 API:``/api/workflow/*``(**只读**;单流水线架构 2026-11-04 阶段1)。

宿主与代码组织
--------------
- 宿主是仓库根 ``monitor_fastapi.py`` 的独立 FastAPI 应用(uvicorn 绑定
  ``127.0.0.1:7860``;主 `.venv`):``python -m uvicorn monitor_fastapi:app``。
- 本模块是**独立只读 router**:`from web.api_workflow import router`。
- 2026-11-04 起,监控不再承担工作流启动职责:工作流触发只有两条路径 ——
  A. 直接 CLI ``scripts/run_workflow.py``;B. ``agent_gate``(MCP / drive)。
  原 ``POST /runs``(CLI 子进程拉起)与 ``GET /chat-sessions/{id}/run``
  (聊天会话关联查询)已随旧聊天运行时下线一并移除。

四个端点
--------
=========================================== =================================================
``GET  /graph``                             图 JSON(拓扑服务缓存;只构图不执行,§11.5)
``GET  /runs/{thread_id}``                  运行快照:事件计数/游标/最近事件/派生状态
``GET  /runs/{thread_id}/events``           SSE 事件流;``Last-Event-ID`` 补发
``GET  /runs/{thread_id}/resume-status``    挂起探测 + 外部恢复指引(见下"关键决策")
=========================================== =================================================

关键决策(为什么这样落)
----------------------
1. **监控只读**(评审修订版计划 §4.4):本 router 只查询、不启动、不恢复、
   不修改运行状态。页面刷新 = 重复 GET = 只重放,不产生第二次 run。
2. **先持久化,后推送 / 不依赖内存队列**(§11.4):本模块不自产事件 ——
   事件由统一 runner 写入共享 PG 事件表(直接 CLI / ``agent_gate`` worker
   分属不同进程);SSE = 从事件表补发 + 短轮询跟进。
3. **resume 端点取"仅查询"形态,并以 GET 命名为 ``resume-status``**
   (R-22:避免接口名让人误以为会执行恢复):本端点做挂起探测 +
   返回外部恢复通道指引;实际恢复只通过对应执行控制器(agent_gate
   ``resume`` / 直接 CLI 的恢复入口),完成后事件按游标自然补发。
4. **访问控制**(§6.2-E 要点 4):uvicorn 已绑定 127.0.0.1;本模块再校验
   ``Origin``,只接受本机来源,默认拒绝跨源。
5. **事件存储不可用必须显式报错**(§6.2-D):连接时
   ``latest_snapshot().available=False`` → 503;流中断连 → ``store_error``
   帧后关流。不得静默把已开始的任务显示为成功。

SSE 行为细节
------------
- 无 ``Last-Event-ID``:先发 ``snapshot`` 帧,再从 id=0 全量补发,再进实时。
- 有 ``Last-Event-ID``(浏览器 EventSource 断线自动重连会带):从
  ``id > Last-Event-ID`` 补发,无重复无遗漏。
- 终态事件(``run.completed`` / ``run.failed``)发出后关流 —— 客户端重连后
  会得到同样终态并再次收到后关闭,形成稳定的"已结束"语义。
- 实时跟进 = 每 0.8s 轮询事件表新增行;每 15s 发 ``: ping`` 注释帧保活。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workflow")

# ---------------------------------------------------------------------------
# 常量(本模块本地,不依赖主仓 config —— 跨进程轻量化)
# ---------------------------------------------------------------------------
def repo_root() -> Path:
    """仓库根(= ``web/`` 的父目录)。

    R-05 修正:旧实现 ``parents[2]`` 解析到仓库**父**目录(历史包袱:
    本文件 2026-11-01 前位于 ``web/`` 之外)。所有派生路径必须从本函数取。
    """
    return Path(__file__).resolve().parents[1]


_REPO_ROOT = repo_root()

#: thread_id 白名单(与 workflow_runs 表 CHECK 同规则;同时防路径注入)
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")

#: SSE 实时轮询间隔 / 保活间隔 / 存储健康复查间隔(秒)
_SSE_POLL_S = 0.8
_SSE_PING_S = 15.0
_SSE_STORE_CHECK_S = 20.0
#: fetch_events_after 单页大小(与存储层 DEFAULT_FETCH_LIMIT 一致)
_SSE_PAGE = 500

#: 终态事件:发出后关流(§9.7 任务 3:结束态发终态事件后关闭流)
_TERMINAL_EVENT_TYPES = frozenset({"run.completed", "run.failed"})

#: 最近一次派生状态的缓存注释……(无;派生是纯函数)
_LAST_EVENT_STATE = {
    "run.completed": "completed",
    "run.failed": "failed",
    "run.interrupted": "waiting_human",
    "node.waiting_input": "waiting_human",
    "node.started": "running",
    "node.resumed": "running",
    "node.retrying": "running",
    "node.progress": "running",
}


def _derive_state(last_event: Any) -> str:
    """从最近事件派生运行状态(快照/快照帧用;前端可按事件流自行精算)。"""
    if not isinstance(last_event, dict):
        return "no_events"
    return _LAST_EVENT_STATE.get(str(last_event.get("event_type") or ""), "idle")


def _validate_thread_id(thread_id: str) -> str:
    value = str(thread_id or "").strip()
    if not _THREAD_ID_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=(
                "thread_id 非法:只允许 [A-Za-z0-9_.:-],长度 1-128"
                "(与 workflow_runs 表 CHECK 一致)"
            ),
        )
    return value


def _verify_same_origin(request: Request) -> None:
    """访问控制(§6.2-E 要点 4):有 Origin 头时必须是同源,默认拒绝跨源。

    curl / 同源 GET 导航不带 Origin,直接放行;浏览器跨源请求一定带 Origin。
    uvicorn 绑定 0.0.0.0(局域网可访问)后,这里是第二道防线:
    - loopback Origin(本机工具/本机页面)放行;
    - 其余 Origin 的 host:port 必须与请求 Host 头一致(浏览器同源场景必然一致,
      即从本机 IP 或局域网 IP 打开的页面取到的 Origin 与其请求的 Host 天然匹配);
    - 其它(跨站页面、代理改写 Host 的转发)一律 403。
    """
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return
    host = (urlsplit(origin).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    host_header = (request.headers.get("host") or "").strip().lower()
    origin_netloc = urlsplit(origin).netloc.lower()
    if not host_header or origin_netloc != host_header:
        raise HTTPException(status_code=403, detail=f"cross-origin rejected: {origin}")


# ---------------------------------------------------------------------------
# GET /graph —— 拓扑(模块级缓存;首次构建含主仓 graph 导入,~13s)
# ---------------------------------------------------------------------------
_schema_lock = threading.Lock()
_schema_cache: Optional[dict[str, Any]] = None
_schema_built_at: float = 0.0


def _build_schema_cached(refresh: bool) -> dict[str, Any]:
    """构图 + 拓扑服务(同步、重;经 run_in_threadpool 调,锁内串行)。

    实测(2026-11-01,openstoryline venv / langgraph 1.1.10):可导入主仓
    graph(13s 重依赖);构图 2.3s;``build_graph_schema`` 输出与主 venv
    基线一致(55 节点 / 61 边 / 13 条件边)。只构图、不执行(§11.5)。
    """
    global _schema_cache, _schema_built_at
    with _schema_lock:
        if _schema_cache is not None and not refresh:
            return _schema_cache
        import config as _av_config  # 主仓 config(仓库根在 sys.path,agent_fastapi 已注入)
        from graph import build_graph as _build_graph
        from langgraph.checkpoint.memory import InMemorySaver

        from monitoring.workflow_graph import build_graph_schema

        compiled = _build_graph(
            checkpointer=InMemorySaver(),
            thread_id="web-schema-view",
            start_heartbeat_thread=False,
            run_preflight=False,
        )
        _schema_cache = build_graph_schema(compiled, _av_config)
        _schema_built_at = time.time()
        return _schema_cache


@router.get("/graph")
async def workflow_graph(request: Request, refresh: bool = False):
    """§4.3 图 JSON(节点/边/元数据/flags/mode)。``?refresh=1`` 重建缓存。"""
    _verify_same_origin(request)
    try:
        schema = await run_in_threadpool(_build_schema_cached, bool(refresh))
    except Exception as e:  # noqa: BLE001 — 图编译失败对页面是 503,不是 500 裸栈
        logger.error("workflow graph schema build failed: %s: %s", type(e).__name__, e)
        raise HTTPException(
            status_code=503,
            detail=f"graph schema unavailable: {type(e).__name__}: {e}",
        )
    return JSONResponse(schema)


# ---------------------------------------------------------------------------
# 事件存储访问(统一延迟 import + 线程池,psycopg 是同步驱动)
# ---------------------------------------------------------------------------
def _store():
    from monitoring import workflow_event_store as store

    return store


def _snapshot(thread_id: str) -> dict[str, Any]:
    snap = _store().latest_snapshot(thread_id)
    if not isinstance(snap, dict):
        snap = {"thread_id": thread_id, "available": False, "event_count": 0,
                "first_id": None, "last_id": None, "last_event": None, "node_attempts": {}}
    snap.setdefault("state", _derive_state(snap.get("last_event")))
    return snap


# ---------------------------------------------------------------------------
# GET /runs/{thread_id} —— 运行快照
# ---------------------------------------------------------------------------
@router.get("/runs/{thread_id}")
async def run_snapshot(request: Request, thread_id: str):
    """运行快照:事件计数、游标(``last_id``)、最近事件、派生状态、attempt 汇总。"""
    _verify_same_origin(request)
    tid = _validate_thread_id(thread_id)
    snap = await run_in_threadpool(_snapshot, tid)
    if not snap.get("available"):
        return JSONResponse(
            {"detail": "event store unavailable — 实时历史不可用,请检查 Postgres"},
            status_code=503,
        )
    return JSONResponse(snap)


# ---------------------------------------------------------------------------
# GET /runs/{thread_id}/events —— SSE
# ---------------------------------------------------------------------------
def _parse_last_event_id(request: Request) -> Optional[int]:
    raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return value if value >= 0 else None


async def _sse_stream(thread_id: str, start_after: int) -> AsyncIterator[str]:
    store = _store()
    to_sse_frame = None
    from monitoring.workflow_events import to_sse_frame as _frame  # 延迟:轻 import

    to_sse_frame = _frame

    # 1) 快照摘要帧(§6.2-E 要点 2:无游标也先给当前快照,前端可冷启动)
    try:
        snap = await run_in_threadpool(_snapshot, thread_id)
    except Exception as e:  # noqa: BLE001
        yield f"event: store_error\ndata: {json.dumps({'detail': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"
        return
    snap_payload = {
        "thread_id": snap.get("thread_id"),
        "available": snap.get("available"),
        "event_count": snap.get("event_count"),
        "first_id": snap.get("first_id"),
        "last_id": snap.get("last_id"),
        "state": snap.get("state"),
        "node_attempts": snap.get("node_attempts") or {},
    }
    yield f"event: snapshot\ndata: {json.dumps(snap_payload, ensure_ascii=False)}\n\n"

    cursor = int(start_after)
    last_beat = time.monotonic()
    last_store_check = time.monotonic()

    # 2) 历史补发:id > cursor,分页到追平
    while True:
        try:
            batch = await run_in_threadpool(store.fetch_events_after, thread_id, cursor, _SSE_PAGE)
        except Exception as e:  # noqa: BLE001
            yield f"event: store_error\ndata: {json.dumps({'detail': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"
            return
        if not batch:
            break
        for ev in batch:
            yield to_sse_frame(ev)
            cursor = max(cursor, int(ev.get("id") or 0))
        if len(batch) < _SSE_PAGE:
            break

    # 3) 实时跟进:短轮询共享事件表(§11.4:跨进程事件只能这样到达本进程)
    while True:
        try:
            batch = await run_in_threadpool(store.fetch_events_after, thread_id, cursor, _SSE_PAGE)
        except Exception as e:  # noqa: BLE001
            yield f"event: store_error\ndata: {json.dumps({'detail': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"
            return
        terminal_hit = False
        for ev in batch or []:
            yield to_sse_frame(ev)
            cursor = max(cursor, int(ev.get("id") or 0))
            if ev.get("event_type") in _TERMINAL_EVENT_TYPES:
                terminal_hit = True
        if terminal_hit:
            return  # 结束态:发终态事件后关流(§9.7 任务 3)
        now = time.monotonic()
        if now - last_store_check >= _SSE_STORE_CHECK_S:
            last_store_check = now
            try:
                snap = await run_in_threadpool(_snapshot, thread_id)
            except Exception:  # noqa: BLE001
                snap = {"available": False}
            if not snap.get("available"):
                yield (
                    "event: store_error\ndata: "
                    + json.dumps({"detail": "event store unavailable — 实时历史不可用,请检查 Postgres"},
                                 ensure_ascii=False)
                    + "\n\n"
                )
                return
        if not batch and now - last_beat >= _SSE_PING_S:
            last_beat = now
            yield ": ping\n\n"
        await asyncio.sleep(_SSE_POLL_S)


@router.get("/runs/{thread_id}/events")
async def run_events(request: Request, thread_id: str):
    """SSE 事件流;``Last-Event-ID``(头或 ``?last_event_id=``)断线补发。

    绝不触发图执行(§6.2-E 要点 1);连接时先显式检查事件存储可用性,
    不可用 → 503(§6.2-D:不得静默降级)。
    """
    _verify_same_origin(request)
    tid = _validate_thread_id(thread_id)
    snap = await run_in_threadpool(_snapshot, tid)
    if not snap.get("available"):
        raise HTTPException(
            status_code=503,
            detail="event store unavailable — 实时历史不可用,请检查 Postgres",
        )
    last_event_id = _parse_last_event_id(request)
    start_after = int(last_event_id) if last_event_id is not None else 0

    headers = {
        "Cache-Control": "no-cache",
        # 关掉可能存在的反向代理缓冲,保证帧即时到达
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        _sse_stream(tid, start_after),
        media_type="text/event-stream",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# GET /runs/{thread_id}/resume-status —— 挂起探测 + 外部恢复指引(关键决策 3)
# ---------------------------------------------------------------------------
@router.get("/runs/{thread_id}/resume-status")
async def resume_probe(request: Request, thread_id: str):
    """挂起探测 + 外部恢复指引(只读;**不执行恢复**)。

    命名与动词(R-22):恢复是写操作,监控不做 —— 本端点只报告"是否挂在
    人工关卡、该由谁恢复"。实际恢复走对应执行控制器(``agent_gate resume``
    / 直接 CLI 的恢复入口),对同一 ``thread_id`` 执行 ``Command(resume=...)``;
    完成后事件由统一 runner 落库,本 SSE 流按游标继续补发。
    """
    _verify_same_origin(request)
    tid = _validate_thread_id(thread_id)
    snap = await run_in_threadpool(_snapshot, tid)
    if not snap.get("available"):
        raise HTTPException(
            status_code=503,
            detail="event store unavailable — 实时历史不可用,请检查 Postgres",
        )
    state = snap.get("state")
    if state != "waiting_human":
        raise HTTPException(
            status_code=409,
            detail=f"thread {tid} 未处于人工等待状态(state={state});无需 resume",
        )
    last_event = snap.get("last_event") or {}
    return JSONResponse(
        {
            "ok": True,
            "thread_id": tid,
            "state": state,
            "last_event": {
                "id": last_event.get("id"),
                "event_type": last_event.get("event_type"),
                "node_id": last_event.get("node_id"),
                "occurred_at": last_event.get("occurred_at"),
            },
            "resume_via": (
                "外部执行控制器(agent_gate resume / 直接 CLI 恢复入口)对同一 "
                "thread_id 执行 Command(resume=...) 后,事件将由统一 runner "
                "落库,本 SSE 流按游标继续补发"
            ),
        }
    )
