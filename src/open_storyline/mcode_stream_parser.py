"""把 ``mcode exec --output-format stream-json`` 的 NDJSON 行解析成 :class:`MCodeEvent`。

协议形状
--------
``stream-json`` 是一行一个 JSON object(NDJSON)。每行都有同一个 envelope:

.. code-block:: json

    {"schemaVersion":1,"sequence":7,"timestampMs":1793384654561,
     "runId":"...","sessionId":"...","turnId":"...","type":"item.updated","item":{...}}

事件顺序是**强不变量**::

    exec.started → session.started|session.resumed → turn.started
      → item.* (可多轮) → turn.completed|turn.failed → exec.completed(恒为最后一条)

三个必须记住的坑
----------------
1. **首段文本在 ``item.started`` 里**,不在 ``item.updated`` 里。
   只认 ``item.updated`` 会丢掉每条消息的第一段。
2. **工具生命周期由 ``toolCall.status`` 驱动**,不是由 ``item.started`` /
   ``item.completed`` 配对驱动。
3. **``reasoning`` 是隐藏思维链原文**,解析层直接丢弃,原文不进事件对象。

用法
----
::

    parser = MCodeStreamParser()
    for chunk in chunks:                 # chunk 可以是任意切片
        for event in parser.feed(chunk):
            ...
    for event in parser.close():         # 收尾:处理最后一行残留
        ...
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional

from open_storyline.mcode_events import (
    EVENT_EXEC_COMPLETED,
    EVENT_TURN_FAILED,
    ITEM_EVENT_TYPES,
    KNOWN_EVENT_TYPES,
    SCHEMA_VERSION,
    MCodeEvent,
    MCodeItem,
    MCodeSchemaError,
    MCodeToolCall,
    tool_status_from_value,
)

logger = logging.getLogger(__name__)

#: 诊断信息最多留多少条(防止异常流把内存吃光)。
MAX_DIAGNOSTICS = 200


class MCodeStreamParser:
    """NDJSON → :class:`MCodeEvent` 的增量解析器。

    解析器是**有状态**的:它会记住上一次 ``feed`` 留下的半行,
    下一次 ``feed`` 补齐后再解析。所以一行被切成两半不会误报无效 JSON。

    遇到下列情况**不会**抛错,只记诊断并跳过:

    * 这一行不是合法 JSON(``strict_json=True`` 时除外);
    * 这一行**没有** ``schemaVersion``(不是 stream-json 事件,比如 CLI 混进来的日志行);
    * 事件 ``type`` 未知(上游新增了事件类型);
    * ``item`` 缺少合法的 ``id`` / ``type``;
    * ``item.type`` 是 ``reasoning``(隐藏思维链,直接丢弃)。

    只有**协议不兼容**才抛 :class:`MCodeSchemaError`:
    ``schemaVersion`` 存在但不是 1、envelope 字段缺失、``exec.completed`` 没有合法 ``result``。

    ``schemaVersion`` **缺失**与**版本对不上**要分开看:
    缺失只是「这不是协议事件」,跳过即可;版本对不上说明上游改了协议,
    必须明确失败 —— 绝不能当成空文本继续跑。
    """

    def __init__(
        self,
        *,
        expected_schema_version: int = SCHEMA_VERSION,
        require_envelope_ids: bool = True,
        strict_json: bool = False,
    ) -> None:
        self._expected_schema_version = expected_schema_version
        self._require_envelope_ids = require_envelope_ids
        self._strict_json = strict_json
        self._buffer = ""
        self._diagnostics: List[str] = []
        self._stats: Dict[str, int] = {
            "lines": 0,
            "events": 0,
            "reasoning_dropped": 0,
            "unknown_type": 0,
            "unknown_item_type": 0,
            "bad_json": 0,
            "bad_item": 0,
            "not_protocol": 0,
        }

    # -- 对外属性 --------------------------------------------------------

    @property
    def diagnostics(self) -> List[str]:
        """已记录的诊断信息(截断到 :data:`MAX_DIAGNOSTICS` 条)。"""
        return list(self._diagnostics)

    @property
    def stats(self) -> Dict[str, int]:
        """计数器。``reasoning_dropped`` 用来确认「思维链确实被丢弃了」。"""
        return dict(self._stats)

    @property
    def pending(self) -> str:
        """当前还没凑满一行的残留内容。"""
        return self._buffer

    # -- 入口 ------------------------------------------------------------

    def feed(self, chunk: str) -> List[MCodeEvent]:
        """喂一段任意切片的文本,返回本次能解析出的事件。

        ``chunk`` 不需要以换行结尾 —— 半个行会留在缓冲区里等下一次。
        """
        if not chunk:
            return []
        self._buffer += chunk
        events: List[MCodeEvent] = []
        while True:
            newline = self._buffer.find("\n")
            if newline < 0:
                break
            line = self._buffer[:newline]
            self._buffer = self._buffer[newline + 1 :]
            events.extend(self.feed_line(line))
        return events

    def close(self) -> List[MCodeEvent]:
        """收尾:把缓冲区里剩下的内容当作最后一行处理。"""
        if not self._buffer:
            return []
        line = self._buffer
        self._buffer = ""
        return self.feed_line(line)

    def feed_line(self, line: str) -> List[MCodeEvent]:
        """解析单行(不含换行符)。"""
        text = line.strip()
        if not text:
            return []
        self._stats["lines"] += 1
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            self._stats["bad_json"] += 1
            if self._strict_json:
                raise MCodeSchemaError(f"stream-json 行不是合法 JSON: {exc}") from exc
            self._diagnose(f"跳过非 JSON 行: {exc}")
            return []
        if not isinstance(obj, dict):
            self._stats["bad_json"] += 1
            self._diagnose("跳过非 JSON object 的行")
            return []
        event = self._build_event(obj)
        if event is None:
            return []
        self._stats["events"] += 1
        return [event]

    # -- 内部 ------------------------------------------------------------

    def _diagnose(self, message: str) -> None:
        if len(self._diagnostics) < MAX_DIAGNOSTICS:
            self._diagnostics.append(message)
        logger.debug("mcode stream 诊断: %s", message)

    def _build_event(self, obj: Mapping[str, Any]) -> Optional[MCodeEvent]:
        """把一条原始 JSON 变成事件。返回 ``None`` 表示「跳过」。"""
        # 1. 协议版本 —— 不兼容就是硬错误,不能当空文本。
        version = obj.get("schemaVersion")
        if version is None:
            # 这一行根本不是 stream-json 事件(可能是 CLI 混进来的日志行,
            # 也可能是 --output-format json 模式的 exec.result)。
            # 跳过 + 记诊断,**不**当成协议不兼容 —— 否则一行噪声就能打断整条流。
            self._stats["not_protocol"] += 1
            self._diagnose("跳过没有 schemaVersion 的行(不是 stream-json 事件)")
            return None
        if version != self._expected_schema_version:
            raise MCodeSchemaError(
                f"stream-json 协议版本不兼容: 期望 schemaVersion="
                f"{self._expected_schema_version},实际 {version!r}"
            )

        # 2. 事件类型
        event_type = obj.get("type")
        if not isinstance(event_type, str) or not event_type:
            self._diagnose("跳过没有 type 的事件")
            return None
        if event_type not in KNOWN_EVENT_TYPES:
            self._stats["unknown_type"] += 1
            self._diagnose(f"跳过未知事件类型: {event_type}")
            return None

        # 3. envelope
        session_id = self._require_str(obj, "sessionId")
        run_id = self._require_str(obj, "runId")
        turn_id = self._require_str(obj, "turnId")
        sequence = obj.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            if self._require_envelope_ids:
                raise MCodeSchemaError(
                    f"事件 {event_type} 缺少合法的 sequence 字段"
                )
            sequence = 0
        timestamp_ms = obj.get("timestampMs")
        if not isinstance(timestamp_ms, int) or isinstance(timestamp_ms, bool):
            timestamp_ms = 0

        # 4. item
        item: Optional[MCodeItem] = None
        if event_type in ITEM_EVENT_TYPES:
            item = self._build_item(obj.get("item"))
            if item is None:
                return None
            if item.is_reasoning:
                # 隐藏思维链:整条丢掉,原文不进内存对象。
                self._stats["reasoning_dropped"] += 1
                self._diagnose("丢弃 reasoning 事件(隐藏思维链不透传)")
                return None

        # 5. 收尾事件的结果体
        result: Optional[Mapping[str, Any]] = None
        status: Optional[str] = None
        error: Optional[Any] = None
        if event_type == EVENT_EXEC_COMPLETED:
            raw_result = obj.get("result")
            if not isinstance(raw_result, Mapping):
                raise MCodeSchemaError("exec.completed 缺少合法的 result")
            result = dict(raw_result)
            raw_status = result.get("status")
            status = raw_status if isinstance(raw_status, str) else None
        elif event_type == EVENT_TURN_FAILED:
            status = self._require_str(obj, "status") or "failed"
            error = obj.get("error") or obj.get("message") or obj.get("reason")

        model = obj.get("model") if isinstance(obj.get("model"), Mapping) else None
        usage = obj.get("usage") if isinstance(obj.get("usage"), Mapping) else None
        duration_ms = obj.get("durationMs")
        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool):
            duration_ms = None

        return MCodeEvent(
            type=event_type,
            sequence=sequence,
            timestamp_ms=timestamp_ms,
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
            schema_version=self._expected_schema_version,
            item=item,
            result=result,
            model=dict(model) if model else None,
            usage=dict(usage) if usage else None,
            duration_ms=duration_ms,
            status=status,
            error=error,
            raw=dict(obj),
        )

    def _require_str(self, obj: Mapping[str, Any], key: str) -> str:
        """取一个必需的字符串字段。缺失就抛协议错误(不静默填空串)。"""
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
        if not self._require_envelope_ids:
            return ""
        raise MCodeSchemaError(f"事件缺少必需的 {key} 字段: {obj.get('type')!r}")

    def _build_item(self, raw: Any) -> Optional[MCodeItem]:
        """构造 item。缺合法 ``id`` / ``type`` 就跳过(不抛错)。"""
        if not isinstance(raw, Mapping):
            self._stats["bad_item"] += 1
            self._diagnose("跳过没有 item 对象的事件")
            return None
        item_id = raw.get("id")
        item_type = raw.get("type")
        if not isinstance(item_id, str) or not item_id:
            self._stats["bad_item"] += 1
            self._diagnose("跳过没有合法 item.id 的事件")
            return None
        if not isinstance(item_type, str) or not item_type:
            self._stats["bad_item"] += 1
            self._diagnose(f"跳过 item.id={item_id} 没有合法 item.type 的事件")
            return None

        content_delta = raw.get("contentDelta")
        if not isinstance(content_delta, str):
            content_delta = ""
        content = raw.get("content")
        if not isinstance(content, str):
            content = None

        tool_call = None
        raw_tool_call = raw.get("toolCall")
        if isinstance(raw_tool_call, Mapping):
            tool_call = self._build_tool_call(raw_tool_call, item_id)

        return MCodeItem(
            id=item_id,
            type=item_type,
            content_delta=content_delta,
            content=content,
            tool_call=tool_call,
        )

    def _build_tool_call(self, raw: Mapping[str, Any], item_id: str) -> MCodeToolCall:
        """构造 toolCall。``status`` 认不出来就留 ``None``,不打断整条流。"""
        call_id = raw.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = item_id
        name = raw.get("name")
        if not isinstance(name, str):
            name = ""
        raw_status = raw.get("status")
        if isinstance(raw_status, bool) or not isinstance(raw_status, int):
            raw_status = None
        return MCodeToolCall(
            id=call_id,
            name=name,
            status=tool_status_from_value(raw_status),
            raw_status=raw_status,
            input=raw.get("input"),
            output=raw.get("output"),
            error=raw.get("error"),
        )


# --------------------------------------------------------------------------


def parse_ndjson(text: str, **kwargs: Any) -> List[MCodeEvent]:
    """解析整段 NDJSON 文本。"""
    parser = MCodeStreamParser(**kwargs)
    events = parser.feed(text)
    events.extend(parser.close())
    return events


def parse_ndjson_lines(lines: Iterable[str], **kwargs: Any) -> List[MCodeEvent]:
    """解析一批 NDJSON 行。

    每个元素按「一整行」处理:带不带结尾换行都行。
    """
    parser = MCodeStreamParser(**kwargs)
    events: List[MCodeEvent] = []
    for line in lines:
        if line.endswith("\n"):
            events.extend(parser.feed(line))
        else:
            events.extend(parser.feed(line + "\n"))
    events.extend(parser.close())
    return events


__all__ = [
    "MCodeStreamParser",
    "parse_ndjson",
    "parse_ndjson_lines",
    "MAX_DIAGNOSTICS",
]
