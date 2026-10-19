"""服务层:CLI 和 MCP 服务共用的业务函数。返回值全部是可 JSON 序列化的 dict。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from agent_gate.mailbox import Mailbox
from agent_gate.protocol import (
    DEFAULT_MAX_ATTEMPTS,
    STALE_AFTER_S,
    STARTING_STALE_AFTER_S,
    STATE_COMPLETED,
    STATE_CRASHED,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_STARTING,
    STATE_WAITING_HUMAN,
    parse_utc,
    utc_now,
)
from agent_gate.safety import UnsafeIdentifier, UnsafePath, rejection, safe_task_id
from agent_gate.validate import InvalidSchema, NotJSON, parse_json_value, validate_result


# ---------------------------------------------------------------- 任务视图
def task_view(task: dict[str, Any], *, max_chars: Optional[int] = None) -> dict[str, Any]:
    """给 Agent 看的任务视图。max_chars 限制 prompt 长度(防止 MCP 输出过大)。"""

    def cut(s: str) -> str:
        if max_chars is None or len(s) <= max_chars:
            return s
        return s[:max_chars] + f"…(已截断,共 {len(s)} 字;用 gate_get_task 看全文,CLI 用 task)"

    return {
        "task_id": task["task_id"],
        "node": task.get("node"),
        "answer_format": task["answer_format"],
        "system_prompt": cut(task["system_prompt"]),
        "user_prompt": cut(task["user_prompt"]),
        "json_schema": task.get("json_schema"),
        "frames": task.get("frames", []),
        "notes": list(task.get("notes", [])),
        "how_to_answer": (
            "用 gate_submit_result 提交(CLI 用 submit)。answer 必须是符合 json_schema 的 JSON 字符串"
            if task["answer_format"] == "json"
            else "用 gate_submit_result 提交(CLI 用 submit)。answer 是纯文本,严格按 prompt 要求的格式写"
        ),
    }


def pack_for_images(
    tasks: list[dict[str, Any]], max_images: int
) -> tuple[list[dict[str, Any]], int, list[tuple[str, str]]]:
    """按「图片预算」挑任务,保证入选任务的画面都完整显示,不会悄悄缺图。

    规则:
    - 按原顺序取;累计图片数超过 max_images 就停,后面的任务留给下一次取。
    - 第一个任务永远入选(哪怕它自己的图片就超预算:只显示前 max_images 张,并在 notes 里写明)。
    - 只数磁盘上真实存在的图片。
    返回 (入选任务, 被推迟的任务数, 要附带的图片 [(task_id, 路径), ...])。
    入选任务会被加上 ``images = {"shown": 实际附带张数, "total": 任务原有图片数}``。
    """
    chosen: list[dict[str, Any]] = []
    pairs: list[tuple[str, str]] = []
    for t in tasks:
        existing = [p for p in t.get("frames", []) if Path(p).exists()]
        if chosen and len(pairs) + len(existing) > max_images:
            break
        take = existing[: max(0, max_images - len(pairs))]
        total = len(t.get("frames", []))
        t["images"] = {"shown": len(take), "total": total}
        if len(take) < total:
            t.setdefault("notes", []).append(
                f"只显示了 {len(take)}/{total} 张画面。看不全的部分请如实说明,不要编造。")
        pairs += [(t["task_id"], p) for p in take]
        chosen.append(t)
    return chosen, len(tasks) - len(chosen), pairs


def pending(mb: Mailbox, *, limit: int = 5, max_chars: Optional[int] = 6000) -> list[dict[str, Any]]:
    out = []
    for tid in mb.pending_ids()[:limit]:
        task = mb.read_task(tid)
        if task is not None:
            out.append(task_view(task, max_chars=max_chars))
    return out


def get_task(mb: Mailbox, tid: str) -> Optional[dict[str, Any]]:
    """取单个任务视图。task_id 不过白名单 → 返回 None(调用方报 unknown_task)。"""
    try:
        safe_task_id(tid)
        task = mb.read_task(tid)
    except (UnsafeIdentifier, UnsafePath):
        return None
    return task_view(task) if task is not None else None


# ---------------------------------------------------------------- 提交
def submit_result(mb: Mailbox, tid: str, answer: Any) -> dict[str, Any]:
    # task_id 由 Agent 传进来,先过白名单(F1):非法一律拒,不碰文件系统。
    try:
        safe_task_id(tid)
    except (UnsafeIdentifier, UnsafePath) as e:
        mb.log_event("input_rejected", code="bad_task_id")
        return rejection(e)

    task = mb.read_task(tid)
    if task is None:
        return {"ok": False, "code": "unknown_task", "message": f"没有这个任务:{tid}"}
    if mb.read_result(tid) is not None:
        return {"ok": True, "already_answered": True}
    err = mb.read_error(tid)
    if err is not None:
        return {"ok": False, "code": "task_closed",
                "message": f"任务已关闭({err.get('reason')}),不再接收答案"}

    limit = int(task.get("attempt_limit") or DEFAULT_MAX_ATTEMPTS)
    errors: list[str] = []
    value: Any = answer
    if task["answer_format"] == "json":
        if isinstance(answer, str):
            try:
                value = parse_json_value(answer)
            except NotJSON as e:
                errors = [str(e)]
        if not errors:
            try:
                errors = validate_result(task.get("json_schema"), value)
            except InvalidSchema as e:
                # F4 fail closed:schema 非法是代码 bug,重试多少次都没用,
                # 所以直接关任务,而且**不消耗 attempts 配额**。
                mb.log_event("input_rejected", task_id=tid, code="schema_invalid")
                mb.write_error(tid, "bad_schema", str(e))
                mb.log_event("task_closed_bad_schema", task_id=tid)
                return {"ok": False, "code": "schema_invalid",
                        "message": "这个任务的 json_schema 本身不合法,属于流水线代码 bug,不是你的问题。"
                                   "该任务已关闭,不要重试。"}
    else:
        value = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        if not value.strip():
            errors = ["答案为空"]

    if errors:
        n = mb.bump_attempts(tid, errors)
        left = limit - n
        mb.log_event("task_invalid", task_id=tid, attempt=n, errors=errors)
        if left <= 0:
            mb.write_error(tid, "rejected", "; ".join(errors))
            mb.log_event("task_rejected", task_id=tid)
            return {"ok": False, "code": "attempts_exhausted", "errors": errors, "attempts_left": 0}
        return {"ok": False, "code": "invalid", "errors": errors, "attempts_left": left}

    mb.write_result(tid, {"task_id": tid, "answer": value, "submitted_at": utc_now(),
                          "attempts": mb.attempts(tid) + 1})
    mb.log_event("task_answered", task_id=tid)
    return {"ok": True}


def submit_batch(mb: Mailbox, results: dict[str, Any]) -> dict[str, Any]:
    """results: {task_id: answer}。逐个提交,汇总结果。"""
    per: dict[str, Any] = {}
    for tid, answer in results.items():
        per[tid] = submit_result(mb, tid, answer)
    return {"ok": all(r.get("ok") for r in per.values()), "results": per}


# ---------------------------------------------------------------- 状态
def _heartbeat_age_s(status: dict[str, Any]) -> float:
    from datetime import datetime, timezone

    hb = status.get("heartbeat_at") or status.get("started_at")
    if not hb:
        return float("inf")
    return (datetime.now(timezone.utc) - parse_utc(hb)).total_seconds()


def status_summary(mb: Mailbox, *, thread_id: str) -> dict[str, Any]:
    st = mb.read_status()
    pending_ids = mb.pending_ids()
    out: dict[str, Any] = {
        "thread_id": thread_id,
        "worker_state": None,
        "pending_tasks": len(pending_ids),
        "pending_task_ids": pending_ids[:20],
        "answered_tasks": mb.count("result"),
        "degraded_tasks": mb.count("error"),
        "interrupts": [],
        "error": None,
    }
    if st is None:
        out["worker_state"] = "not_started"
        out["next_action"] = "start_run"
        return out

    state = st.get("state")
    if state == STATE_STARTING:
        # starting 有自己的宽限期:worker 还没写出第一次心跳(见 protocol 说明)
        if _heartbeat_age_s(st) > STARTING_STALE_AFTER_S:
            state = STATE_CRASHED
    elif state == STATE_RUNNING and _heartbeat_age_s(st) > STALE_AFTER_S:
        state = STATE_CRASHED
    out["worker_state"] = state
    out["interrupts"] = st.get("interrupts", [])
    out["error"] = st.get("error")

    if state == STATE_CRASHED:
        action = "restart_worker"
    elif state in (STATE_STARTING, STATE_RUNNING):
        action = "answer_tasks" if pending_ids else "wait"
    elif state == STATE_WAITING_HUMAN:
        action = "ask_human"
        out["human_commands"] = [
            f"python -m agent_gate resume --thread-id {thread_id} "
            f"--checkpoint {i.get('checkpoint')} --value true"
            for i in out["interrupts"]
        ]
    elif state == STATE_COMPLETED:
        action = "done"
    elif state == STATE_FAILED:
        action = "report_failure"
    else:
        action = "wait"
    out["next_action"] = action
    return out


def wait_for_work(mb: Mailbox, *, thread_id: str, wait_s: float, poll_s: float = 0.5,
                  limit: int = 5, max_chars: Optional[int] = 6000) -> dict[str, Any]:
    """长轮询:最多等 wait_s 秒,直到出现待办任务或 worker 进入终态,然后返回。"""
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        summary = status_summary(mb, thread_id=thread_id)
        tasks = pending(mb, limit=limit, max_chars=max_chars)
        if tasks or summary["next_action"] not in ("wait",) or time.monotonic() >= deadline:
            summary["tasks"] = tasks
            return summary
        time.sleep(poll_s)
