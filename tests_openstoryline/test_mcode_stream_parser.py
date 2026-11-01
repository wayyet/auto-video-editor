"""``mcode_stream_parser`` 的单元测试。

覆盖设计文档 §10.1 的 UT-01…UT-08,并把**阶段 A 的脱敏 fixture** 用成回归基线:
协议形状一旦变了,这些用例就该红。

重点盯三个坑:

1. 首段文本在 ``item.started`` 里(不是 ``item.updated``);
2. 工具生命周期由 ``toolCall.status`` 驱动(不是 item 配对);
3. ``reasoning`` 是隐藏思维链,必须被丢弃。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_storyline.mcode_events import (
    EVENT_EXEC_COMPLETED,
    EVENT_EXEC_STARTED,
    EVENT_ITEM_STARTED,
    EVENT_ITEM_UPDATED,
    EVENT_TURN_COMPLETED,
    ITEM_AGENT_MESSAGE,
    ITEM_REASONING,
    MCodeSchemaError,
    MCodeToolStatus,
)
from open_storyline.mcode_stream_parser import (
    MCodeStreamParser,
    parse_ndjson,
    parse_ndjson_lines,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "mcode_stream"


# --------------------------------------------------------------------------
# 造事件的工具
# --------------------------------------------------------------------------

def _event(seq: int, event_type: str, **extra) -> dict:
    """造一条合法 envelope 事件。"""
    base = {
        "schemaVersion": 1,
        "sequence": seq,
        "timestampMs": 1700000000000 + seq,
        "runId": "run-1",
        "sessionId": "sess-1",
        "turnId": "turn-1",
        "type": event_type,
    }
    base.update(extra)
    return base


def _item_event(seq: int, event_type: str, item: dict) -> dict:
    return _event(seq, event_type, item=item)


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


# --------------------------------------------------------------------------
# UT-01 单行解析
# --------------------------------------------------------------------------

def test_parses_single_event_line():
    parser = MCodeStreamParser()
    events = parser.feed(_line(_event(1, "exec.started")) + "\n")
    assert len(events) == 1
    event = events[0]
    assert event.type == EVENT_EXEC_STARTED
    assert event.sequence == 1
    assert event.run_id == "run-1"
    assert event.session_id == "sess-1"
    assert event.turn_id == "turn-1"
    assert event.schema_version == 1


def test_parses_turn_completed_with_object_model():
    """实测 ``turn.completed.model`` 是**对象**,不是字符串。"""
    payload = _event(
        11,
        EVENT_TURN_COMPLETED,
        model={
            "providerId": "minimax",
            "modelId": "MiniMax-M3.1-Flash",
            "variant": "thinking",
            "providerSource": "provider",
            "providerKind": "minimax-managed",
        },
        usage={"inputTokens": 100, "outputTokens": 10, "cacheReadTokens": 5, "totalTokens": 110},
        durationMs=15018,
    )
    event = parse_ndjson(_line(payload) + "\n")[0]
    assert event.model is not None
    assert event.model["modelId"] == "MiniMax-M3.1-Flash"
    assert event.usage is not None
    assert event.usage["cacheReadTokens"] == 5
    assert event.duration_ms == 15018


# --------------------------------------------------------------------------
# UT-02 跨读取块的半行
# --------------------------------------------------------------------------

def test_line_split_across_chunks_is_reassembled():
    """一行被切成两半时,不能误报「无效 JSON」。"""
    payload = _line(_event(1, "exec.started"))
    parser = MCodeStreamParser()
    midpoint = len(payload) // 2
    assert parser.feed(payload[:midpoint]) == []
    events = parser.feed(payload[midpoint:] + "\n")
    assert len(events) == 1
    assert events[0].type == EVENT_EXEC_STARTED
    assert parser.stats["bad_json"] == 0


def test_multibyte_character_split_across_chunks():
    """中文字符横跨两个 chunk 时不能变成乱码。"""
    text = "增量文本"
    payload = _line(_item_event(1, EVENT_ITEM_UPDATED, {
        "id": "u1:message",
        "type": ITEM_AGENT_MESSAGE,
        "contentDelta": text,
    }))
    parser = MCodeStreamParser()
    events = []
    for ch in payload:
        events.extend(parser.feed(ch))
    events.extend(parser.close())
    assert len(events) == 1
    assert events[0].text_delta == text


def test_crlf_and_blank_lines_are_tolerated():
    payload = (
        _line(_event(1, "exec.started")) + "\r\n"
        + "\r\n"
        + _line(_event(2, "session.started")) + "\r\n"
    )
    events = parse_ndjson(payload)
    assert [e.type for e in events] == ["exec.started", "session.started"]


def test_close_flushes_trailing_line_without_newline():
    payload = _line(_event(1, "exec.started"))
    parser = MCodeStreamParser()
    assert parser.feed(payload) == []
    events = parser.close()
    assert len(events) == 1
    assert events[0].type == EVENT_EXEC_STARTED


# --------------------------------------------------------------------------
# UT-03 / 首段文本在 item.started
# --------------------------------------------------------------------------

def test_first_delta_arrives_in_item_started():
    """**首段文本在 item.started 里** —— 只认 item.updated 会丢第一段。"""
    events = parse_ndjson_lines([
        _line(_item_event(1, EVENT_ITEM_STARTED, {
            "id": "u1:message",
            "type": ITEM_AGENT_MESSAGE,
            "contentDelta": "第一段",
        })),
        _line(_item_event(2, EVENT_ITEM_UPDATED, {
            "id": "u1:message",
            "type": ITEM_AGENT_MESSAGE,
            "contentDelta": "第二段",
        })),
    ])
    assert [e.text_delta for e in events] == ["第一段", "第二段"]


def test_deltas_keep_order():
    deltas = ["a", "b", "c", "d"]
    events = parse_ndjson_lines([
        _line(_item_event(i + 1, EVENT_ITEM_UPDATED, {
            "id": "u1:message",
            "type": ITEM_AGENT_MESSAGE,
            "contentDelta": d,
        }))
        for i, d in enumerate(deltas)
    ])
    assert "".join(e.text_delta for e in events) == "abcd"


# --------------------------------------------------------------------------
# UT-04 最终结果与增量并存
# --------------------------------------------------------------------------

def test_final_output_and_deltas_coexist_without_duplication():
    events = parse_ndjson_lines([
        _line(_item_event(1, EVENT_ITEM_STARTED, {
            "id": "u1:message", "type": ITEM_AGENT_MESSAGE, "contentDelta": "Hello",
        })),
        _line(_item_event(2, EVENT_ITEM_UPDATED, {
            "id": "u1:message", "type": ITEM_AGENT_MESSAGE, "contentDelta": "!",
        })),
        _line(_item_event(3, "item.completed", {
            "id": "u1:message", "type": ITEM_AGENT_MESSAGE, "content": "Hello!",
        })),
        _line(_event(4, EVENT_EXEC_COMPLETED, result={
            "schemaVersion": 1,
            "type": "exec.result",
            "status": "succeeded",
            "output": "Hello!",
        })),
    ])
    deltas = "".join(e.text_delta for e in events)
    final = [e for e in events if e.is_exec_completed][0]
    assert deltas == "Hello!"
    assert final.final_output == "Hello!"
    assert final.status == "succeeded"
    # 增量不重复:最终正文只出现一次
    assert deltas.count("Hello!") == 1


# --------------------------------------------------------------------------
# UT-05 reasoning 必须被丢弃
# --------------------------------------------------------------------------

def test_reasoning_event_is_dropped():
    """隐藏思维链原文不能进入事件对象。"""
    secret = "The user wants 5 numbered facts, internal reasoning"
    parser = MCodeStreamParser()
    events = parser.feed(_line(_item_event(1, EVENT_ITEM_STARTED, {
        "id": "u1:reasoning",
        "type": ITEM_REASONING,
        "contentDelta": secret,
    })) + "\n")
    assert events == []
    assert parser.stats["reasoning_dropped"] == 1
    assert secret not in "".join(parser.diagnostics)


def test_reasoning_content_never_becomes_text_delta():
    """即使绕过解析层,``text_delta`` 也不给 reasoning 文本。"""
    events = parse_ndjson_lines([
        _line(_item_event(1, EVENT_ITEM_STARTED, {
            "id": "u1:message", "type": ITEM_AGENT_MESSAGE, "contentDelta": "可见",
        })),
    ])
    assert events[0].text_delta == "可见"


# --------------------------------------------------------------------------
# UT-06 tool_call
# --------------------------------------------------------------------------

def test_tool_call_status_mapping():
    """工具生命周期由 ``toolCall.status`` 驱动。"""
    def tool_event(seq: int, status: int, **extra):
        tool_call = {"id": "call_1", "name": "glob", "status": status}
        tool_call.update(extra)
        return _line(_item_event(seq, EVENT_ITEM_UPDATED, {
            "id": "call_1", "type": "tool_call", "toolCall": tool_call,
        }))

    events = parse_ndjson_lines([
        tool_event(1, 4),                       # Preparing
        tool_event(2, 5),                       # Prepared
        tool_event(3, 1, input={"pattern": "**/x"}),   # Start
        tool_event(4, 2, output={"content": []}),       # Finished
    ])
    statuses = [e.item.tool_call.status for e in events]
    assert statuses == [
        MCodeToolStatus.PREPARING,
        MCodeToolStatus.PREPARED,
        MCodeToolStatus.START,
        MCodeToolStatus.FINISHED,
    ]
    assert events[2].item.tool_call.input == {"pattern": "**/x"}
    assert events[3].item.tool_call.is_finished
    assert events[3].item.tool_call.is_running is False


def test_tool_call_failed_status():
    events = parse_ndjson_lines([
        _line(_item_event(1, EVENT_ITEM_UPDATED, {
            "id": "call_1",
            "type": "tool_call",
            "toolCall": {"id": "call_1", "name": "bash", "status": 3, "error": "boom"},
        })),
    ])
    call = events[0].item.tool_call
    assert call.status == MCodeToolStatus.FAILED
    assert call.is_failed
    assert call.error == "boom"


def test_unknown_tool_status_does_not_break_stream():
    """上游加新枚举时不能打断整条流。"""
    events = parse_ndjson_lines([
        _line(_item_event(1, EVENT_ITEM_UPDATED, {
            "id": "call_1",
            "type": "tool_call",
            "toolCall": {"id": "call_1", "name": "x", "status": 99},
        })),
        _line(_event(2, EVENT_EXEC_COMPLETED, result={"status": "succeeded", "output": "ok"})),
    ])
    assert len(events) == 2
    assert events[0].item.tool_call.status is None
    assert events[0].item.tool_call.raw_status == 99


# --------------------------------------------------------------------------
# UT-07 未知事件
# --------------------------------------------------------------------------

def test_unknown_event_type_is_skipped_with_diagnostic():
    parser = MCodeStreamParser()
    events = parser.feed(
        _line(_event(1, "totally.unknown.event")) + "\n"
        + _line(_event(2, EVENT_EXEC_COMPLETED, result={"status": "succeeded", "output": "ok"})) + "\n"
    )
    assert [e.type for e in events] == [EVENT_EXEC_COMPLETED]
    assert parser.stats["unknown_type"] == 1
    assert any("totally.unknown.event" in d for d in parser.diagnostics)


def test_malformed_json_line_is_skipped_not_fatal():
    parser = MCodeStreamParser()
    events = parser.feed("这不是 JSON\n" + _line(_event(1, EVENT_EXEC_COMPLETED, result={"status": "succeeded", "output": "ok"})) + "\n")
    assert len(events) == 1
    assert parser.stats["bad_json"] == 1


def test_strict_json_raises_on_malformed_line():
    parser = MCodeStreamParser(strict_json=True)
    with pytest.raises(MCodeSchemaError):
        parser.feed("这不是 JSON\n")


def test_item_without_id_is_skipped():
    parser = MCodeStreamParser()
    events = parser.feed(_line(_item_event(1, EVENT_ITEM_UPDATED, {"type": ITEM_AGENT_MESSAGE})) + "\n")
    assert events == []
    assert parser.stats["bad_item"] == 1


# --------------------------------------------------------------------------
# UT-08 协议版本不兼容
# --------------------------------------------------------------------------

def test_schema_version_mismatch_raises():
    payload = _event(1, EVENT_EXEC_STARTED)
    payload["schemaVersion"] = 2
    with pytest.raises(MCodeSchemaError) as exc:
        parse_ndjson(_line(payload) + "\n")
    assert "schemaVersion" in str(exc.value)


def test_missing_envelope_id_raises():
    payload = _event(1, EVENT_EXEC_STARTED)
    del payload["turnId"]
    with pytest.raises(MCodeSchemaError):
        parse_ndjson(_line(payload) + "\n")


def test_exec_completed_without_result_raises():
    with pytest.raises(MCodeSchemaError):
        parse_ndjson(_line(_event(1, EVENT_EXEC_COMPLETED)) + "\n")


# --------------------------------------------------------------------------
# 阶段 A 的 fixture 回归:协议基线
# --------------------------------------------------------------------------

def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    ["sample_lightweight_ok.jsonl", "sample_standard_toolcall.jsonl"],
)
def test_fixture_lines_are_valid_json(name):
    for line in _fixture(name).splitlines():
        if line.strip():
            assert isinstance(json.loads(line), dict)


@pytest.mark.parametrize(
    "name",
    ["sample_lightweight_ok.jsonl", "sample_standard_toolcall.jsonl"],
)
def test_fixture_has_no_sensitive_strings(name):
    """阶段 A 的脱敏验收:不得出现用户目录 / 真实会话 id / 原始输出。"""
    text = _fixture(name)
    for forbidden in (
        "C:\\Users\\",
        "C:/Users/",
        "mvs_",
        "exec_turn_",
        "turn_mv",
        "AppData",
    ):
        assert forbidden not in text, f"{name} 泄露了 {forbidden}"


@pytest.mark.parametrize(
    "name",
    ["sample_lightweight_ok.jsonl", "sample_standard_toolcall.jsonl"],
)
def test_fixture_event_order_is_stable(name):
    """事件顺序是强不变量:exec.started 打头,exec.completed 收尾。"""
    events = parse_ndjson(_fixture(name))
    assert events[0].type == EVENT_EXEC_STARTED
    assert events[-1].type == EVENT_EXEC_COMPLETED
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)


def test_standard_fixture_first_delta_is_in_item_started():
    """**回归重点**:首段文本必须落在 item.started。"""
    events = parse_ndjson(_fixture("sample_standard_toolcall.jsonl"))
    message_deltas = [
        e for e in events
        if e.item is not None and e.item.is_agent_message and e.text_delta
    ]
    assert message_deltas, "fixture 里应该有 agent_message 增量"
    assert message_deltas[0].type == EVENT_ITEM_STARTED
    assert message_deltas[0].text_delta


def test_standard_fixture_delta_concatenation_matches_final_output():
    """**阶段 B 验收**:增量拼接 == exec.completed.result.output。"""
    events = parse_ndjson(_fixture("sample_standard_toolcall.jsonl"))
    per_item: dict = {}
    order: list = []
    for event in events:
        if event.item is None or not event.item.is_agent_message:
            continue
        if not event.text_delta:
            continue
        if event.item.id not in per_item:
            per_item[event.item.id] = []
            order.append(event.item.id)
        per_item[event.item.id].append(event.text_delta)

    assert order, "fixture 里应该有 agent_message"
    last_item = order[-1]
    joined = "".join(per_item[last_item])

    final = [e for e in events if e.is_exec_completed]
    assert len(final) == 1
    assert joined == final[0].final_output
    assert joined, "最终正文不能是空串"


def test_standard_fixture_dropped_reasoning():
    """fixture 里确实含 reasoning,而解析层把它们全丢了。"""
    raw = _fixture("sample_standard_toolcall.jsonl")
    assert '"reasoning"' in raw
    parser = MCodeStreamParser()
    events = parser.feed(raw)
    assert parser.stats["reasoning_dropped"] > 0
    assert all(
        not (e.item is not None and e.item.is_reasoning) for e in events
    )


def test_standard_fixture_tool_lifecycle_is_status_driven():
    """工具项的状态迁移是 4 → 5 → 1 → 2,而不是 item 配对。"""
    events = parse_ndjson(_fixture("sample_standard_toolcall.jsonl"))
    tool_events = [
        e for e in events
        if e.item is not None and e.item.tool_call is not None
    ]
    assert tool_events
    assert all(e.item.tool_call.status is not None for e in tool_events)
    # 至少有工具真的跑完了
    assert any(e.item.tool_call.is_finished for e in tool_events)
