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
    STATE_FAILED,
    STATE_RUNNING,
    STATE_STARTING,
    STATE_WAITING_HUMAN,
    utc_now,
)


def load_callable(spec: str) -> Callable[..., Any]:
    """'pkg.module:func' → 函数。"""
    mod, _, name = spec.partition(":")
    if not mod or not name:
        raise ValueError(f"需要 'module:function' 形式,拿到:{spec!r}")
    return getattr(importlib.import_module(mod), name)


def gate_root(outputs_root: Path | str, thread_id: str) -> Path:
    return Path(outputs_root) / thread_id / "agent_gate"


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


def run_worker(
    *,
    thread_id: str,
    outputs_root: Path | str,
    graph_factory: str,
    initial_state: Optional[dict[str, Any]] = None,
    resume_values: Optional[dict[str, Any]] = None,
    backend: str = "gate",
    client_factory: Optional[Callable[[Mailbox], Any]] = None,
) -> int:
    """返回进程退出码:0=正常停下(挂起/完成),1=失败。"""
    mb = Mailbox(gate_root(outputs_root, thread_id))
    w = _StatusWriter(mb, {"thread_id": thread_id, "pid": os.getpid(),
                           "started_at": utc_now(), "interrupts": [], "error": None})
    w.set(STATE_STARTING)
    stop = threading.Event()

    def beat_loop() -> None:
        while not stop.wait(HEARTBEAT_S):
            w.beat()

    threading.Thread(target=beat_loop, daemon=True, name="agent-gate-heartbeat").start()
    try:
        reopened = mb.reset_errors()
        if reopened:
            mb.log_event("errors_reset", count=reopened)
        from agent_gate.factory import build_client

        client = client_factory(mb) if client_factory else build_client(backend, mb)
        install_client(client)
        graph = load_callable(graph_factory)(
            thread_id=thread_id, start_heartbeat_thread=False, run_preflight=False
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


# ------------------------------------------------------------------ 父进程:拉起 worker
def spawn_worker(
    *,
    thread_id: str,
    outputs_root: Path | str,
    graph_factory: str,
    initial_state_file: Optional[Path] = None,
    resume_values: Optional[dict[str, Any]] = None,
    backend: str = "gate",
    cwd: Optional[Path] = None,
    python: str = sys.executable,
) -> dict[str, Any]:
    mb = Mailbox(gate_root(outputs_root, thread_id))
    cur = service.status_summary(mb, thread_id=thread_id)
    if cur["worker_state"] in (STATE_STARTING, STATE_RUNNING):
        return {"ok": False, "code": "already_running", "status": cur}
    # worker 在仓库根目录里启动:`python -m` 会把 cwd 放进 sys.path,`graph:build_graph` 才导入得到
    if cwd is None and os.environ.get("AGENT_GATE_REPO_ROOT"):
        cwd = Path(os.environ["AGENT_GATE_REPO_ROOT"])

    args = [python, "-m", "agent_gate", "worker", "--thread-id", thread_id,
            "--outputs-root", str(outputs_root), "--graph-factory", graph_factory,
            "--backend", backend]
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
    return {"ok": True, "pid": p.pid, "gate_root": str(mb.root),
            "log": str(mb.root / "worker.log")}
