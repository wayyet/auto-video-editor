"""统一 WorkflowRunner(设计《工作流流程图与实时节点追踪》§6.2-B/C,阶段3)。

职责与边界
----------
- **唯一 async 执行入口**:封装 ``astream(stream_mode=["tasks","updates","custom"],
  version="v2")`` 的完整消费循环,把 LangGraph 流事件经
  :class:`WorkflowStreamAdapter` 转成 §7.1 标准事件。``scripts/run_workflow.py``
  的 ``ainvoke``(:259)已迁移到这里;CLI / Web API 启动的运行因此产出**同一套**
  事件(设计 §6.2-B"避免一套路径能显示、另一套没有事件")。
- **先落库,后推送**(§6.2-E 实现要点 2):每个事件先 ``store.append_event``
  拿全局 ``id``,再回调 ``on_event``(进程内广播器只是实时加速层;跨进程的
  ``agent_gate`` worker 事件由共享 PG 事件表补发,不走本模块)。
- **不改变执行语义**:节点业务函数、重试策略、checkpointer 与
  ``configurable.thread_id`` 全部原样传入;返回值 = 最终 state values
  (经 ``aget_state`` 取,与 ``ainvoke`` 的返回语义一致,CLI 退出码判定不变)。
- **异常兜底**:节点异常在流里(task end ``error``)与外层 raise **双现**
  (基线记录 §4.5),adapter 只发一次 ``node.failed``,外层统一发 ``run.failed``
  后原样 re-raise —— 不吞异常,CLI 退出码行为与迁移前一致。
- 本模块**顶层不 import langgraph**(图对象由调用方注入,``Command`` 判定
  延迟到函数内)—— 纯逻辑部分(适配器)可脱离 langgraph 单测。

流事件契约(langgraph 1.2.9 实测,完整样例见
``docs/修正/auto-video-editor_工作流流程图_阶段0基线记录.md`` §4):

1. 统一封套 ``{"type": "tasks"|"updates"|"custom", "ns": [...], "data": {...}}``;
2. task start ``{id, name, input, triggers}`` / task end
   ``{id, name, error, result, interrupts}``,同 ``id`` 关联;
3. interrupt 在 tasks.end.interrupts 与 updates.__interrupt__ **双现** ——
   以 tasks.end 为准(§4.6 契约 3:此时**不发** ``node.succeeded``);
4. resume 复跑**同一 task id** → ``node.resumed``(§4.6 契约 4);
5. ``triggers`` 含 ``join:a+b:x`` → fan-in 汇合(免费信号),可推
   ``branch.selected``;``branch:to:<node>`` 为普通触发,配合"最近完成节点
   ∩ 条件入边源"做时序推断(§7.3 规则 8,``inferred=true``)。

用法::

    from graph import build_graph, get_checkpointer
    from monitoring.workflow_runner import run_with_events

    async with get_checkpointer() as saver:
        g = build_graph(checkpointer=saver, thread_id=tid, ...)
    result = await run_with_events(
        g, state, {"configurable": {"thread_id": tid}},
        on_event=print,           # 可选:进程内订阅者
        store=None,               # None = 默认 PG 事件表(monitoring.workflow_event_store)
    )
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from typing import Any, Callable, Optional

from monitoring.workflow_events import (
    EVENT_BRANCH_SELECTED,
    EVENT_NODE_FAILED,
    EVENT_NODE_PROGRESS,
    EVENT_NODE_RESUMED,
    EVENT_NODE_RETRYING,
    EVENT_NODE_STARTED,
    EVENT_NODE_SUCCEEDED,
    EVENT_NODE_WAITING_INPUT,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    EVENT_RUN_INTERRUPTED,
    EVENT_RUN_RESUMED,
    EVENT_RUN_STARTED,
    WAITING_KIND_INTERRUPT,
    WAITING_KIND_WEB_FILE_HANDSHAKE,
    make_event,
    sanitize_exception,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_STREAM_MODE",
    "STREAM_VERSION",
    "STATE_SUMMARY_KEYS",
    "WorkflowStreamAdapter",
    "run_with_events",
    "run_id_new",
]

#: 流模式(设计 §6.2-C:tasks 生命周期 + updates 增量 + custom 节点主动上报)
DEFAULT_STREAM_MODE = ("tasks", "updates", "custom")
#: v2 统一封套(langgraph 1.2.9 默认 v1,**必须**显式传,基线记录 §1/§4)
STREAM_VERSION = "v2"

#: state 增量里允许进 ``summary`` 的白名单键(§7.3 规则 3:不把整个 state 推给
#: 浏览器)。值仍会经 make_event 的递归脱敏(绝对路径降级文件名等)。
STATE_SUMMARY_KEYS = frozenset({
    "import_video_status",
    "import_video_filename",
    "import_video_media_paths",
    "import_video_web_session_id",
    "openstoryline_ready",
    "draft_encryption_status",
    "video_input_paths",
})

#: run.* 事件 → ``workflow_chat_links`` 状态(执行设计 §10.4 任务 2.3)。
#: **只挂钩 run 级事件** —— interrupt 挂起必然伴随 ``run.interrupted``,由它
#: 负责 waiting_human;``node.waiting_input`` 不挂钩,因为 import_video 式
#: custom 握手等待(web_file_handshake)是运行中的等待,工作流并未挂起,
#: 稍后会正常 node.succeeded / run.completed。
_CHAT_LINK_STATUS_BY_RUN_EVENT = {
    EVENT_RUN_STARTED: "running",
    EVENT_RUN_RESUMED: "running",
    EVENT_RUN_INTERRUPTED: "waiting_human",
    EVENT_RUN_COMPLETED: "completed",
    EVENT_RUN_FAILED: "failed",
}


def run_id_new() -> str:
    """一次业务运行的标识(恢复运行沿用同一 thread_id,但每次 runner 调用新 run_id)。"""
    return f"run-{uuid.uuid4().hex[:12]}"


def _is_task_end(data: dict[str, Any]) -> bool:
    """task end 判据(§4.6 契约 1):end 携带 error / result / interrupts 之一;
    start 只有 input + triggers。"""
    return any(k in data for k in ("error", "result", "interrupts"))


def summarize_state_delta(result: Any) -> dict[str, Any]:
    """把节点返回的 state 增量转成白名单摘要(§7.3 规则 3)。

    - 白名单键:带脱敏后的值(绝对路径已降级文件名 —— 由 make_event 统一执行);
    - 其余键:只出现在 ``keys`` 名单里,值一律不外传
      (提示词 / ASR 全文 / 大对象天然被挡住)。
    """
    if not isinstance(result, dict):
        return {"keys": []}
    summary: dict[str, Any] = {"keys": sorted(str(k) for k in result)}
    for key in sorted(STATE_SUMMARY_KEYS & result.keys()):
        summary[key] = result[key]
    return summary


def _normalize_task_error(err: Any) -> dict[str, str]:
    """task end 的 ``error`` 字段(契约样例形如 ``{"ValueError": "msg"}``)
    归一为 ``{"type", "message"}``,再由 make_event 统一脱敏。"""
    if isinstance(err, BaseException):
        return sanitize_exception(err)
    if isinstance(err, dict) and len(err) == 1:
        etype, message = next(iter(err.items()))
        return {"type": str(etype), "message": str(message)}
    if isinstance(err, str):
        return {"type": "error", "message": err}
    return {"type": "error", "message": repr(err)}


def _normalize_interrupt_value(item: Any) -> Optional[dict[str, Any]]:
    """interrupt 对象(有 .value/.id)或 dict → 统一 dict;解析失败返回 None。"""
    if isinstance(item, dict):
        value = item.get("value")
        iid = item.get("id")
    else:
        value = getattr(item, "value", None)
        iid = getattr(item, "id", None)
    if not isinstance(value, dict):
        value = {"value": value}
    out = {"value": value}
    if iid is not None:
        out["id"] = str(iid)
    return out


class WorkflowStreamAdapter:
    """把 v2 流 chunk 转成 §7.1 标准事件(§7.3 解析规则)。

    非线程安全;**单次运行**一个实例(状态机:task 起止关联 / attempt 计数 /
    最近完成节点窗口)。``emit`` 签名与 :func:`run_with_events` 内部的一致::

        emit(event_type, *, node_id=None, attempt=1, payload=None)
    """

    #: "最近完成节点"窗口上限(branch.selected 时序推断用;主图最大屏障 4 源,
    #: 8 足够覆盖且限制陈旧窗口的误推断面)
    RECENT_FINISHED_MAX = 8

    def __init__(
        self,
        emit: Callable[..., None],
        *,
        conditional_edges: frozenset[tuple[str, str]] = frozenset(),
        resumed_nodes: frozenset[str] = frozenset(),
        previous_attempts: Optional[dict[str, int]] = None,
    ) -> None:
        self._emit = emit
        self._conditional_edges = conditional_edges
        #: 恢复运行前快照里带 interrupt 的任务名(跨 runner 调用的 resume 判定:
        #: adapter 是每 run 新建的,"同一 task id 二次 start"的内存状态不可跨调用,
        #: §4.6 契约 4 改由运行前快照提供)
        self._resumed_nodes = set(resumed_nodes)
        # task_id -> {"name", "attempt", "started_at", "interrupted"}
        self._tasks: dict[str, dict[str, Any]] = {}
        # 正在执行(已 start 未 end)的 task_id -> node 名(custom 事件归属用)
        self._active: dict[str, str] = {}
        # node 名 -> 已用 attempt(重试递增;§11.1 同一节点可执行多次)
        self._node_attempts: dict[str, int] = dict(previous_attempts or {})
        self._recent_finished: deque[str] = deque(maxlen=self.RECENT_FINISHED_MAX)
        #: 已发过 node.failed(流内 task end error);供 run 级去重参考
        self.node_failed_emitted = False

    # -- 统计(测试 / run.completed 摘要用) ---------------------------------
    @property
    def node_attempt_counts(self) -> dict[str, int]:
        return dict(self._node_attempts)

    # -- tasks 流 -----------------------------------------------------------
    def handle_task_start(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        name = data.get("name")
        task_id = data.get("id")
        if not name or not task_id:
            return
        triggers = [str(t) for t in (data.get("triggers") or []) if t]
        prev = self._tasks.get(task_id)
        is_resumed = name in self._resumed_nodes or (
            prev is not None and prev.get("interrupted")
        )
        if is_resumed:
            # §4.6 契约 4:resume 复跑同一 task id → node.resumed(不是新 attempt)
            self._resumed_nodes.discard(name)
            attempt = int((prev or {}).get("attempt") or self._node_attempts.get(name) or 1)
            if prev is not None:
                prev["interrupted"] = False
                prev["started_at"] = time.monotonic()
            else:
                self._tasks[task_id] = {
                    "name": name,
                    "attempt": attempt,
                    "started_at": time.monotonic(),
                    "interrupted": False,
                }
            self._active[task_id] = name
            self._emit(
                EVENT_NODE_RESUMED,
                node_id=name,
                attempt=attempt,
                payload={"detail": {"triggers": triggers[:4]}},
            )
            self._branch_selected(name, triggers)
            return

        attempt = self._node_attempts.get(name, 0) + 1
        self._node_attempts[name] = attempt
        if attempt > 1:
            # §7.3 规则 5:同一 node 再次启动 = 重试,attempt 递增、保留历史
            self._emit(
                EVENT_NODE_RETRYING,
                node_id=name,
                attempt=attempt,
                payload={"detail": {"previous_attempt": attempt - 1}},
            )
        self._tasks[task_id] = {
            "name": name,
            "attempt": attempt,
            "started_at": time.monotonic(),
            "interrupted": False,
        }
        self._active[task_id] = name
        payload: dict[str, Any] = {}
        if triggers:
            payload["trigger"] = triggers[0]
        self._emit(EVENT_NODE_STARTED, node_id=name, attempt=attempt, payload=payload)
        self._branch_selected(name, triggers)

    def handle_task_end(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        name = data.get("name")
        task_id = data.get("id")
        if not name:
            return
        task = self._tasks.get(task_id) or {}
        attempt = int(task.get("attempt") or self._node_attempts.get(name) or 1)
        started_at = task.get("started_at")
        duration_ms = (
            int((time.monotonic() - started_at) * 1000) if started_at else None
        )
        self._active.pop(task_id, None)

        err = data.get("error")
        interrupts = list(data.get("interrupts") or [])
        if err:
            # §4.6 契约 2:task end error 与外层 raise 双现 → node.failed 只发一次
            payload: dict[str, Any] = {"error": _normalize_task_error(err)}
            if duration_ms is not None:
                payload["duration_ms"] = duration_ms
            self._emit(EVENT_NODE_FAILED, node_id=name, attempt=attempt, payload=payload)
            self.node_failed_emitted = True
        elif interrupts:
            # §4.6 契约 3:interrupt 挂起 → waiting_input + run.interrupted,
            # **不发** node.succeeded(result 为空)。挂起不是失败(设计 §11.3)。
            marks: list[str] = []
            for item in interrupts:
                norm = _normalize_interrupt_value(item)
                if not norm:
                    continue
                cp = norm["value"].get("checkpoint")
                if cp:
                    marks.append(str(cp))
            detail: dict[str, Any] = {"interrupt_count": len(interrupts)}
            if marks:
                detail["checkpoints"] = marks
            self._emit(
                EVENT_NODE_WAITING_INPUT,
                node_id=name,
                attempt=attempt,
                payload={"waiting_kind": WAITING_KIND_INTERRUPT, "detail": detail},
            )
            run_detail: dict[str, Any] = {"node_id": name, **detail}
            self._emit(
                EVENT_RUN_INTERRUPTED,
                payload={
                    "reason": f"等待人工操作({name})",
                    "detail": run_detail,
                },
            )
        else:
            payload = {"summary": summarize_state_delta(data.get("result"))}
            if duration_ms is not None:
                payload["duration_ms"] = duration_ms
            self._emit(
                EVENT_NODE_SUCCEEDED, node_id=name, attempt=attempt, payload=payload
            )

        if task_id:
            if task:
                task["interrupted"] = bool(interrupts)
            self._recent_finished.append(name)

    # -- custom 流 ----------------------------------------------------------
    def handle_custom(self, data: Any) -> None:
        """节点经 ``get_stream_writer`` 主动上报(§7.3 规则 7:import_video 等)。

        约定载荷(节点侧)::

            {"monitor_event": "waiting_input"|"progress", "node_id": str,
             "waiting_kind": str, "detail": {...}}

        ``node_id`` 缺失时:恰好一个活跃 task → 归属它;否则丢弃(记 debug)——
        宁可丢观测信号,也不把等待标到错误节点上。
        """
        if not isinstance(data, dict):
            return
        node_id = str(data.get("node_id") or "").strip() or None
        if node_id is None:
            running = list(set(self._active.values()))
            if len(running) == 1:
                node_id = running[0]
            else:
                logger.debug(
                    "custom 事件缺 node_id 且活跃 task 不唯一(%s),丢弃", running
                )
                return
        attempt = self._node_attempts.get(node_id) or 1
        kind = str(data.get("monitor_event") or "").strip()
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        if kind in ("waiting_input", "waiting"):
            self._emit(
                EVENT_NODE_WAITING_INPUT,
                node_id=node_id,
                attempt=attempt,
                payload={
                    "waiting_kind": str(
                        data.get("waiting_kind") or WAITING_KIND_WEB_FILE_HANDSHAKE
                    ),
                    "detail": detail,
                },
            )
        else:
            self._emit(
                EVENT_NODE_PROGRESS,
                node_id=node_id,
                attempt=attempt,
                payload={"summary": {"monitor_event": kind or "custom", **detail}},
            )

    # -- branch.selected(§7.3 规则 8,全部 inferred=true) -------------------
    def _branch_selected(self, target: str, triggers: list[str]) -> None:
        join_triggers = [t for t in triggers if t.startswith("join:")]
        if join_triggers:
            for t in join_triggers:
                # join:a+b:x → from=[a, b], to=x(汇合点免费信号,基线记录 §4.3)
                body = t[len("join:"):]
                from_part, _, _to = body.partition(":")
                sources = [s for s in from_part.split("+") if s]
                self._emit(
                    EVENT_BRANCH_SELECTED,
                    node_id=target,
                    payload={
                        "label": t,
                        "inferred": True,
                        "detail": {"from": sources, "to": target},
                    },
                )
            return
        if not self._conditional_edges:
            return
        for src in self._recent_finished:
            if (src, target) in self._conditional_edges:
                self._emit(
                    EVENT_BRANCH_SELECTED,
                    node_id=target,
                    payload={
                        "inferred": True,
                        "detail": {"from": [src], "to": target},
                    },
                )


def _conditional_edges_of(graph: Any) -> frozenset[tuple[str, str]]:
    """从编译图 ``builder.branches`` 提取条件边 ``(src, dst)`` 集。

    只用于 branch.selected 时序推断(§7.3 规则 8),不复制拓扑定义;
    ``builder`` 缺失(langgraph 行为变化 / 测试桩)时返回空集 —— 推断降级,
    不影响节点生命周期事件。
    """
    builder = getattr(graph, "builder", None)
    branches = getattr(builder, "branches", None) or {}
    out: set[tuple[str, str]] = set()
    for src, named in branches.items():
        for _name, spec in named.items():
            for _key, dst in (getattr(spec, "ends", None) or {}).items():
                out.add((str(src), str(dst)))
    return frozenset(out)


def _default_store() -> Any:
    """默认事件存储 = 共享 PG 事件表(延迟 import:未接 store 的调用方
    不承担 db/psycopg 导入成本)。"""
    from monitoring import workflow_event_store as _store

    return _store


def _default_link_store() -> Any:
    """默认关联存储 = ``monitoring.workflow_chat_link_store`` 模块级函数
    (延迟 import:未启用会话关联的调用方不承担 db/psycopg 导入成本)。"""
    from monitoring import workflow_chat_link_store as _links

    return _links


def _chat_link_upsert_running(
    link_store: Any, chat_session_id: str, thread_id: str, run_id: str
) -> None:
    """会话关联 upsert(执行设计 §4.4 第 3/5 条):产生首个运行事件前建立关联。

    任何失败只记明确 ERROR 日志(含 chat_session_id / thread_id / 目标状态),
    绝不阻塞工作流主流程(§10.4 任务 2.6 降级)。
    """
    try:
        link_store.upsert_running(chat_session_id, thread_id, run_id)
    except Exception as e:  # noqa: BLE001 — store 自身已降级,这里兜自定义 store
        logger.error(
            "[workflow_runner] 会话关联 upsert 失败"
            "(chat_session=%s thread=%s status=running): %s: %s",
            chat_session_id, thread_id, type(e).__name__, e,
        )


def _chat_link_update(
    link_store: Any,
    chat_session_id: str,
    status: str,
    thread_id: str,
    run_id: str,
) -> None:
    """关联状态流转(§4.4 第 4 条);失败降级同上,不阻塞运行。"""
    try:
        link_store.update_status(
            chat_session_id, status, thread_id=thread_id, run_id=run_id
        )
    except Exception as e:  # noqa: BLE001 — 同上
        logger.error(
            "[workflow_runner] 会话关联状态更新失败"
            "(chat_session=%s thread=%s status=%s): %s: %s",
            chat_session_id, thread_id, status, type(e).__name__, e,
        )


async def run_with_events(
    graph: Any,
    initial_state: Any,
    config: dict[str, Any],
    *,
    run_id: Optional[str] = None,
    on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    store: Any = None,
    link_store: Optional[Any] = None,
    stream_mode: tuple[str, ...] = DEFAULT_STREAM_MODE,
) -> dict[str, Any]:
    """统一执行入口:astream 消费 + 标准事件发布;返回最终 state values。

    Args:
        graph: ``build_graph(...)`` 返回的编译图(checkpointer 必须已注入)。
        initial_state: 初始 state dict / ``Command(resume=...)`` / ``None``
            (后两者 = 恢复运行,首事件为 ``run.resumed``)。
        config: LangGraph config,``configurable.thread_id`` 必填 ——
            事件以 thread_id 为主关联键(设计 §6.2-D)。
        run_id: 业务运行标识;None 时自动生成(:func:`run_id_new`)。
        on_event: 进程内订阅者(广播器加速层),每事件先落库再回调;
            回调抛异常只记 warning,不影响运行。
        store: 事件存储对象(需有 ``append_event(event) -> int|None``);
            None 用默认 PG 事件表。注入假存储可让单测脱离 Postgres。
        link_store: 会话—工作流关联存储(需有 ``upsert_running(csid, tid, rid)``
            与 ``update_status(csid, status, *, thread_id, run_id)``);
            None 用默认 ``monitoring.workflow_chat_link_store``(执行设计 §4.4)。
            注入假存储可让单测脱离 Postgres;关联写失败一律降级不阻塞运行。
        stream_mode: 流模式;默认含 custom(§7.3 规则 7 依赖)。

    Returns:
        最终 state values(``aget_state(config).values``,与 ``ainvoke`` 返回
        语义一致;异常时不返回 —— 原样 re-raise,CLI 退出码行为不变)。

    Raises:
        ValueError: ``config.configurable.thread_id`` 缺失。
        Exception: 图执行异常(先发 ``run.failed`` 再抛出)。
    """
    cfg = config or {}
    thread_id = ((cfg.get("configurable") or {}).get("thread_id")) or ""
    if not thread_id:
        raise ValueError(
            "run_with_events 需要 config.configurable.thread_id(事件按 thread_id 关联)"
        )
    run_id = run_id or run_id_new()
    if store is None:
        store = _default_store()
    if link_store is None:
        link_store = _default_link_store()

    def emit(
        event_type: str,
        *,
        node_id: Optional[str] = None,
        attempt: int = 1,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        event = make_event(
            event_type,
            thread_id=thread_id,
            node_id=node_id,
            attempt=attempt,
            run_id=run_id,
            payload=payload,
        )
        try:
            event_id = store.append_event(event)
        except Exception as e:  # noqa: BLE001 — 存储异常不拖垮运行(store 自身已降级,这里是自定义 store 兜底)
            logger.warning(
                "事件存储 append 失败(%s: %s);本事件不落库", type(e).__name__, e
            )
        else:
            if event_id is not None:
                event["id"] = event_id
        if on_event is not None:
            try:
                on_event(event)
            except Exception as e:  # noqa: BLE001 — 订阅者异常不拖垮运行
                logger.warning(
                    "事件订阅者回调失败(%s: %s)", type(e).__name__, e
                )
        # 会话关联状态流转(执行设计 §4.4 第 4 条;只挂钩 run 级事件,
        # node.waiting_input 不挂钩 —— 见 _CHAT_LINK_STATUS_BY_RUN_EVENT 注释)
        link_status = _CHAT_LINK_STATUS_BY_RUN_EVENT.get(event_type)
        if link_status and chat_session_id:
            _chat_link_update(link_store, chat_session_id, link_status, thread_id, run_id)

    # run.started / run.resumed(§7.1:恢复运行 = Command(resume=...) 或 None 输入)
    from langgraph.types import Command  # 延迟 import:模块顶层保持无 langgraph 依赖

    is_resume = initial_state is None or isinstance(initial_state, Command)

    # 会话—工作流关联(执行设计 §4.4):首次执行从 initial_state 读;恢复执行
    # 从 checkpoint State 读。旧 checkpoint / 旧调用无该键 → None → 不建关联,
    # 行为与迁移前完全一致(§10.3 红线:统一 .get() 访问)。
    chat_session_id: Optional[str] = None
    if not is_resume and isinstance(initial_state, dict):
        chat_session_id = (
            str(initial_state.get("chat_session_id") or "").strip() or None
        )

    resumed_nodes: set[str] = set()
    previous_attempts: dict[str, int] = {}
    if is_resume:
        # 跨调用 resume:adapter 每 run 新建,"同一 task id 二次 start"的内存
        # 状态不可跨调用 —— 挂起任务改从**运行前快照**取(§4.6 契约 4);
        # attempt 历史从事件存储快照续接(缺失时从 1 起,不阻塞运行)。
        try:
            pre_snapshot = await graph.aget_state(cfg)
            resumed_nodes = {
                str(t.name)
                for t in (getattr(pre_snapshot, "tasks", None) or ())
                if getattr(t, "interrupts", None)
            }
            # §4.4 第 2 条:恢复执行从 checkpoint State 读取 chat_session_id
            chat_session_id = (
                str(
                    (getattr(pre_snapshot, "values", None) or {}).get(
                        "chat_session_id"
                    )
                    or ""
                ).strip()
                or None
            )
        except Exception as e:  # noqa: BLE001 — 判定降级,只影响 resumed 归因
            logger.warning("resume 前置快照获取失败(%s: %s)", type(e).__name__, e)
        snapshot_getter = getattr(store, "latest_snapshot", None)
        if callable(snapshot_getter):
            try:
                previous_attempts = dict(
                    (snapshot_getter(thread_id) or {}).get("node_attempts") or {}
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("resume attempt 历史读取失败(%s: %s)", type(e).__name__, e)

    if chat_session_id:
        # §4.4 第 3 条:产生首个运行事件前建立 / 刷新关联(running + run_id)
        _chat_link_upsert_running(link_store, chat_session_id, thread_id, run_id)

    adapter = WorkflowStreamAdapter(
        emit,
        conditional_edges=_conditional_edges_of(graph),
        resumed_nodes=frozenset(resumed_nodes),
        previous_attempts=previous_attempts,
    )
    emit(
        EVENT_RUN_RESUMED if is_resume else EVENT_RUN_STARTED,
        payload={
            "reason": "resume" if is_resume else "start",
            "detail": {"stream_mode": list(stream_mode), "version": STREAM_VERSION},
        },
    )

    try:
        async for chunk in graph.astream(
            initial_state,
            config=cfg,
            stream_mode=list(stream_mode),
            version=STREAM_VERSION,
        ):
            if not isinstance(chunk, dict):
                continue
            chunk_type = chunk.get("type")
            data = chunk.get("data")
            if chunk_type == "tasks" and isinstance(data, dict):
                if _is_task_end(data):
                    adapter.handle_task_end(data)
                else:
                    adapter.handle_task_start(data)
            elif chunk_type == "custom":
                adapter.handle_custom(data)
            # updates:与 tasks.end 双现(§4.6 契约 3/7),以 tasks 为准,忽略
    except Exception as e:
        emit(
            EVENT_RUN_FAILED,
            payload={"error": sanitize_exception(e), "reason": "graph execution raised"},
        )
        raise

    # 终态判定:interrupt 挂起时 run.interrupted 已在 task end 发过;
    # 走到终点才发 run.completed(§7.1)。
    suspended = False
    values: dict[str, Any] = {}
    try:
        snapshot = await graph.aget_state(cfg)
        values = dict(getattr(snapshot, "values", None) or {})
        suspended = bool(getattr(snapshot, "interrupts", None)) or bool(
            getattr(snapshot, "next", None)
        )
    except Exception as e:  # noqa: BLE001 — 终态查询失败不影响运行结果
        logger.warning("aget_state 终态判定失败(%s: %s)", type(e).__name__, e)
    if not suspended:
        emit(
            EVENT_RUN_COMPLETED,
            payload={"detail": {"node_attempts": adapter.node_attempt_counts}},
        )
    return values
