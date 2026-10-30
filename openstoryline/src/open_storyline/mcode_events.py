"""MCode ``stream-json`` 事件模型。

本模块只定义**数据形状**:不碰 IO、不碰子进程、不做协议兼容。
想读事件流请用 :mod:`open_storyline.mcode_stream_parser`;
想跑子进程请用 :mod:`open_storyline.mcode_runner`。

协议依据
--------
本机实测 ``mcode CLI 0.6.12`` 的 ``mcode exec --output-format stream-json``,
样本落盘在 ``openstoryline/tests/fixtures/mcode_stream/``。
枚举值取自该版本安装包内的状态定义
(``Start:1, Finished:2, Failed:3, Preparing:4, Prepared:5``)。

安全边界(硬约束)
----------------
``item.type == "reasoning"`` 携带的是模型**隐藏思维链原文**。
解析层直接丢弃这类事件,原文**不进入**本模块定义的对象,
更不能透传浏览器。需要「思考摘要」时,只能用真实状态 / 工具事件派生。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Mapping, Optional

#: 本模块支持的协议版本。``schemaVersion`` 不是这个值就当协议不兼容。
SCHEMA_VERSION = 1


class MCodeSchemaError(RuntimeError):
    """协议不符。

    典型触发:``schemaVersion`` 不是 1、envelope 字段缺失、``exec.completed`` 没有
    合法 ``result``。这类错误**不能**被当成「空文本」吞掉。
    """


class MCodeToolStatus(IntEnum):
    """``toolCall.status`` 的数值枚举。

    取自 mcode CLI 0.6.12 安装包内的状态定义。

    ==========  ======  ======================================================
    枚举名        数值    语义
    ==========  ======  ======================================================
    ``START``      1     开始执行,此时通常带 ``input``
    ``FINISHED``   2     执行完成,此时通常带 ``output``
    ``FAILED``     3     执行失败,此时通常带 ``error``
    ``PREPARING``  4     工具准备中
    ``PREPARED``   5     准备完成,等待执行
    ==========  ======  ======================================================
    """

    START = 1
    FINISHED = 2
    FAILED = 3
    PREPARING = 4
    PREPARED = 5


# -- 事件类型 ---------------------------------------------------------------

EVENT_EXEC_STARTED = "exec.started"
EVENT_SESSION_STARTED = "session.started"
EVENT_SESSION_RESUMED = "session.resumed"
EVENT_TURN_STARTED = "turn.started"
EVENT_ITEM_STARTED = "item.started"
EVENT_ITEM_UPDATED = "item.updated"
EVENT_ITEM_COMPLETED = "item.completed"
EVENT_TURN_COMPLETED = "turn.completed"
EVENT_TURN_FAILED = "turn.failed"
EVENT_EXEC_COMPLETED = "exec.completed"

#: 已知事件类型。**不在**这个集合里的类型会被解析层跳过并记诊断。
KNOWN_EVENT_TYPES = frozenset(
    {
        EVENT_EXEC_STARTED,
        EVENT_SESSION_STARTED,
        EVENT_SESSION_RESUMED,
        EVENT_TURN_STARTED,
        EVENT_ITEM_STARTED,
        EVENT_ITEM_UPDATED,
        EVENT_ITEM_COMPLETED,
        EVENT_TURN_COMPLETED,
        EVENT_TURN_FAILED,
        EVENT_EXEC_COMPLETED,
    }
)

#: 携带 ``item`` 字段的事件类型。
ITEM_EVENT_TYPES = frozenset(
    {EVENT_ITEM_STARTED, EVENT_ITEM_UPDATED, EVENT_ITEM_COMPLETED}
)

# -- item 类型 --------------------------------------------------------------

ITEM_AGENT_MESSAGE = "agent_message"
ITEM_REASONING = "reasoning"
ITEM_TOOL_CALL = "tool_call"

#: 已知 item 类型。
KNOWN_ITEM_TYPES = frozenset({ITEM_AGENT_MESSAGE, ITEM_REASONING, ITEM_TOOL_CALL})


@dataclass(frozen=True)
class MCodeToolCall:
    """一次工具调用。

    生命周期由 :attr:`status` 驱动,**不是**由 ``item.started`` / ``item.completed``
    配对驱动:实测工具项只会收到 ``item.started`` + 若干次 ``item.updated``,
    收尾状态(Finished / Failed)同样走 ``item.updated``。
    """

    id: str
    name: str
    status: Optional[MCodeToolStatus] = None
    #: 原始数值。``status`` 解析不出来时(上游加了新枚举)原值留在这里。
    raw_status: Optional[int] = None
    input: Any = None
    output: Any = None
    error: Any = None

    @property
    def is_finished(self) -> bool:
        return self.status == MCodeToolStatus.FINISHED

    @property
    def is_failed(self) -> bool:
        return self.status == MCodeToolStatus.FAILED

    @property
    def is_running(self) -> bool:
        return self.status in (
            MCodeToolStatus.PREPARING,
            MCodeToolStatus.PREPARED,
            MCodeToolStatus.START,
        )


@dataclass(frozen=True)
class MCodeItem:
    """一个输出项。

    ``content_delta`` 来自上游的 ``item.contentDelta``。
    注意:**首段文本装在 ``item.started`` 里**,``item.updated`` 只带后续增量。
    丢掉 ``item.started`` 就等于丢掉每条消息的第一段。

    ``content`` 来自 ``item.completed`` 的 ``item.content``,是该项的完整文本,
    只用于一致性校验。
    """

    id: str
    type: str
    content_delta: str = ""
    content: Optional[str] = None
    tool_call: Optional[MCodeToolCall] = None

    @property
    def is_agent_message(self) -> bool:
        return self.type == ITEM_AGENT_MESSAGE

    @property
    def is_reasoning(self) -> bool:
        return self.type == ITEM_REASONING

    @property
    def is_tool_call(self) -> bool:
        return self.type == ITEM_TOOL_CALL


@dataclass(frozen=True)
class MCodeEvent:
    """一条 ``stream-json`` 事件。

    ``raw`` 保留原始 JSON 对象,供诊断与阶段 D 的状态映射使用。
    解析层保证 ``raw`` 里**不含** reasoning 原文(reasoning 事件根本不会被构造出来)。
    """

    type: str
    sequence: int
    timestamp_ms: int
    run_id: str
    session_id: str
    turn_id: str
    schema_version: int = SCHEMA_VERSION
    item: Optional[MCodeItem] = None
    #: ``exec.completed`` 的 ``result`` 对象。
    result: Optional[Mapping[str, Any]] = None
    #: ``turn.completed`` / ``exec.completed`` 的模型信息。
    model: Optional[Mapping[str, Any]] = None
    #: token 用量。
    usage: Optional[Mapping[str, Any]] = None
    duration_ms: Optional[int] = None
    #: ``exec.completed`` 的 ``result.status``;``turn.failed`` 时为失败状态名。
    status: Optional[str] = None
    #: ``turn.failed`` 的错误信息。
    error: Optional[Any] = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def text_delta(self) -> str:
        """助手可见文本增量。

        **只有** ``agent_message`` 的增量才算正文。
        reasoning / tool_call 一律返回空字符串 —— 这是防「隐藏思维链外泄」的兜底闸门,
        即使调用方绕过解析层直接传了别的 item,这里也拿不到文本。
        """
        if self.item is None or not self.item.is_agent_message:
            return ""
        return self.item.content_delta

    @property
    def is_terminal(self) -> bool:
        """这是不是本轮的收尾事件(成功 / 失败)。"""
        return self.type in (EVENT_TURN_COMPLETED, EVENT_TURN_FAILED)

    @property
    def is_exec_completed(self) -> bool:
        return self.type == EVENT_EXEC_COMPLETED

    @property
    def final_output(self) -> str:
        """``exec.completed`` 里的最终正文。非收尾事件返回空串。"""
        if self.result is None:
            return ""
        output = self.result.get("output")
        return output if isinstance(output, str) else ""


def tool_status_from_value(value: Any) -> Optional[MCodeToolStatus]:
    """把上游的 ``toolCall.status`` 数值转成枚举。

    认不出来的值返回 ``None``(原始数值由调用方自己从 raw 里取),
    **不抛错** —— 上游加新枚举不该把整条流打断。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, MCodeToolStatus):
        return value
    if isinstance(value, int):
        try:
            return MCodeToolStatus(value)
        except ValueError:
            return None
    return None


__all__ = [
    "SCHEMA_VERSION",
    "MCodeSchemaError",
    "MCodeToolStatus",
    "MCodeToolCall",
    "MCodeItem",
    "MCodeEvent",
    "EVENT_EXEC_STARTED",
    "EVENT_SESSION_STARTED",
    "EVENT_SESSION_RESUMED",
    "EVENT_TURN_STARTED",
    "EVENT_ITEM_STARTED",
    "EVENT_ITEM_UPDATED",
    "EVENT_ITEM_COMPLETED",
    "EVENT_TURN_COMPLETED",
    "EVENT_TURN_FAILED",
    "EVENT_EXEC_COMPLETED",
    "KNOWN_EVENT_TYPES",
    "ITEM_EVENT_TYPES",
    "ITEM_AGENT_MESSAGE",
    "ITEM_REASONING",
    "ITEM_TOOL_CALL",
    "KNOWN_ITEM_TYPES",
    "tool_status_from_value",
]
