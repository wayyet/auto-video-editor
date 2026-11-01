"""ZCode 事件适配 —— 把一次 zcode 无头运行翻译成内部事件。

定位
----
zcode CLI 0.17.1 的 ``-p/--prompt`` 无头形态**没有中途事件通道**(设计文档
附录 A.5:任何 ``-p`` 形态都不外发过程事件,``--json`` 只在结束时输出一个
结构化结果块)。所以本模块的能力是**有意收窄**的:

1. 状态事件:启动 / 周期心跳(可选)/ 终态(成功 / 失败 / 取消 / 超时);
2. 最终正文:结果 JSON 的 ``response`` 字段是唯一权威。

**不伪造工具事件**(设计 §6.3):拿不到工具开始 / 结束就不发 ``tool.*``,
前端面板自然只显示状态与最终文本 —— 与 mcode ``final_json`` 降级形态同款。

分层(与 mcode 对齐)
--------------------
* :class:`ZCodeChatModel`(zcode_agent_adapter)在**模型侧**知道「什么时候发生
  什么」,它用本模块的纯构造器造出 ``{"kind": ..., "data": ...}`` 原始事件,
  交给本轮 sink;
* WebSocket 层每轮建一个 :class:`ZCodeEventAdapter`,负责补上
  ``source`` / ``session_id`` / ``turn_id`` / ``run_id`` / ``seq`` 标识
  (与 ``MCodeEventAdapter.adapt`` 的统一补戳同款),产出
  :class:`MCodeInternalEvent`,再走同一条 ``_dispatch_mcode_internal`` 翻译成
  WebSocket 事件。

``KIND_*`` / ``STATUS_*`` / ``PHASE_*`` 词表**直接复用** mcode 事件适配器:
下游(``_dispatch_mcode_internal``、前端面板)只认这一套词表,不再发明第二套。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from open_storyline.mcode_event_adapter import (
    KIND_FINAL,
    KIND_STATUS,
    MCodeInternalEvent,
    PHASE_ANALYZING,
    PHASE_COMPLETED,
    PHASE_INITIALIZING,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_TIMED_OUT,
)

logger = logging.getLogger(__name__)

#: 前端面板 / trace 里的来源显示名(与 MCODE_SERVER_NAME 同位)。
ZCODE_SERVER_NAME = "ZCode Agent"

#: 内部事件的来源标识(assistant.start 的 ``source`` 字段、事件 data 的
#: ``source`` 字段都用它)。前端按它路由事件,与 ``mcode`` 平级。
ZCODE_SOURCE = "zcode"

#: 模型侧原始事件的类型:``{"kind": <KIND_*>, "data": {...}}``。
#: 只用 dict 而不是 dataclass,是为了让模型侧不必 import 内部事件类 ——
#: 模型层与 WS 层之间只传纯数据。
ZCodeRawEvent = Dict[str, Any]


# --------------------------------------------------------------------------
# 模型侧:纯构造器(zcode_agent_adapter 调用)
# --------------------------------------------------------------------------

def status_event(status: str, phase: str, message: str) -> ZCodeRawEvent:
    """造一条状态事件(启动 / 心跳 / 终态通用)。"""
    return {"kind": KIND_STATUS, "data": {"status": status, "phase": phase, "message": message}}


def running_event(message: str = "ZCode Agent 进程已启动") -> ZCodeRawEvent:
    return status_event(STATUS_RUNNING, PHASE_INITIALIZING, message)


def tick_event(elapsed_s: int, timeout_s: int) -> ZCodeRawEvent:
    """周期心跳:如实报告已运行秒数,不伪造阶段推进。"""
    return status_event(
        STATUS_RUNNING,
        PHASE_ANALYZING,
        f"ZCode Agent 仍在执行…(已运行 {elapsed_s}s / 上限 {timeout_s}s)",
    )


def completed_event(message: str = "本轮已完成") -> ZCodeRawEvent:
    return status_event(STATUS_COMPLETED, PHASE_COMPLETED, message)


def failed_event(message: str) -> ZCodeRawEvent:
    return status_event(STATUS_FAILED, PHASE_COMPLETED, message)


def cancelled_event() -> ZCodeRawEvent:
    return status_event(STATUS_CANCELLED, PHASE_COMPLETED, "本轮已被用户取消")


def timed_out_event(timeout_s: int) -> ZCodeRawEvent:
    return status_event(STATUS_TIMED_OUT, PHASE_COMPLETED, f"本轮执行超时(>{timeout_s}s)")


def final_event(text: str) -> ZCodeRawEvent:
    """最终正文事件。``text`` 是结果 JSON 的 ``response`` 字段(唯一权威)。"""
    return {"kind": KIND_FINAL, "data": {"text": text}}


# --------------------------------------------------------------------------
# WS 层:标识补戳适配器(每轮一个)
# --------------------------------------------------------------------------

class ZCodeEventAdapter:
    """给模型侧原始事件补上「来自哪一轮、来源是谁、第几条」。

    与 ``MCodeEventAdapter`` 的差别:zcode **没有** CLI 事件流可解析,
    本类不做翻译,只做标识补戳(统一补 ``source`` / ``session_id`` /
    ``turn_id`` / ``run_id`` / ``seq``)。

    ``run_id`` 为什么是构造期定死的:zcode 结果块要到**结束**才知道真实
    ``sessionId``,而前端的去重门(``TurnEventGate``)要求本轮所有事件的
    ``run_id`` 稳定 —— 所以本轮用一个合成 id(``zcode_<turn_id>``),
    真实 sessionId 走 AIMessage 的 ``response_metadata`` 回传,不进事件流。
    """

    def __init__(
        self,
        *,
        source: str = ZCODE_SOURCE,
        session_id: str = "",
        turn_id: str = "",
        run_id: Optional[str] = None,
        server_name: str = ZCODE_SERVER_NAME,
    ) -> None:
        self.source = source
        self.session_id = session_id
        self.turn_id = turn_id
        self.server_name = server_name
        if run_id:
            self.run_id = run_id
        elif turn_id:
            self.run_id = f"zcode_{turn_id}"
        else:
            self.run_id = "zcode"
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    def stamp(self, raw: ZCodeRawEvent) -> MCodeInternalEvent:
        """原始事件 -> 补齐标识的内部事件。``seq`` 单调递增。"""
        kind = str(raw.get("kind") or "")
        data = dict(raw.get("data") or {})
        self._seq += 1
        data.setdefault("source", self.source)
        data.setdefault("session_id", self.session_id)
        data.setdefault("turn_id", self.turn_id)
        data.setdefault("run_id", self.run_id)
        data.setdefault("server", self.server_name)
        data["seq"] = self._seq
        return MCodeInternalEvent(kind=kind, data=data)

    def stamp_all(self, raws: List[ZCodeRawEvent]) -> List[MCodeInternalEvent]:
        return [self.stamp(raw) for raw in raws]


def make_zcode_sink(
    adapter: ZCodeEventAdapter,
    dispatch: Any,
) -> Any:
    """把「原始事件 sink」接成「内部事件派发」。

    返回的协程供 :class:`ZCodeChatModel` 的 ``event_sink_provider`` 返回:
    模型侧给原始事件,这里补戳后逐条交给 ``dispatch``(即
    ``_dispatch_mcode_internal`` 的包装)。adapter/dispatch 的异常都不外抛 ——
    事件通道只是附加能力,不能拖垮模型调用。

    Args:
        adapter: 本轮的 :class:`ZCodeEventAdapter`。
        dispatch: ``async (internal: MCodeInternalEvent) -> None``。
    """
    async def sink(raw: ZCodeRawEvent) -> None:
        try:
            internal = adapter.stamp(raw)
        except Exception:  # noqa: BLE001
            logger.warning("zcode 事件补戳失败(已忽略)", exc_info=True)
            return
        try:
            result = dispatch(internal)
            if hasattr(result, "__await__"):
                await result
        except Exception:  # noqa: BLE001
            logger.warning("zcode 事件派发失败(已忽略)", exc_info=True)

    return sink
