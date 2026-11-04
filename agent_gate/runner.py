"""runner:在独立 worker 进程里跑 LangGraph 图;以及拉起 / 恢复 worker 的父进程逻辑。

worker 的状态写在 <gate_root>/status.json:
    starting → running → waiting_human | completed | failed
心跳每 HEARTBEAT_S 秒更新一次;心跳过期且状态仍是 running,status 会判定为 crashed。
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from agent_gate import lease, service
from agent_gate.agent_bin import augment_path
from agent_gate.mailbox import Mailbox
from agent_gate.protocol import (
    HEARTBEAT_S,
    STATE_COMPLETED,
    STATE_CRASHED,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_STARTING,
    STATE_WAITING_HUMAN,
    utc_now,
)
from agent_gate.safety import (
    UnsafeIdentifier,
    UnsafePath,
    safe_draft_dir,
    safe_gate_root,
    safe_thread_id,
)

# 统一 DSN 解析层(阶段 B-T1):主图与 worker 共用 ``db.postgres_config``。
# 本模块 re-export 既有公开名 —— ``from agent_gate.runner import
# resolve_postgres_target`` 等旧导入(scripts/verify_agent_gate_postgres.py、
# agent_gate/tests/test_postgres_target.py)不受影响。
from db.postgres_config import (
    DEFAULT_POSTGRES_URI,
    PostgresTarget,
    describe_postgres_target,
    resolve_postgres_target,
)
from agent_gate.resume_match import match_resume_values

_logger = logging.getLogger("agent_gate.runner")


def load_callable(spec: str) -> Callable[..., Any]:
    """'pkg.module:func' → 函数。"""
    mod, _, name = spec.partition(":")
    if not mod or not name:
        raise ValueError(f"需要 'module:function' 形式,拿到:{spec!r}")
    return getattr(importlib.import_module(mod), name)


def gate_root(outputs_root: Path | str, thread_id: str) -> Path:
    """<outputs_root>/<thread_id>/agent_gate。

    这是 CLI / MCP / drive 三个入口的共同路径,F1 的校验放在这里一处覆盖多入口。
    非法 thread_id 抛 :class:`agent_gate.safety.UnsafeIdentifier`,路径越界抛
    :class:`agent_gate.safety.UnsafePath`。
    """
    return safe_gate_root(outputs_root, thread_id)


def draft_dir(outputs_root: Path | str, thread_id: str) -> Path:
    """<outputs_root>/<thread_id>/drafts —— 本 thread 的剪映草稿目录。

    worker 启动时把这个目录写进 ``AUTO_VIDEO_EDITOR_DRAFT_DIR``,
    项目的 ``config.resolve_draft_dir()`` 就会用它,而不是回落到
    **所有 thread 共用的** ``<cwd>/drafts/default``。
    """
    return safe_draft_dir(outputs_root, thread_id)


def _isolate_draft_dir(outputs_root: Path | str, thread_id: str) -> tuple[Path, Optional[str], bool]:
    """把本 thread 的剪映草稿目录写进 ``AUTO_VIDEO_EDITOR_DRAFT_DIR``。

    项目的 ``config.resolve_draft_dir()`` 解析顺序是:
    ``state["_draft_dir_override"]`` → env ``AUTO_VIDEO_EDITOR_DRAFT_DIR``
    → ``<cwd>/drafts/default``。

    最后那档是**所有 thread 共用**的:两个并发运行会互相覆盖草稿,
    草稿也会落在 ``outputs/`` 之外(项目自己的 ``outputs/{job_id}/`` 隔离
    就失效了)。所以在跑图之前显式指到 ``<outputs_root>/<thread_id>/drafts``。

    操作员已经显式设了 ``AUTO_VIDEO_EDITOR_DRAFT_DIR`` 时**不覆盖** ——
    那是人的显式意图,agent_gate 不越权。

    返回 ``(实际生效的目录, 旧值, 是否是本次设的)``。
    旧值是 None 表示原来没设过;调用方负责按 ``是否本次设的`` 决定还原方式,
    否则会把操作员的显式设置误删。
    """
    prev = os.environ.get("AUTO_VIDEO_EDITOR_DRAFT_DIR")
    if prev:
        return Path(prev), prev, False
    d = draft_dir(outputs_root, thread_id)
    os.environ["AUTO_VIDEO_EDITOR_DRAFT_DIR"] = str(d)
    return d, None, True


# ---------------------------------------------------------------------------
# 检查点后端(PostgreSQL 统一改造 §4.7)
# ---------------------------------------------------------------------------
#: ``AGENT_GATE_CKPT_BACKEND`` 可选值:
#: - ``postgres``:同步 ``langgraph.checkpoint.postgres.PostgresSaver``(默认,唯一持久化后端)
#: - ``memory``  :``InMemorySaver``(沙箱 / 单测;进程内,无持久化)
CKPT_BACKEND_ENV = "AGENT_GATE_CKPT_BACKEND"

_CKPT_BACKENDS = ("postgres", "memory")


# PostgresTarget / resolve_postgres_target() / describe_postgres_target() 已上移到
# 统一解析层 db.postgres_config(阶段 B-T1),本模块经顶部 import re-export。
# 解析语义(APP_ENV=production 拒绝测试库变量 / 空值防御)以该模块为准。


def postgres_uri() -> str:
    """worker 用的 Postgres 连接串(:func:`resolve_postgres_target` 的薄封装)。

    保留返回 ``str`` 的旧签名 —— 既有调用点(``make_checkpointer`` /
    ``checkpoint_exists``)无需改动;需要 DSN 来源时直接用
    :func:`resolve_postgres_target`。
    """
    return resolve_postgres_target().uri


def checkpoint_backend() -> str:
    """当前 worker 用的 checkpointer 后端(``postgres`` / ``memory``)。

    默认 ``postgres`` —— 统一改造后 SQLite 后端已删除,没有"本地文件兜底"这条退路。
    """
    backend = os.environ.get(CKPT_BACKEND_ENV, "").strip().lower()
    if not backend:
        backend = "postgres"
    if backend not in _CKPT_BACKENDS:
        raise ValueError(
            f"{CKPT_BACKEND_ENV}={backend!r} 无效;可选: {' / '.join(_CKPT_BACKENDS)}"
        )
    return backend


def _configure_json_dumps() -> None:
    """让 psycopg3 写 JSONB 时不做 ``\\uXXXX`` 转义。

    本机 Postgres 库编码是 ``SQL_ASCII``:PG 写 JSONB 时要把 unicode 转义翻译成
    库编码,而 ``SQL_ASCII`` 表示不了非 ASCII 字符 ⇒ ``UntranslatableCharacter``。
    改成 ``ensure_ascii=False`` 后中文以原始 UTF-8 字节写入,不需要翻译转义。

    ⚠️ ``set_json_dumps`` 是**进程级全局**设置 ⇒ LangGraph saver 内部用的
    ``Jsonb(...)`` 也吃这一套(踩坑记录:agent_gate worker 写中文 checkpoint
    时报 ``UntranslatableCharacter ... "\\u4e24..."``)。

    优先复用仓库的 ``db.connection.configure_json_dumps``;纯 kit 场景(仓库不在
    sys.path 上)退回到本地实现。
    """
    try:
        from db.connection import configure_json_dumps

        configure_json_dumps()
        return
    except Exception:  # noqa: BLE001 — 纯 kit 场景
        pass
    import json

    from psycopg.types.json import set_json_dumps

    set_json_dumps(lambda obj, context=None: json.dumps(obj, ensure_ascii=False))


def make_checkpointer(thread_id: str = "default", mailbox: Optional[Mailbox] = None):
    """构造 worker 用的 checkpointer(调用方负责释放,见 ``close_checkpointer``)。

    worker 是**同步**进程(``graph.invoke``),所以用**同步**的
    ``langgraph.checkpoint.postgres.PostgresSaver``;
    ``AsyncPostgresSaver`` 只实现异步接口,同步 ``invoke`` 会抛 ``NotImplementedError``。

    Args:
        thread_id: 工作流线程 ID;仅用于日志 / 错误信息。
        mailbox: worker 信箱;提供时在连接**之前**落 ``postgres_target`` 脱敏事件
            (阶段 B-T6,事件名/字段与原 run_worker 内联版本一致)。
            脚本 / 单测等无信箱调用方可省略,此时只走 logging。

    Returns:
        同步 saver 实例(PostgresSaver 或 InMemorySaver)。
        PostgresSaver 上挂了 ``_agent_gate_conn`` 属性,供 ``close_checkpointer`` 释放。

    Raises:
        RuntimeError: Postgres 不可用 / 表未建 / 包未安装。
        ValueError: ``AGENT_GATE_CKPT_BACKEND`` 取值非法。
    """
    backend = checkpoint_backend()
    if backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()

    uri = postgres_uri()
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg import Connection
        from psycopg.conninfo import make_conninfo
        from psycopg.rows import dict_row
    except ImportError as e:  # pragma: no cover — 依赖缺失
        raise RuntimeError(
            "agent_gate 需要 langgraph-checkpoint-postgres 与 psycopg[binary,pool]:"
            "pip install langgraph-checkpoint-postgres 'psycopg[binary,pool]'"
        ) from e

    # B-T6:连接**之前**落一次脱敏 DSN 诊断(来源/主机/端口/库/用户/是否带密码)。
    # 诊断挪进连接路径本身 —— 任何直接调用 make_checkpointer 的入口(脚本/单测)
    # 也先有"到底用了哪个来源、哪个目标"可查;事件里没有密码,也没有 URI 原文。
    if mailbox is not None:
        mailbox.log_event("postgres_target", **describe_postgres_target())
    else:
        _logger.info("make_checkpointer postgres target: %s", describe_postgres_target())

    # B-T5:连接超时 —— Postgres 不可达时快速失败,worker 不能长时间停在
    # starting。复用 db/connection.py 的统一策略(默认 10s,可用环境变量
    # ``POSTGRES_CONNECT_TIMEOUT`` 覆盖);纯 kit 场景退回同款默认值。
    # 超时用 ``psycopg.conninfo.make_conninfo`` 合并进 conninfo,**不拼接字符串**。
    try:
        from db.connection import _connect_timeout_s

        connect_timeout = _connect_timeout_s()
    except Exception:  # noqa: BLE001 — 纯 kit 场景
        connect_timeout = 10  # 与 db.connection.DEFAULT_CONNECT_TIMEOUT_S 一致

    # 写 JSONB 的全局 dumps 设置(见 ``_configure_json_dumps``)
    _configure_json_dumps()

    # 用**单条连接**而不是连接池:与 ``PostgresSaver.from_conn_string`` 同款。
    # 理由:worker 是单线程跑图的(sync invoke),连接池的 worker 线程在进程退出时
    # 会打 "couldn't stop thread" 噪声,单连接没有这个问题。
    try:
        conn = Connection.connect(
            make_conninfo(uri, connect_timeout=connect_timeout),
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
            client_encoding="utf-8",
        )
    except Exception as e:
        raise RuntimeError(
            f"Postgres checkpointer 连接失败(thread_id={thread_id}): {e}"
        ) from e
    try:
        saver = PostgresSaver(conn)
        saver.setup()
    except Exception as e:
        conn.close()
        raise RuntimeError(
            f"Postgres checkpointer 初始化失败(thread_id={thread_id}): {e}"
        ) from e
    # 把连接挂在 saver 上,让调用方能释放它
    setattr(saver, "_agent_gate_conn", conn)
    return saver


def close_checkpointer(saver: Any) -> None:
    """释放 checkpointer 持有的数据库连接(如果有)。"""
    conn = getattr(saver, "_agent_gate_conn", None)
    if conn is None:
        return
    try:
        conn.close()
    except Exception:  # noqa: BLE001 — 释放失败不该盖掉真正的错误
        pass


@contextmanager
def checkpointer_session(thread_id: str, mailbox: Optional[Mailbox] = None):
    """构造 worker 用的 checkpointer,退出时释放连接池。

    语义同 ``make_checkpointer``,但保证连接池被回收。

    Raises:
        RuntimeError: Postgres 不可用 / 表未建 / 包未安装。
        ValueError: ``AGENT_GATE_CKPT_BACKEND`` 取值非法。
    """
    saver = make_checkpointer(thread_id, mailbox=mailbox)
    try:
        yield saver
    finally:
        close_checkpointer(saver)


def _scrub_dsn_text(text: str, uri: str) -> str:
    """把文本里可能出现的 DSN 密码子串替换成 ``***``(B-T7 诊断脱敏)。

    psycopg 的连接错误不会回显密码,但按"不信任异常文本"处理:
    只要解析出的 URI 带密码,就把该子串从任何要落日志的文本中抹掉。
    """
    password = urlsplit(uri).password
    if password:
        text = text.replace(password, "***")
    return text


def checkpoint_exists(thread_id: str) -> Optional[bool]:
    """这个 thread 在 Postgres 里有没有 checkpoint(**三态**,阶段 B-T7)。

    统一改造后 checkpoint 不再是本地文件,而是 ``checkpoints`` 表里的行
    (方案 §4.7)。查两套 thread_id:文本版 + ``\\x<hex>`` 字面量版 ——
    后者是历史遗留(bytes 传进 saver 造成的,见方案 §3.1 M2)。

    Returns:
        ``True``  —— 数据库可连,存在 checkpoint;
        ``False`` —— 数据库可连,确实不存在;
        ``None``  —— 查询本身失败(数据库不可达等)。调用方**必须**按
            ``database_unavailable`` 处理:仍然 fail-closed(不许盲目续跑),
            但错误语义不再是"无 checkpoint",避免把"数据库挂了"误导成
            "换新 thread_id 重跑"(执行设计 §6.5)。

    查询失败会先落一条脱敏诊断日志(密码打码),再返回 ``None``。
    """
    safe_thread_id(thread_id)
    target = resolve_postgres_target()
    hex_literal = "\\x" + thread_id.encode("utf-8").hex()
    try:
        import psycopg

        with psycopg.connect(target.uri, connect_timeout=5, client_encoding="utf-8") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM checkpoints"
                    " WHERE thread_id = %s OR thread_id = %s LIMIT 1",
                    (thread_id, hex_literal),
                )
                return cur.fetchone() is not None
    except Exception as e:  # noqa: BLE001 — 查询失败 ≠ 无 checkpoint:返回 None(fail-closed)
        diag = describe_postgres_target(target)
        _logger.warning(
            "checkpoint_exists 查询失败(thread_id=%s source=%s host=%s port=%s db=%s): %s",
            thread_id, diag.get("source"), diag.get("host"), diag.get("port"),
            diag.get("dbname"),
            _scrub_dsn_text(f"{type(e).__name__}: {e}", target.uri)[:500],
        )
        return None


# ------------------------------------------------------------------ 恢复(同步版)
def describe_interrupts(snapshot: Any) -> list[dict[str, Any]]:
    out = []
    for i in list(getattr(snapshot, "interrupts", None) or []):
        v = i.value if isinstance(i.value, dict) else {"value": i.value}
        out.append({
            "id": i.id,
            "checkpoint": v.get("checkpoint"),
            "step": v.get("step"),
            "instructions": v.get("instructions"),
            "payload": v,
        })
    return out


def resume_pending_sync(
    graph: Any,
    config: dict,
    values_by_checkpoint: Mapping[str, Any],
    *,
    fallback_key: str = "default",
) -> Any:
    """``resume_all_pending`` 的同步版。

    agent_gate 的 worker 是**同步进程**(``graph.invoke`` / ``graph.get_state``),
    所以走同步接口。统一改造后同步 saver 是
    ``langgraph.checkpoint.postgres.PostgresSaver``(实现了同步 get/put);
    ``AsyncPostgresSaver`` 只支持异步方法,同步调用会抛 ``NotImplementedError``。

    匹配规则与原函数一致:按 payload["checkpoint"] 匹配;只有 1 个挂起时可用 fallback_key。
    """
    from langgraph.types import Command

    snap = graph.get_state(config)
    interrupts = list(snap.interrupts or [])
    if not interrupts:
        raise ValueError("无挂起可恢复:graph state 中没有 interrupts")
    resume_map, _unmatched = match_resume_values(
        interrupts, values_by_checkpoint, fallback_key=fallback_key
    )
    if not resume_map:
        raise ValueError(
            f"没有匹配的挂起。可用 checkpoint={[d['checkpoint'] for d in describe_interrupts(snap)]},"
            f"传入的 keys={list(values_by_checkpoint)}"
        )
    return graph.invoke(Command(resume=resume_map), config)


# ------------------------------------------------------------------ worker
class _StatusWriter:
    """内存里保存最近一次状态,心跳线程和主线程共用一把锁,避免旧状态覆盖新状态。"""

    def __init__(self, mb: Mailbox, base: dict[str, Any]) -> None:
        self._mb = mb
        self._base = base
        self._lock = threading.Lock()
        self._cur: dict[str, Any] = {}

    def set(self, state: str, **extra: Any) -> None:
        with self._lock:
            self._cur = {**self._base, "state": state, "heartbeat_at": utc_now(), **extra}
            self._mb.write_status(self._cur)

    def beat(self) -> None:
        with self._lock:
            if self._cur.get("state") in (STATE_STARTING, STATE_RUNNING):
                self._cur["heartbeat_at"] = utc_now()
                self._mb.write_status(self._cur)


def install_client(client: Any) -> None:
    """把 client 设为仓库的默认 LLM 客户端。"""
    from storyline_capabilities.vlm_client import set_default_client

    set_default_client(client)


def production_blocker() -> Optional[str]:
    """返回"当前环境不能跑 worker"的原因;None = 可以启动(F6 护栏)。

    PostgreSQL 统一改造后(方案 §4.7)的语义变化:

    - **删掉**原来"``WORKFLOW_ENV=production`` 或 backend=postgres 就拒绝启动"的规则。
      原文注释写的是"本方案只验收 SQLite 本地开发路径" —— 而 SQLite 后端已被删除,
      Postgres 现在是唯一后端;继续拦着等于让 agent 链路永远起不来(方案 §3.2 A1)。
    - **保留正向校验**:运行时后端必须落在 Postgres 上。残留的旧
      ``CHECKPOINTER_BACKEND`` 环境变量(如 ``=sqlite``)直接拒绝,
      避免有人以为还在用本地文件存储。
    """
    stale = os.environ.get("CHECKPOINTER_BACKEND", "").strip().lower()
    if stale and stale != "postgres":
        return (
            f"CHECKPOINTER_BACKEND={stale} 已废弃 —— PostgreSQL 统一改造后运行时"
            "只有 Postgres 后端(SQLite 分支已删除);请 unset 该环境变量。"
        )
    backend = os.environ.get(CKPT_BACKEND_ENV, "").strip().lower()
    if backend and backend not in _CKPT_BACKENDS:
        return f"{CKPT_BACKEND_ENV}={backend} 无效(可选: {' / '.join(_CKPT_BACKENDS)})"
    return None


def _emit_run_event(
    thread_id: str,
    event_type: str,
    run_id: str,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """运行级事件写入共享 PG 事件表(阶段3 底线接入,设计 §6.2-B)。

    worker 是**独立进程**且保持同步 ``invoke`` 语义不变(§11.9:节点级 stream
    改造仅在契约测试证明终态一致后启用)—— 运行级事件是页面可见性的底线。
    事件走共享 PG 事件表(§11.4:跨进程不可能靠内存队列);延迟 import 保持
    "纯 kit 场景"(仓库不在 sys.path)可用;任何失败只记 debug —— 观测信号
    不允许影响 worker 状态机(status.json 挂点不受影响)。

    **写入在单一后台线程里执行(FIFO 保序)**:run.* 事件是可观测性数据,
    同步 PG 往返不该加进 worker 执行路径 —— 实测(阶段6)同步写会放大
    ``test_images.py`` 里既有的取任务时序竞态窗口(该测试本就偶发,见执行
    记录 §6)。``drain_run_events`` 在 worker 退出前最多等 5s 把队列排空,
    避免进程退出杀掉守护线程丢尾事件。
    """
    global _run_event_queue
    try:
        with _run_event_queue_lock:
            if _run_event_queue is None:
                _run_event_queue = queue.Queue()
                threading.Thread(
                    target=_run_event_writer_loop,
                    args=(_run_event_queue,),
                    daemon=True,
                    name="agent-gate-run-events",
                ).start()

        def _write() -> None:
            from monitoring.workflow_events import make_event
            from monitoring.workflow_event_store import append_event

            append_event(
                make_event(
                    event_type, thread_id=thread_id, run_id=run_id, payload=payload
                )
            )

        _run_event_queue.put(_write)
    except Exception as e:  # noqa: BLE001 — 观测信号,不抬高 worker 失败面
        _logger.debug("run event %s enqueue failed: %s", event_type, e)


#: 运行级事件的后台写队列(懒建;None = 尚未用过)
_run_event_queue: Optional["queue.Queue[Callable[[], None]]"] = None
_run_event_queue_lock = threading.Lock()


def _run_event_writer_loop(q: "queue.Queue[Callable[[], None]]") -> None:
    """单写线程:严格按入队顺序落库,单条失败只记 debug。"""
    while True:
        write = q.get()
        try:
            write()
        except Exception as e:  # noqa: BLE001 — 观测信号,吞掉
            _logger.debug("run event write failed: %s", e)
        finally:
            q.task_done()


def drain_run_events(timeout_s: float = 5.0) -> None:
    """worker 退出前排空运行级事件队列(最多等 ``timeout_s`` 秒)。

    只等"已入队"的事件;PG 不可达时单条写最多拖一个连接超时,超时即放弃
    (观测信号允许丢失,worker 状态机早已落盘 status.json)。
    """
    with _run_event_queue_lock:
        q = _run_event_queue
    if q is None:
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if q.unfinished_tasks == 0:
            return
        time.sleep(0.05)


def run_worker(
    *,
    thread_id: str,
    outputs_root: Path | str,
    graph_factory: str,
    initial_state: Optional[dict[str, Any]] = None,
    resume_values: Optional[dict[str, Any]] = None,
    backend: str = "gate",
    client_factory: Optional[Callable[[Mailbox], Any]] = None,
    require_preflight: bool = False,
) -> int:
    """返回进程退出码:0=正常停下(挂起/完成),1=失败,3=生产环境被护栏拦下。"""
    safe_thread_id(thread_id)
    mb = Mailbox(gate_root(outputs_root, thread_id))
    # 阶段3:本次 worker 执行的业务运行标识(run.* 事件关联键;try 外定义,
    # 任何早期失败也能发 run.failed)
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    _draft_dir, _prev_draft_env, _draft_env_set_by_us = _isolate_draft_dir(outputs_root, thread_id)
    w = _StatusWriter(mb, {"thread_id": thread_id, "pid": os.getpid(),
                           "started_at": utc_now(), "interrupts": [], "error": None})
    w.set(STATE_STARTING)
    stop = threading.Event()

    def beat_loop() -> None:
        while not stop.wait(HEARTBEAT_S):
            w.beat()

    threading.Thread(target=beat_loop, daemon=True, name="agent-gate-heartbeat").start()
    try:
        blocker = production_blocker()
        if blocker:
            mb.log_event("production_unsupported", reason=blocker)
            w.set(STATE_FAILED, error={"type": "ProductionUnsupported", "message": blocker})
            return 3
        reopened = mb.reset_errors()
        if reopened:
            mb.log_event("errors_reset", count=reopened)
        from agent_gate.factory import build_client

        client = client_factory(mb) if client_factory else build_client(backend, mb)
        install_client(client)
        # checkpointer 由 worker 自己建并注入(方案 §4.7):仓库的
        # ``graph.build_graph()`` 已改为"未注入就报错",不再自动兜底。
        # worker 眼中的最终 DSN 脱敏诊断(postgres_target 事件)已挪进
        # make_checkpointer 连接路径本身(B-T6);传 mb 让它落 events.jsonl,
        # 事件名/字段与原内联版本一致,连接失败时才有"哪个来源、哪个目标"可查。
        with checkpointer_session(thread_id, mailbox=mb) as saver:
            graph = load_callable(graph_factory)(
                checkpointer=saver,
                thread_id=thread_id,
                start_heartbeat_thread=False,
                run_preflight=require_preflight,
            )
            cfg = {"configurable": {"thread_id": thread_id}}
            run_mode = ("resume" if resume_values is not None
                        else "start" if initial_state is not None else "continue")
            mb.log_event("worker_started", mode=run_mode)
            w.set(STATE_RUNNING)
            # 阶段3:运行级事件(设计 §6.2-B 底线)—— resume 用 run.resumed,
            # 首跑 / 崩溃续跑用 run.started(reason 区分)。
            if resume_values is not None:
                _emit_run_event(thread_id, "run.resumed", run_id, {"reason": "resume"})
            else:
                _emit_run_event(thread_id, "run.started", run_id,
                                {"reason": run_mode})
            if resume_values is not None:
                out = resume_pending_sync(graph, cfg, resume_values)
            elif initial_state is not None:
                out = graph.invoke(initial_state, cfg)
            else:  # 崩溃后从最近检查点继续
                out = graph.invoke(None, cfg)
            snap = graph.get_state(cfg)
            intr = describe_interrupts(snap)
        if intr:
            w.set(STATE_WAITING_HUMAN, interrupts=intr)
            mb.log_event("waiting_human", checkpoints=[i["checkpoint"] for i in intr])
            # interrupt 挂起不是失败(设计 §11.3);运行级事件如实报等待
            _emit_run_event(thread_id, "run.interrupted", run_id,
                            {"detail": {"checkpoints": [i["checkpoint"] for i in intr]}})
        elif snap.next:
            w.set(STATE_FAILED, error={"type": "Incomplete",
                                       "message": f"图没有结束,但也没有挂起;next={list(snap.next)}"})
            _emit_run_event(thread_id, "run.failed", run_id,
                            {"reason": "incomplete",
                             "detail": {"next": [str(n) for n in snap.next]}})
            return 1
        else:
            w.set(STATE_COMPLETED, final_keys=sorted((out or {}).keys()))
            mb.log_event("completed")
            _emit_run_event(thread_id, "run.completed", run_id,
                            {"detail": {"final_keys": sorted((out or {}).keys())}})
        return 0
    except BaseException as e:  # noqa: BLE001 — 任何失败都要落盘,方便 Agent 汇报
        w.set(STATE_FAILED, error={"type": type(e).__name__, "message": str(e)[:2000],
                                   "traceback": traceback.format_exc()[-4000:]})
        mb.log_event("worker_failed", error=f"{type(e).__name__}: {e}"[:500])
        _emit_run_event(thread_id, "run.failed", run_id,
                        {"error": {"type": type(e).__name__, "message": str(e)[:2000]}})
        return 1
    finally:
        stop.set()
        # 阶段3:退出前排空运行级事件队列(最多 5s),避免守护线程被进程
        # 退出杀掉丢尾事件(run.completed/run.failed)。
        try:
            drain_run_events()
        except Exception:  # noqa: BLE001 — 排空失败不影响退出码
            pass
        # 还原草稿目录环境变量:run_worker 也可能在测试进程里就地调用,
        # 不还原会污染同进程后续用例(以及同进程的第二个 thread)。
        # 只有本次确实是 agent_gate 设的才动 —— 操作员显式设的值要原样留下。
        if _draft_env_set_by_us:
            if _prev_draft_env is None:
                os.environ.pop("AUTO_VIDEO_EDITOR_DRAFT_DIR", None)
            else:
                os.environ["AUTO_VIDEO_EDITOR_DRAFT_DIR"] = _prev_draft_env


# ------------------------------------------------------------------ 父进程:拉起 worker
#: 允许的启动模式。三种互斥,语义互不重叠。
MODES = ("start", "continue_after_crash", "resume")


def check_mode(
    *,
    mode: str,
    cur: Mapping[str, Any],
    thread_id: str,
    has_checkpoint: Optional[bool] = None,
    resume_values: Optional[Mapping[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """按 F3 的状态机判断这次启动合不合法。合法返回 None,不合法返回拒绝体。

    ``cur`` 是 :func:`agent_gate.service.status_summary` 的结果(已含
    "心跳过期即 crashed" 的推断,所以崩掉的 worker 在这里读出来就是 ``crashed``)。

    拒绝码:

    ==================  ==========================================================
    ``already_used``    mode=start,但这个 thread 已经有 status 或 checkpoint
    ``already_running`` 正在 starting / running —— 保留的第 4 条独立检查
    ``not_restartable`` mode=continue_after_crash,但状态不是 crashed / failed
    ``no_checkpoint``   mode=continue_after_crash,但 Postgres 里(可连、可查)没有这个 thread 的检查点
    ``database_unavailable`` mode=continue_after_crash,但 checkpoint 查询本身失败
                        (数据库不可达;B-T7 三态化,fail-closed 不许盲目续跑)
    ``not_waiting_human`` mode=resume,但状态不是 waiting_human
    ``bad_checkpoint``  mode=resume,但传入的 checkpoint 不在挂起列表里
    ==================  ==========================================================
    """
    state = cur.get("worker_state")

    if mode not in MODES:
        return {"ok": False, "code": "bad_mode", "message": f"mode 只能是 {list(MODES)} 之一"}
    if state in (STATE_STARTING, STATE_RUNNING):
        return {"ok": False, "code": "already_running", "status": dict(cur)}

    if mode == "start":
        if state not in (None, "not_started") or has_checkpoint:
            return {"ok": False, "code": "already_used", "status": dict(cur),
                    "message": "这个 thread_id 已经跑过了。换一个新 ID 重跑,"
                               "或者用 mode=continue_after_crash / mode=resume 接着跑。"}
        return None

    if mode == "continue_after_crash":
        if state not in (STATE_CRASHED, STATE_FAILED):
            return {"ok": False, "code": "not_restartable", "status": dict(cur),
                    "message": f"当前状态是 {state!r},只有 crashed / failed 才可以从检查点继续。"}
        if has_checkpoint is None:
            has_checkpoint = checkpoint_exists(thread_id)
        if has_checkpoint is None:
            # B-T7 三态:查询失败(数据库不可达)≠ 无 checkpoint。
            # fail-closed 不许盲目续跑,但错误语义要准确,避免误导排障方向。
            return {"ok": False, "code": "database_unavailable", "status": dict(cur),
                    "message": "无法确认该 thread 是否有 checkpoint(Postgres 查询失败,"
                               "见 worker 侧 checkpoint_exists 诊断日志)。请先恢复数据库"
                               "连接再重试 continue_after_crash;不要新建 thread_id 盲目重跑。"}
        if not has_checkpoint:
            return {"ok": False, "code": "no_checkpoint", "status": dict(cur),
                    "message": "没有找到这个 thread 的检查点(Postgres 里没有记录),无法从崩溃处继续。"
                               "如果还没开始跑过,请改用 mode=start 和新的 thread_id。"}
        return None

    # mode == "resume"
    if state != STATE_WAITING_HUMAN:
        return {"ok": False, "code": "not_waiting_human", "status": dict(cur),
                "message": f"当前状态是 {state!r},只有 waiting_human 才需要人工放行。"}
    known = {i.get("checkpoint") for i in (cur.get("interrupts") or [])}
    asked = set((resume_values or {}).keys())
    if asked and not (asked & known):
        return {"ok": False, "code": "bad_checkpoint", "status": dict(cur),
                "message": f"传入的 checkpoint 不在当前挂起列表里。可用:{sorted(str(k) for k in known)}"}
    return None


def spawn_worker(
    *,
    thread_id: str,
    outputs_root: Path | str,
    graph_factory: str,
    mode: str,
    initial_state_file: Optional[Path] = None,
    resume_values: Optional[dict[str, Any]] = None,
    backend: str = "gate",
    cwd: Optional[Path] = None,
    python: str = sys.executable,
    require_preflight: bool = False,
    operator: Optional[str] = None,
) -> dict[str, Any]:
    try:
        safe_thread_id(thread_id)
        mb = Mailbox(gate_root(outputs_root, thread_id))
    except (UnsafeIdentifier, UnsafePath) as e:
        from agent_gate.safety import rejection

        return rejection(e)

    cur = service.status_summary(mb, thread_id=thread_id)
    if mode == "start" and initial_state_file is None and resume_values is None:
        return {"ok": False, "code": "need_input",
                "message": "mode=start 需要 initial_state_file(要么给 --video 生成,要么给 --initial-state-file)"}
    if mode == "resume" and not resume_values:
        return {"ok": False, "code": "need_input", "message": "mode=resume 需要 resume_values"}
    if mode == "continue_after_crash" and (initial_state_file is not None or resume_values is not None):
        return {"ok": False, "code": "bad_mode",
                "message": "mode=continue_after_crash 不能同时带 initial_state_file 或 resume_values"}

    bad = check_mode(mode=mode, cur=cur, thread_id=thread_id, resume_values=resume_values)
    if bad is not None:
        return bad

    # 操作员租约(阶段 D / 设计 §6.4):start / continue_after_crash 只做
    # **检查** —— 其他活操作员还持有租约时,不允许第二个消费者启动或接管
    # (稳定错误码 operator_already_claimed)。mode=resume 是人工放行,不拦。
    # 取得租约交给第一次真正的消费动作(gate_next_tasks / submit),
    # 这样 drive 模式(父进程拉起、agent 会话消费)不会被父进程身份挡住。
    if mode in ("start", "continue_after_crash"):
        claimed = lease.ensure_can_start(thread_id, operator=operator)
        if claimed is not None:
            mb.log_event("operator_lease_rejected", mode=mode, code=claimed.get("code"),
                         owner=claimed.get("owner"))
            return claimed

    # worker 在仓库根目录里启动:`python -m` 会把 cwd 放进 sys.path,`graph:build_graph` 才导入得到
    if cwd is None and os.environ.get("AGENT_GATE_REPO_ROOT"):
        cwd = Path(os.environ["AGENT_GATE_REPO_ROOT"])

    args = [python, "-m", "agent_gate", "worker", "--thread-id", thread_id,
            "--outputs-root", str(outputs_root), "--graph-factory", graph_factory,
            "--backend", backend]
    if require_preflight:
        args.append("--require-preflight")
    if initial_state_file is not None:
        args += ["--initial-state-file", str(initial_state_file)]
    if resume_values is not None:
        rf = mb.root / "resume.json"
        rf.write_text(json.dumps(resume_values, ensure_ascii=False), encoding="utf-8")
        args += ["--resume-file", str(rf)]

    # 先写 starting,这样紧接着的 status 不会误报 not_started
    mb.write_status({"thread_id": thread_id, "state": STATE_STARTING, "pid": None,
                     "started_at": utc_now(), "heartbeat_at": utc_now(),
                     "interrupts": [], "error": None})
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    # 子进程(worker)的 PATH 常常比交互式 shell 干净:少了 ffmpeg 目录,项目里
    # 那些 `cmd = ["ffmpeg", ...]` 就**静默失败**(抽不出帧 / 渲染报错,而不是
    # 明确说「找不到 ffmpeg」)。这里把解析到的工具目录显式补进 PATH。
    env["PATH"] = augment_path(env.get("PATH"), ("ffmpeg", "ffprobe"))
    kw: dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    with open(mb.root / "worker.log", "ab") as logf:
        p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                             cwd=str(cwd) if cwd else None, env=env, close_fds=True, **kw)
    return {"ok": True, "pid": p.pid, "gate_root": str(mb.root), "mode": mode,
            "log": str(mb.root / "worker.log")}
