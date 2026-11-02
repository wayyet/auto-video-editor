"""工作流监控 API:``/api/workflow/*``(设计《工作流流程图与实时节点追踪》§6.2-E,阶段4)。

宿主与代码组织
--------------
- 宿主是 ``agent_fastapi.py`` 的 FastAPI 应用(uvicorn 绑定 ``127.0.0.1:7860``,
  start_production.ps1:678)。本模块是**独立 router**,由主文件
  ``app.include_router(...)`` 挂载 —— 与 ``/api/system/import-video`` 同层,
  不进 FastMCP 的 ``server.py``,也不再膨胀主文件(设计 §11.8)。
- 导入方式:``from web.api_workflow import router``(仓库根在 sys.path 上,
  ``web`` 为命名空间包;2026-11-01 扁平化后本文件随 web/ 上移仓库根)。

六个端点(设计 §6.2-E 表 + 执行设计《对话框显示 LangGraph 当前运行节点》§5.1)
------------------------
=========================================== =================================================
``GET  /graph``                             §4.3 图 JSON(拓扑服务缓存;只构图不执行,§11.5)
``POST /runs``                              显式启动运行(主 venv CLI 子进程;见下"关键决策")
``GET  /runs/{thread_id}``                  运行快照:事件计数/游标/最近事件/派生状态
``GET  /runs/{thread_id}/events``           SSE 事件流;``Last-Event-ID`` 补发
``POST /runs/{thread_id}/resume``           挂起探测 + 外部恢复指引(见下"关键决策")
``GET  /chat-sessions/{chat_session_id}/run`` 会话关联查询(只读;执行设计 §5.1,严格 GET)
=========================================== =================================================

关键决策(为什么这样落)
----------------------
1. **启动与订阅分离**(§6.2-E 要点 1):``GET .../events`` 只读事件表,
   绝不触发图执行;页面刷新 = 重复 GET = 只重放,不产生第二次 run。
2. **先持久化,后推送 / 不依赖内存队列**(§11.4):本模块不自产事件 ——
   事件由统一 runner 写入共享 PG 事件表(CLI 子进程 / ``agent_gate`` worker
   分属不同进程、不同 venv);SSE = 从事件表补发 + 短轮询跟进。
3. **POST /runs 用主 venv CLI 子进程**:Web 进程(openstoryline venv)的
   langgraph 是 1.1.10,与生产运行时(主 venv 1.2.9 + editable
   langgraph-checkpoint 4.1.1)**不同版本** —— 在 Web 进程内跑图会踩
   checkpoint 格式兼容风险。子进程跑 ``scripts/run_workflow.py``:它经统一
   runner(阶段3),产出事件与 CLI 完全一致(§9.7 任务 5),且不把 Web 服务
   变成图运行时(§3.2"不另造任务调度系统")。
4. **resume 端点取"仅查询"形态**:设计 §6.2-E 明确允许
   ("或仅查询外部恢复后的事件")。理由同 3 —— Web 进程对生产 checkpoint 执行
   ``Command(resume=...)`` 有跨版本写坏 checkpoint 的风险。本端点做挂起探测 +
   返回外部恢复通道指引;外部恢复(agent_gate MCP ``human_resume`` / 人工脚本)
   完成后,事件按游标自然补发。
5. **访问控制**(§6.2-E 要点 4):uvicorn 已绑定 127.0.0.1;本模块再校验
   ``Origin``,只接受本机来源,默认拒绝跨源。
6. **事件存储不可用必须显式报错**(§6.2-D):连接时
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
import subprocess
import sys
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
# 常量(本模块本地,不依赖主仓 config —— 跨 venv 轻量化)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
#: Web 进程拉起运行用的主 venv 解释器(统一 runner 在那边)
_MAIN_VENV_PYTHON = _REPO_ROOT / ".venv" / "Scripts" / "python.exe"
_MAIN_CLI = _REPO_ROOT / "scripts" / "run_workflow.py"
#: POST /runs 子进程日志(工作区 gitignore 范围内)
_WEB_RUNS_DIR = _REPO_ROOT / "outputs" / "web_runs"

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
    """访问控制(§6.2-E 要点 4):有 Origin 头时必须是本机来源,默认拒绝跨源。

    curl / 同源 GET 导航不带 Origin,直接放行;浏览器跨源请求一定带 Origin。
    uvicorn 已绑定 127.0.0.1,这里是第二道防线(防本机其它页面/代理转发)。
    """
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return
    host = (urlsplit(origin).hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(status_code=403, detail=f"cross-origin rejected: {origin}")


async def _json_body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


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
# POST /runs —— 显式启动运行(主 venv CLI 子进程)
# ---------------------------------------------------------------------------
def _spawn_cli_run(thread_id: str, initial_state: Optional[dict], *, preflight: bool) -> dict[str, Any]:
    """子进程拉起主 venv CLI(统一 runner;关键决策 3)。

    返回 spawn 信息 dict;失败抛 ``RuntimeError``(端点转 500)。
    输出重定向到 ``outputs/web_runs/<thread_id>.log``(thread_id 已过白名单,
    无路径注入面)。
    """
    python = str(_MAIN_VENV_PYTHON) if _MAIN_VENV_PYTHON.exists() else sys.executable
    if not _MAIN_CLI.exists():
        raise RuntimeError(f"主 CLI 不存在: {_MAIN_CLI}")

    args = [python, str(_MAIN_CLI), "--thread-id", thread_id]
    if not preflight:
        # Web 拉起默认跳过 pre-flight:它需要管理员权限(Start-Service),
        # uvicorn 进程通常没有;环境就绪由"Web 服务本身在跑"背书。
        args.append("--skip-preflight")
    state_file: Optional[Path] = None
    if initial_state:
        _WEB_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        state_file = _WEB_RUNS_DIR / f"initial_state_{thread_id}.json"
        state_file.write_text(
            json.dumps(initial_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        args += ["--initial-state-json", str(state_file)]

    _WEB_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _WEB_RUNS_DIR / f"{thread_id}.log"
    kw: dict[str, Any] = {}
    if sys.platform == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=logf,
                cwd=str(_REPO_ROOT),
                close_fds=True,
                **kw,
            )
    except OSError as e:
        raise RuntimeError(f"子进程拉起失败: {e}") from e
    return {
        "pid": proc.pid,
        "log": str(log_path),
        "python": python,
        "preflight": bool(preflight),
    }


def _new_thread_id() -> str:
    import uuid

    return f"video-web-{uuid.uuid4().hex[:8]}"


@router.post("/runs")
async def start_run(request: Request):
    """显式启动运行(§6.2-E:启动与订阅分离;重复启动防护见 409)。

    Body(全部可选)::

        {"thread_id": "...", "session_id": "...",
         "chat_session_id": "...",   # 聊天会话 ID(执行设计 §4.2,可选)
         "video_input_path": "...", "video_input_paths": ["...", ...],
         "preflight": false}

    返回 202 + spawn 信息;运行事件由统一 runner 落 PG 事件表,
    用 ``GET /runs/{thread_id}/events`` 订阅。
    """
    _verify_same_origin(request)
    body = await _json_body(request)

    thread_id = str(body.get("thread_id") or "").strip() or _new_thread_id()
    tid = _validate_thread_id(thread_id)

    # 防重复启动:事件表里已有未结束的运行 → 409(§11.5:刷新/重连不得触发运行)
    snap = await run_in_threadpool(_snapshot, tid)
    if snap.get("available") and int(snap.get("event_count") or 0) > 0:
        state = snap.get("state")
        if state in ("running", "waiting_human"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"thread {tid} 已有进行中的运行(state={state});"
                    "请订阅 GET /api/workflow/runs/"
                    + tid
                    + "/events 观察,不要重复启动"
                ),
            )

    # initial state:白名单字段(§3.1 第 9 条:不透传任意 state)
    state: dict[str, Any] = {}
    session_id = str(body.get("session_id") or "").strip()
    if session_id:
        state["session_id"] = session_id
    # 聊天会话 ID(执行设计 §4.2):与业务 session_id 分开保存;格式非法 → 400,
    # 合法时写入初始 State,由统一 runner 建立 workflow_chat_links 关联。
    chat_session_id_raw = str(body.get("chat_session_id") or "").strip()
    if chat_session_id_raw:
        from monitoring.workflow_chat_link_store import (  # 延迟:轻 import
            ChatLinkValidationError,
            validate_chat_session_id,
        )

        try:
            state["chat_session_id"] = validate_chat_session_id(chat_session_id_raw)
        except ChatLinkValidationError as e:
            raise HTTPException(status_code=400, detail=f"chat_session_id 非法: {e}")
    paths = body.get("video_input_paths")
    single = str(body.get("video_input_path") or "").strip()
    if isinstance(paths, list) and paths:
        cleaned = [str(p).strip() for p in paths if str(p or "").strip()]
        if cleaned:
            state["video_input_paths"] = cleaned
            state["video_input_path"] = cleaned[0]
    elif single:
        state["video_input_path"] = single

    preflight = bool(body.get("preflight"))
    try:
        info = await run_in_threadpool(
            _spawn_cli_run, tid, state or None, preflight=preflight
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("workflow run spawned via API: thread=%s pid=%s", tid, info["pid"])
    return JSONResponse(
        {
            "ok": True,
            "thread_id": tid,
            "spawned": True,
            "pid": info["pid"],
            "log": info["log"],
            "preflight": info["preflight"],
            "events_url": f"/api/workflow/runs/{tid}/events",
            "note": "事件由统一 runner 落 PG 事件表;订阅 events_url 观察",
        },
        status_code=202,
    )


# ---------------------------------------------------------------------------
# POST /runs/{thread_id}/resume —— 挂起探测 + 外部恢复指引(关键决策 4)
# ---------------------------------------------------------------------------
@router.post("/runs/{thread_id}/resume")
async def resume_probe(request: Request, thread_id: str):
    """挂起探测 + 外部恢复指引(设计 §6.2-E 允许的"仅查询"形态)。

    Web 进程对生产 checkpoint 执行 ``Command(resume=...)`` 有跨 langgraph
    版本写坏 checkpoint 的风险(关键决策 4),不在本端点做。真实恢复走
    既有外部通道(agent_gate MCP ``human_resume`` / 人工脚本),完成后
    事件按游标自然补发到订阅端。
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
                "外部通道(agent_gate MCP human_resume / 人工脚本)对同一 "
                "thread_id 执行 Command(resume=...) 后,事件将由统一 runner "
                "落库,本 SSE 流按游标继续补发"
            ),
        }
    )


# ---------------------------------------------------------------------------
# GET /chat-sessions/{chat_session_id}/run —— 会话关联查询(只读;执行设计 §5.1)
# ---------------------------------------------------------------------------
@router.get("/chat-sessions/{chat_session_id}/run")
async def chat_session_run(request: Request, chat_session_id: str):
    """按聊天会话 ID 查询最近一次关联的工作流线程(执行设计 §5.1)。

    **严格只读**:不触发 start / resume / 任何状态写(§10.3 红线 5)。

    - 有关联 → 200 ``{"available": true, "chat_session_id", "thread_id",
      "run_id", "status", "updated_at"}``;
    - 尚无关联 → 200 ``{"available": true, "run": null}``(隐藏组件,不是错误);
    - 会话 ID 格式非法 → 400;
    - 关联存储(PostgreSQL)不可用 → 503 —— **不得**冒充"无关联",否则对话页
      会把"查不了"当成"没在跑"(§5.1)。
    """
    _verify_same_origin(request)
    from monitoring.workflow_chat_link_store import (  # 延迟:轻 import
        ChatLinkStoreUnavailable,
        ChatLinkValidationError,
        WorkflowChatLinkStore,
        validate_chat_session_id,
    )

    try:
        csid = validate_chat_session_id(chat_session_id)
    except ChatLinkValidationError as e:
        raise HTTPException(status_code=400, detail=f"chat_session_id 非法: {e}")

    try:
        link = await run_in_threadpool(
            WorkflowChatLinkStore().get_by_chat_session, csid
        )
    except ChatLinkStoreUnavailable as e:
        logger.error("chat link query unavailable: %s", e)
        raise HTTPException(
            status_code=503,
            detail="workflow chat link unavailable — 关联存储不可用,请检查 Postgres",
        )
    except Exception as e:  # noqa: BLE001 — 其它查询异常同样按 503 显式暴露
        logger.error("chat link query failed: %s: %s", type(e).__name__, e)
        raise HTTPException(
            status_code=503,
            detail="workflow chat link unavailable — 关联存储不可用,请检查 Postgres",
        )

    if link is None:
        return JSONResponse({"available": True, "run": None})
    return JSONResponse({"available": True, **link})
