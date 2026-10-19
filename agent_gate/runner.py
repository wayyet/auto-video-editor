"""runner:在独立 worker 进程里跑 LangGraph 图;以及拉起 / 恢复 worker 的父进程逻辑。

worker 的状态写在 <gate_root>/status.json:
    starting → running → waiting_human | completed | failed
心跳每 HEARTBEAT_S 秒更新一次;心跳过期且状态仍是 running,status 会判定为 crashed。
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from agent_gate import service
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


def checkpoint_path(thread_id: str) -> Path:
    """这个 thread 的 checkpoint 落盘位置。

    解析顺序:``AGENT_GATE_CKPT_DIR`` → ``TOY_CKPT_DIR``(沙箱玩具图)→ 仓库
    ``config.CHECKPOINTER_DB_DIR`` → ``<cwd>/checkpoints``。
    """
    safe_thread_id(thread_id)
    for env in ("AGENT_GATE_CKPT_DIR", "TOY_CKPT_DIR"):
        d = os.environ.get(env)
        if d:
            return Path(d) / f"{thread_id}.sqlite"
    try:
        from config import CHECKPOINTER_DB_DIR

        return Path(CHECKPOINTER_DB_DIR) / f"{thread_id}.sqlite"
    except Exception:  # noqa: BLE001 — 仓库不在 sys.path 上(纯 kit 场景)
        return Path("checkpoints") / f"{thread_id}.sqlite"


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

    仓库自带的 resume_all_pending 是 async(用 aget_state / ainvoke),
    而开发默认的 SqliteSaver 不支持异步方法(沙箱实测抛 NotImplementedError),
    所以 SQLite 路径用本函数;生产的 Postgres 路径继续用原函数。
    匹配规则与原函数一致:按 payload["checkpoint"] 匹配;只有 1 个挂起时可用 fallback_key。
    """
    from langgraph.types import Command

    snap = graph.get_state(config)
    interrupts = list(snap.interrupts or [])
    if not interrupts:
        raise ValueError("无挂起可恢复:graph state 中没有 interrupts")
    resume_map: dict[str, Any] = {}
    for i in interrupts:
        cp = (i.value or {}).get("checkpoint") if isinstance(i.value, dict) else None
        if cp in values_by_checkpoint:
            resume_map[i.id] = values_by_checkpoint[cp]
        elif fallback_key in values_by_checkpoint and len(interrupts) == 1:
            resume_map[i.id] = values_by_checkpoint[fallback_key]
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
    """命中生产环境就返回原因,否则 None(F6 护栏)。

    本方案只验收 SQLite 本地开发路径。Postgres 异步 worker 不在范围内,
    所以这里**显式拒绝启动**而不是静默退回 SQLite。
    """
    if os.environ.get("WORKFLOW_ENV") == "production":
        return "WORKFLOW_ENV=production"
    try:
        from config import resolve_checkpointer_backend

        backend = resolve_checkpointer_backend()
    except Exception:  # noqa: BLE001 — 仓库不在 sys.path 上就只看 env
        backend = os.environ.get("CHECKPOINTER_BACKEND", "sqlite")
    if str(backend).strip().lower() == "postgres":
        return f"checkpointer backend = {backend}"
    return None


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
        graph = load_callable(graph_factory)(
            thread_id=thread_id, start_heartbeat_thread=False, run_preflight=require_preflight
        )
        cfg = {"configurable": {"thread_id": thread_id}}
        mb.log_event("worker_started", mode=("resume" if resume_values is not None
                                             else "start" if initial_state is not None else "continue"))
        w.set(STATE_RUNNING)
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
        elif snap.next:
            w.set(STATE_FAILED, error={"type": "Incomplete",
                                       "message": f"图没有结束,但也没有挂起;next={list(snap.next)}"})
            return 1
        else:
            w.set(STATE_COMPLETED, final_keys=sorted((out or {}).keys()))
            mb.log_event("completed")
        return 0
    except BaseException as e:  # noqa: BLE001 — 任何失败都要落盘,方便 Agent 汇报
        w.set(STATE_FAILED, error={"type": type(e).__name__, "message": str(e)[:2000],
                                   "traceback": traceback.format_exc()[-4000:]})
        mb.log_event("worker_failed", error=f"{type(e).__name__}: {e}"[:500])
        return 1
    finally:
        stop.set()
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
    ``no_checkpoint``   mode=continue_after_crash,但检查点文件不存在
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
            has_checkpoint = checkpoint_path(thread_id).exists()
        if not has_checkpoint:
            return {"ok": False, "code": "no_checkpoint", "status": dict(cur),
                    "message": "没有找到这个 thread 的检查点文件,无法从崩溃处继续。"
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
