"""文件信箱:LLM 任务与结果都落在 <gate_root>/ 下的 JSON 文件里。

目录结构::

    <gate_root>/                       默认 outputs/<thread_id>/agent_gate/
      status.json                      worker 状态(worker 写)
      events.jsonl                     事件日志(追加)
      worker.log                       worker 的 stdout/stderr
      tasks/<id>.task.json             待办任务(GateLLMClient 写)
      tasks/<id>.result.json           Agent 提交且校验通过的结果(服务层写)
      tasks/<id>.error.json            放弃该任务的原因(超时 / 次数用尽)
      tasks/<id>.attempts.json         已提交次数与最近错误
      frames/<id>/*.jpg                任务附带的图片(视频抽帧)
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from agent_gate.protocol import utc_now
from agent_gate.safety import (
    UnsafeIdentifier,
    UnsafePath,
    safe_frame_dir,
    safe_task_id,
)

_LOCK = threading.Lock()


def atomic_write_json(path: Path, obj: Any) -> None:
    """先写临时文件再 os.replace,读的人永远看不到半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows:目标文件正被别的进程短暂占用
            time.sleep(0.05 * (i + 1))
    os.replace(tmp, path)


def read_json(path: Path) -> Optional[dict[str, Any]]:
    """读 JSON;文件不存在返回 None。Windows 上遇到瞬时占用会重试。"""
    for i in range(10):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    return None


def _under(base: Path, name: str, filename: str) -> Path:
    """在 base 下拼一个文件名,并确认 resolve 之后仍在 base 内。"""
    root = Path(base).resolve()
    p = (root / filename).resolve()
    if p != root and not p.is_relative_to(root):
        raise UnsafePath("task_path_escape")
    return p


class Mailbox:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.tasks_dir = self.root / "tasks"
        self.frames_root = self.root / "frames"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    # ---- 路径 ----
    # 所有按 task_id 拼出来的路径都**重复**过一遍白名单 + 目录归属(F1)。
    # 入口层(service / mcp_server / cli)已经校验过一次,这里再过一次是纵深防御:
    # 任何绕过入口的直接调用(mailbox 是公开类)也拿不到越界路径。
    def task_path(self, tid: str) -> Path:
        return _under(self.tasks_dir, safe_task_id(tid), f"{tid}.task.json")

    def result_path(self, tid: str) -> Path:
        return _under(self.tasks_dir, safe_task_id(tid), f"{tid}.result.json")

    def error_path(self, tid: str) -> Path:
        return _under(self.tasks_dir, safe_task_id(tid), f"{tid}.error.json")

    def attempts_path(self, tid: str) -> Path:
        return _under(self.tasks_dir, safe_task_id(tid), f"{tid}.attempts.json")

    def frames_dir(self, tid: str) -> Path:
        safe_task_id(tid)
        return safe_frame_dir(self, tid)

    @property
    def status_path(self) -> Path:
        return self.root / "status.json"

    # ---- 任务 / 结果 / 错误 ----
    def write_task(self, task: dict[str, Any]) -> bool:
        """已存在则不覆盖。返回是否新建。"""
        p = self.task_path(task["task_id"])
        if p.exists():
            return False
        atomic_write_json(p, task)
        return True

    def read_task(self, tid: str) -> Optional[dict[str, Any]]:
        return read_json(self.task_path(tid))

    def write_result(self, tid: str, record: dict[str, Any]) -> None:
        atomic_write_json(self.result_path(tid), record)

    def read_result(self, tid: str) -> Optional[dict[str, Any]]:
        return read_json(self.result_path(tid))

    def write_error(self, tid: str, reason: str, message: str) -> None:
        atomic_write_json(
            self.error_path(tid),
            {"task_id": tid, "reason": reason, "message": message, "created_at": utc_now()},
        )

    def read_error(self, tid: str) -> Optional[dict[str, Any]]:
        return read_json(self.error_path(tid))

    def reset_errors(self) -> int:
        """worker 启动时清掉上次遗留的失败标记,让这些任务重新开放。返回清掉的个数。"""
        n = 0
        for p in list(self.tasks_dir.glob("*.error.json")):
            tid = p.name[: -len(".error.json")]
            try:
                safe_task_id(tid)        # 目录里混入的怪文件直接跳过,不要拿它拼路径
            except UnsafeIdentifier:
                continue
            for q in (p, self.attempts_path(tid)):
                try:
                    q.unlink()
                except FileNotFoundError:
                    pass
            n += 1
        return n

    # ---- 次数 ----
    def attempts(self, tid: str) -> int:
        rec = read_json(self.attempts_path(tid)) or {}
        return int(rec.get("count", 0))

    def bump_attempts(self, tid: str, errors: list[str]) -> int:
        count = self.attempts(tid) + 1
        atomic_write_json(
            self.attempts_path(tid),
            {"task_id": tid, "count": count, "last_errors": errors, "updated_at": utc_now()},
        )
        return count

    # ---- 列表 ----
    def pending_ids(self) -> list[str]:
        """还没有结果、也没被放弃的任务,按创建先后排序。"""
        found: list[tuple[int, str]] = []
        for p in self.tasks_dir.glob("*.task.json"):
            tid = p.name[: -len(".task.json")]
            try:
                safe_task_id(tid)        # 不合法的一律不认(目录被手工塞过东西也不会炸)
                done = self.result_path(tid).exists() or self.error_path(tid).exists()
            except (UnsafeIdentifier, UnsafePath):
                continue
            if done:
                continue
            try:
                found.append((p.stat().st_mtime_ns, tid))
            except FileNotFoundError:
                continue
        return [tid for _, tid in sorted(found)]

    def count(self, suffix: str) -> int:
        return sum(1 for _ in self.tasks_dir.glob(f"*.{suffix}.json"))

    # ---- worker 状态 ----
    def write_status(self, status: dict[str, Any]) -> None:
        atomic_write_json(self.status_path, status)

    def read_status(self) -> Optional[dict[str, Any]]:
        return read_json(self.status_path)

    # ---- 事件日志 ----
    def log_event(self, event: str, **fields: Any) -> None:
        rec = {"ts": utc_now(), "event": event, **fields}
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with _LOCK:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.root / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(line)

    def read_events(self, limit: int = 200) -> list[dict[str, Any]]:
        p = self.root / "events.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
