"""GateLLMClient:实现仓库的 ``LLMClient`` 协议,但不调 API,而是把请求写成任务文件,
阻塞等待 Agent 提交答案。

为什么是「阻塞等待」而不是在 chat() 里调 ``interrupt()``:
仓库里 11 处 chat 调用全都包在 ``except Exception`` 里,而 LangGraph 的
``GraphInterrupt`` 继承自 ``Exception``(沙箱实测:被吞掉,图不会暂停)。
阻塞等待不需要抛任何异常,所以不受影响。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

from agent_gate.mailbox import Mailbox
from agent_gate.media import extract_frames, is_image
from agent_gate.protocol import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_POLL_S,
    DEFAULT_TIMEOUT_S,
    SCHEMA_VERSION,
    task_id_for,
    utc_now,
)
from agent_gate.validate import InvalidSchema, check_schema


class LLMGateError(Exception):
    """闸门错误基类。继承 Exception:仓库里 ``except Exception`` 的降级逻辑会接住它。"""


class LLMGateTimeout(LLMGateError):
    """等 Agent 回答超时。"""


class LLMGateRejected(LLMGateError):
    """任务被放弃(Agent 多次提交不合格结果,或已超时)。"""


def answer_to_text(answer: Any) -> str:
    """把存盘的答案还原成 chat() 要返回的字符串。"""
    if isinstance(answer, str):
        return answer
    return json.dumps(answer, ensure_ascii=False)


def _current_node() -> Optional[str]:
    """在 LangGraph 节点里调用时,返回当前节点名;其他场景返回 None。"""
    try:
        from langgraph.config import get_config

        return (get_config().get("metadata") or {}).get("langgraph_node")
    except Exception:  # noqa: BLE001 — 不在 runnable 上下文里会抛 RuntimeError
        return None


class GateLLMClient:
    def __init__(
        self,
        mailbox: Mailbox,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_s: float = DEFAULT_POLL_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        frames_per_clip: int = 6,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.mailbox = mailbox
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.max_attempts = max_attempts
        self.frames_per_clip = frames_per_clip
        self._sleep = sleep
        self._clock = clock
        self._failed: set[str] = set()

    # ---- LLMClient 协议 ----
    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        media: Optional[list[dict[str, Any]]] = None,
        json_schema: Optional[dict[str, Any]] = None,
        temperature: float = 0.3,
        top_p: float = 0.9,
        max_tokens: int = 4096,
    ) -> str:
        media_list = [dict(m) for m in (media or [])]
        identity = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "media": media_list,
            "json_schema": json_schema,
        }
        tid = task_id_for(identity)

        done = self.mailbox.read_result(tid)
        if done is not None:  # 重启后重放:上次已经答过
            return answer_to_text(done["answer"])
        if tid in self._failed:  # 同一进程里已经放弃过,别再等一遍
            raise LLMGateRejected(f"任务 {tid} 本次运行已放弃")

        if self.mailbox.read_task(tid) is None:
            self._create_task(
                tid, identity, {"temperature": temperature, "top_p": top_p, "max_tokens": max_tokens}
            )
        return self._wait(tid)

    # ---- 内部 ----
    def _create_task(self, tid: str, identity: dict[str, Any], params: dict[str, Any]) -> None:
        frames: list[str] = []
        notes: list[str] = []
        for m in identity["media"]:
            path = str(m.get("path") or "")
            if not path:
                continue
            if is_image(path):
                frames.append(path)
                continue
            try:
                got = extract_frames(
                    path, m.get("in_sec"), m.get("out_sec"),
                    self.mailbox.frames_dir(tid), n=self.frames_per_clip,
                )
            except Exception as e:  # noqa: BLE001 — 取不出帧不应让任务发不出去
                got = []
                notes.append(f"抽帧失败 {path}: {type(e).__name__}: {e}")
            if not got:
                notes.append(f"没有取到画面帧:{path}")
            frames += got
        task = {
            "schema_version": SCHEMA_VERSION,
            "task_id": tid,
            "created_at": utc_now(),
            "node": _current_node(),
            "answer_format": "json" if identity["json_schema"] else "text",
            "system_prompt": identity["system_prompt"],
            "user_prompt": identity["user_prompt"],
            "json_schema": identity["json_schema"],
            "media": identity["media"],
            "frames": frames,
            "notes": notes,
            "params": params,
            "attempt_limit": self.max_attempts,
        }
        # F4:写盘之前先预检 schema。非法就把任务标成 schema_invalid,
        # 让 Agent 看到明确说明,而不是反复提交、重试到配额用尽才发现白费。
        try:
            check_schema(identity["json_schema"])
        except InvalidSchema as e:
            task["schema_invalid"] = True
            notes.append(f"这个任务的 json_schema 本身不合法(流水线代码 bug):{e}。"
                         "不要为它提交答案,重试没有意义。")
            task["notes"] = notes
        if self.mailbox.write_task(task):
            self.mailbox.log_event("task_created", task_id=tid, node=task["node"],
                                   frames=len(frames),
                                   schema_invalid=bool(task.get("schema_invalid")))

    def _wait(self, tid: str) -> str:
        deadline = self._clock() + self.timeout_s
        while True:
            rec = self.mailbox.read_result(tid)
            if rec is not None:
                return answer_to_text(rec["answer"])
            err = self.mailbox.read_error(tid)
            if err is not None:
                self._failed.add(tid)
                raise LLMGateRejected(f"任务 {tid} 被放弃:{err.get('reason')}:{err.get('message')}")
            if self._clock() >= deadline:
                self.mailbox.write_error(tid, "timeout", f"等待超过 {self.timeout_s:.0f}s")
                self.mailbox.log_event("task_timeout", task_id=tid)
                self._failed.add(tid)
                raise LLMGateTimeout(f"任务 {tid} 等待超过 {self.timeout_s:.0f}s")
            self._sleep(self.poll_s)
