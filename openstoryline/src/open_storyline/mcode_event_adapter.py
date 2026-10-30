"""把 MCode CLI 事件映射成 OpenStoryline 内部的 WebSocket 事件(阶段 D)。

这一层只做**翻译**:上游 ``stream-json`` 事件 → 内部事件负载。
它不认识 WebSocket、也不认识队列 —— 那是 ``agent_fastapi.py`` 的活。

为什么单独一层
--------------

* 协议细节(``toolCall.status`` 枚举、``item.contentDelta``)只在这里出现一次;
* 工具生命周期需要**状态**:同一个工具 id 只发一次 ``tool.start``、
  只发一次 ``tool.end``(上游会把 ``Preparing`` 重复发好几遍);
* 单测不用起子进程就能把映射规则钉死。

三条硬规则(设计文档 §3.1.7 / §6.4)
------------------------------------

1. **``item.started`` 与 ``item.updated`` 都要读 ``item.contentDelta``** ——
   首段文本在 ``item.started`` 里,只认 ``item.updated`` 会丢掉每条消息的第一段。
2. **工具生命周期由 ``toolCall.status`` 枚举驱动**,不由 item 配对驱动。
3. **一轮里可能有多条 ``agent_message``**,最终正文取最后一条 ——
   也就是 ``exec.completed.result.output``,由 :attr:`MCodeEventAdapter.final_output` 给出。

安全边界
--------

* ``reasoning``(隐藏思维链)在解析层就整条丢了,这里**根本拿不到**原文;
* 工具输出**必须摘要化 + 截断**后才准出去 —— 原文可能含路径、密钥、大段日志。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from open_storyline.mcode_events import (
    EVENT_EXEC_COMPLETED,
    EVENT_EXEC_STARTED,
    EVENT_SESSION_RESUMED,
    EVENT_SESSION_STARTED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    EVENT_TURN_STARTED,
    ITEM_EVENT_TYPES,
    MCodeEvent,
    MCodeToolCall,
    MCodeToolStatus,
)

logger = logging.getLogger(__name__)

# -- 内部事件类型 ----------------------------------------------------------

#: 文本增量 → ``assistant.delta``。
KIND_TEXT = "text"

#: 执行状态 → ``agent.status``。
KIND_STATUS = "status"

#: 工具开始 → ``tool.start``。
KIND_TOOL_START = "tool.start"

#: 工具进行中 → ``tool.progress``。
KIND_TOOL_PROGRESS = "tool.progress"

#: 工具结束 → ``tool.end``。
KIND_TOOL_END = "tool.end"

#: 最终正文 → ``assistant.end`` 的收口依据。
KIND_FINAL = "final"

# -- 状态 / 阶段枚举(设计文档 §7.2)----------------------------------------

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_WAITING_TOOL = "waiting_tool"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_TIMED_OUT = "timed_out"

PHASE_INITIALIZING = "initializing"
PHASE_ANALYZING = "analyzing"
PHASE_TOOL_EXECUTION = "tool_execution"
PHASE_SYNTHESIZING = "synthesizing"
PHASE_COMPLETED = "completed"

#: 工具卡片的来源标识(前端过程面板显示「MCode Agent」)。
MCODE_SERVER_NAME = "MCode Agent"

#: 单条工具输出摘要的默认上限。
DEFAULT_MAX_TEXT_CHARS = 2000

#: 单轮最多转发多少条过程事件。
DEFAULT_MAX_TRACE_EVENTS = 300


@dataclass(frozen=True)
class MCodeInternalEvent:
    """一条内部事件。

    ``kind`` 决定它会被翻译成哪个 WebSocket 事件,
    ``data`` 是已经裁剪过的负载(不含 ``source`` / ``seq`` / ``run_id``,
    那三个由适配器统一补上)。
    """

    kind: str
    data: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 摘要化工具
# --------------------------------------------------------------------------

def _stringify(value: Any, max_chars: int) -> str:
    """把任意值压成一段**有上限**的可读文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, Mapping) or isinstance(value, (list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[:max_chars] + f"...(截断,共 {len(text)} 字符)"
    return text


def summarize_tool_output(output: Any, max_chars: int = DEFAULT_MAX_TEXT_CHARS) -> str:
    """把工具输出摘要化 + 截断。

    上游 ``toolCall.output`` 的形状是
    ``{"content":[{"type":"text","text":...}], "details":{...}}``。
    这里只取可读文本,``details`` 在没有正文时兜底 —— **原文一律不进浏览器**。
    """
    if output is None:
        return ""
    if isinstance(output, str):
        return _stringify(output, max_chars)
    if not isinstance(output, Mapping):
        return _stringify(output, max_chars)

    parts: List[str] = []
    content = output.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
    if not parts:
        details = output.get("details")
        if details is not None:
            parts.append(_stringify(details, max_chars))
    return _stringify("\n".join(p for p in parts if p), max_chars)


def summarize_tool_args(value: Any, max_chars: int = DEFAULT_MAX_TEXT_CHARS) -> Dict[str, Any]:
    """工具入参摘要:保留结构,长的值截断,条目数有上限。"""
    if not isinstance(value, Mapping):
        return {"value": _stringify(value, max_chars)}
    out: Dict[str, Any] = {}
    for index, (key, val) in enumerate(value.items()):
        if index >= 20:
            out["_truncated"] = f"还有 {len(value) - 20} 个参数未显示"
            break
        if isinstance(val, (Mapping, list, tuple)):
            out[str(key)] = _stringify(val, max_chars)
        else:
            out[str(key)] = _stringify(val, max_chars)
    return out


# --------------------------------------------------------------------------
# 适配器
# --------------------------------------------------------------------------

class MCodeEventAdapter:
    """有状态的 MCode 事件 → 内部事件适配器。

    一个 turn 一个实例(状态就是「本轮的工具生命周期」)。
    """

    def __init__(
        self,
        *,
        source: str = "mcode",
        session_id: str = "",
        turn_id: str = "",
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        max_trace_events: int = DEFAULT_MAX_TRACE_EVENTS,
        server_name: str = MCODE_SERVER_NAME,
    ) -> None:
        self.source = source
        self.session_id = session_id
        self.turn_id = turn_id
        self.max_text_chars = max_text_chars
        self.max_trace_events = max_trace_events
        self.server_name = server_name

        self._seq = 0
        self._seen = 0
        self._started_tools: set[str] = set()
        self._ended_tools: set[str] = set()
        self._final_output = ""

        # 本轮的事件标识基准(第一条事件立下,之后必须一致)。
        self._expected_run_id: Optional[str] = None
        self._expected_session_id: Optional[str] = None
        self._expected_turn_id: Optional[str] = None

    # -- 对外属性 --------------------------------------------------------

    @property
    def final_output(self) -> str:
        """``exec.completed.result.output`` —— 本轮最终正文的权威来源。"""
        return self._final_output

    @property
    def seq(self) -> int:
        """已产出的内部事件数(WebSocket 侧的单调 seq)。"""
        return self._seq

    @property
    def truncated(self) -> bool:
        """是否因为超过事件上限而丢弃过事件。"""
        return self._seen > self.max_trace_events

    # -- 主入口 ----------------------------------------------------------

    def adapt(self, event: MCodeEvent) -> List[MCodeInternalEvent]:
        """把一条 CLI 事件翻译成若干条内部事件。"""
        self._seen += 1
        if self._seen > self.max_trace_events:
            # 超上限:后面的过程事件直接丢(最终正文不受影响 ——
            # 它走 exec.completed,一定在上限之内或之后由 agent.done 兜底)。
            logger.info(
                "mcode 过程事件超过 %d 条上限,后续事件丢弃", self.max_trace_events
            )
            return []

        if not self._check_identity(event):
            return []

        internal: List[MCodeInternalEvent] = []
        etype = event.type

        if etype == EVENT_EXEC_STARTED:
            internal.append(self._status(
                STATUS_RUNNING, PHASE_INITIALIZING, "MCode 进程已启动",
            ))
        elif etype in (EVENT_SESSION_STARTED, EVENT_SESSION_RESUMED):
            internal.append(self._status(
                STATUS_RUNNING, PHASE_INITIALIZING, "MCode 会话已就绪",
            ))
        elif etype == EVENT_TURN_STARTED:
            internal.append(self._status(
                STATUS_RUNNING, PHASE_ANALYZING, "正在分析任务",
            ))
        elif etype in ITEM_EVENT_TYPES:
            internal.extend(self._adapt_item(event))
        elif etype == EVENT_TURN_COMPLETED:
            internal.append(self._status(
                STATUS_COMPLETED, PHASE_COMPLETED, "本轮已完成",
            ))
        elif etype == EVENT_TURN_FAILED:
            message = _stringify(event.error or "MCode 本轮失败", self.max_text_chars)
            internal.append(self._status(
                STATUS_FAILED, PHASE_COMPLETED, message,
            ))
        elif etype == EVENT_EXEC_COMPLETED:
            self._final_output = event.final_output
            internal.append(MCodeInternalEvent(
                kind=KIND_FINAL,
                data={"text": self._final_output},
            ))

        # 统一补上来源标识与单调 seq:每条 WebSocket 事件都必须能说清
        # 「来自哪一轮、来源是谁、第几条」。
        for item in internal:
            self._seq += 1
            item.data.setdefault("source", self.source)
            item.data.setdefault("session_id", self.session_id)
            item.data.setdefault("turn_id", self.turn_id)
            item.data.setdefault("run_id", event.run_id)
            item.data["seq"] = self._seq

        return internal

    # -- 内部 ------------------------------------------------------------

    def _check_identity(self, event: MCodeEvent) -> bool:
        """校验事件的 ``runId`` / ``sessionId`` / ``turnId`` 与本轮一致。

        同一轮里的这三个 id 必须完全相同。第一条事件立下基准,
        之后对不上的**整条丢弃**并记诊断 —— 宁可少一条过程事件,
        也不能把别的轮次的事件混进当前 turn(SRC-07)。
        """
        if self._expected_run_id is None:
            self._expected_run_id = event.run_id
            self._expected_session_id = event.session_id
            self._expected_turn_id = event.turn_id
            return True

        if (
            event.run_id == self._expected_run_id
            and event.session_id == self._expected_session_id
            and event.turn_id == self._expected_turn_id
        ):
            return True

        logger.warning(
            "mcode 事件标识与本轮不一致,已丢弃: run=%s/%s session=%s/%s turn=%s/%s type=%s",
            event.run_id, self._expected_run_id,
            event.session_id, self._expected_session_id,
            event.turn_id, self._expected_turn_id,
            event.type,
        )
        return False

    def _status(self, status: str, phase: str, message: str) -> MCodeInternalEvent:
        return MCodeInternalEvent(
            kind=KIND_STATUS,
            data={"status": status, "phase": phase, "message": message},
        )

    def _adapt_item(self, event: MCodeEvent) -> List[MCodeInternalEvent]:
        item = event.item
        if item is None:
            return []
        if item.is_agent_message:
            # 首段(item.started)与后续(item.updated)都要读 contentDelta。
            delta = item.content_delta
            if not delta:
                return []
            return [MCodeInternalEvent(kind=KIND_TEXT, data={"delta": delta})]
        if item.is_tool_call and item.tool_call is not None:
            return self._adapt_tool(event, item.tool_call)
        return []

    def _adapt_tool(self, event: MCodeEvent, tool_call: MCodeToolCall) -> List[MCodeInternalEvent]:
        """按 ``toolCall.status`` 枚举驱动 start / progress / end。"""
        status = tool_call.status
        if status is None:
            # 上游加了新枚举:不猜,只记诊断跳过(不能把整条流打断)。
            logger.info(
                "mcode 工具 %s 的 status 认不出来: %r", tool_call.id, tool_call.raw_status
            )
            return []

        tool_id = tool_call.id
        if not tool_id and event.item is not None:
            tool_id = event.item.id

        if status in (
            MCodeToolStatus.PREPARING,
            MCodeToolStatus.PREPARED,
            MCodeToolStatus.START,
        ):
            if tool_id in self._started_tools:
                # 同一个工具只发一次 start;之后都是 progress。
                return [self._tool_progress(tool_call, status)]
            self._started_tools.add(tool_id)
            return [self._tool_start(tool_call)]

        if status in (MCodeToolStatus.FINISHED, MCodeToolStatus.FAILED):
            if tool_id in self._ended_tools:
                return []
            self._ended_tools.add(tool_id)
            return [self._tool_end(tool_call, status)]

        return []

    def _tool_start(self, tool_call: MCodeToolCall) -> MCodeInternalEvent:
        return MCodeInternalEvent(
            kind=KIND_TOOL_START,
            data={
                "tool_call_id": tool_call.id,
                "server": self.server_name,
                "name": tool_call.name,
                "args": summarize_tool_args(tool_call.input, self.max_text_chars),
            },
        )

    def _tool_progress(self, tool_call: MCodeToolCall, status: MCodeToolStatus) -> MCodeInternalEvent:
        if status == MCodeToolStatus.PREPARED:
            message = "准备完成,等待执行"
        elif status == MCodeToolStatus.START:
            message = "正在执行"
        else:
            message = "正在准备"
        return MCodeInternalEvent(
            kind=KIND_TOOL_PROGRESS,
            data={
                "tool_call_id": tool_call.id,
                "server": self.server_name,
                "name": tool_call.name,
                "message": message,
            },
        )

    def _tool_end(self, tool_call: MCodeToolCall, status: MCodeToolStatus) -> MCodeInternalEvent:
        is_error = status == MCodeToolStatus.FAILED
        if is_error:
            summary = summarize_tool_output(tool_call.error, self.max_text_chars)
            if not summary:
                summary = "工具执行失败"
        else:
            summary = summarize_tool_output(tool_call.output, self.max_text_chars)
        return MCodeInternalEvent(
            kind=KIND_TOOL_END,
            data={
                "tool_call_id": tool_call.id,
                "server": self.server_name,
                "name": tool_call.name,
                "is_error": is_error,
                "summary": summary,
            },
        )


__all__ = [
    "MCodeInternalEvent",
    "MCodeEventAdapter",
    "MCODE_SERVER_NAME",
    "KIND_TEXT",
    "KIND_STATUS",
    "KIND_TOOL_START",
    "KIND_TOOL_PROGRESS",
    "KIND_TOOL_END",
    "KIND_FINAL",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_WAITING_TOOL",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_CANCELLED",
    "STATUS_TIMED_OUT",
    "PHASE_INITIALIZING",
    "PHASE_ANALYZING",
    "PHASE_TOOL_EXECUTION",
    "PHASE_SYNTHESIZING",
    "PHASE_COMPLETED",
    "summarize_tool_args",
    "summarize_tool_output",
]
