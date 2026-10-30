"""把 ``mcode exec --output-format stream-json`` 的原始输出脱敏成可入库的 fixture。

用法
----
::

    python sanitize_stream_json.py <raw.ndjson> <sanitized.ndjson>

为什么需要这个脚本
----------------
``stream-json`` 原始输出里含有**真实敏感内容**:绝对路径、完整命令行、工具 stdout、
模型隐藏思维链。直接入库等于泄密。但协议**结构**(事件名、item 类型、item id 关系、
``toolCall.status`` 迁移、字段形状)必须原样保留,否则 fixture 就失去回归价值。

本脚本的取舍
------------
**保留结构,替换内容。** 具体规则:

============================  ==================================================
字段                          处理
============================  ==================================================
``sessionId`` / ``runId``     常量占位(``sess_redacted`` / ``run_redacted``)
``turnId``                    常量占位(``turn_redacted``)
``item.id`` / ``toolCall.id`` uuid → ``u1``/``u2``(同名共用一份映射);
                              ``call_xxx`` → ``call_1``/``call_2``
``contentDelta``              换成**等长**填充(``x`` × 原字符数),长度分布不变
``item.content``              由该 item 自己的 delta 拼接重建(等长填充)
``result.output``             由**最后一条** agent_message 的 delta 拼接重建
``toolCall.input/output``     递归替换所有**字符串**值为 ``<redacted>``,保留键与形状
``model.modelId``             ``<redacted>``(providerId 等保留)
``usage`` / ``durationMs``    原样保留(纯数字,无敏感信息)
``timestampMs``               原样保留
============================  ==================================================

等长填充是刻意的:它让「增量拼接 == 最终正文」这条不变量在 fixture 上**仍然成立**,
于是阶段 B 的回归用例可以直接对脱敏 fixture 跑,不需要第二份未脱敏文件。

不变量校验
----------
脚本会自己验证三件事,任何一条不成立就报错退出(而不是产出一份骗人的 fixture):

1. 同一条 agent_message 的 delta 拼接 == 它的 ``item.content``;
2. 最后一条 agent_message 的 ``item.content`` == ``exec.completed.result.output``;
3. 每行都能被 ``json.loads`` 解析。
"""
from __future__ import annotations

import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List

#: 敏感字符串的统一占位符。
REDACTED = "<redacted>"

#: 等长填充用的字符。
FILLER = "x"

#: 事件里需要打码的 envelope 字段。
_ENVELOPE_IDS = ("sessionId", "runId", "turnId")

#: envelope 占位值。
_ENVELOPE_PLACEHOLDERS = {
    "sessionId": "sess_redacted",
    "runId": "run_redacted",
    "turnId": "turn_redacted",
}


class SanitizeError(RuntimeError):
    """脱敏过程中的不一致。"""


def _filler(text: str) -> str:
    """把一段原文换成等长填充串。"""
    return FILLER * len(text)


def _redact_strings(value: Any) -> Any:
    """递归把 dict / list 里的**字符串**换成 ``<redacted>``,保留键与整体形状。

    非字符串标量(数字 / 布尔)保留 —— 它们不携带路径或命令原文。
    """
    if isinstance(value, str):
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_strings(v) for v in value]
    return value


class _IdMapper:
    """把真实 id 映射成稳定占位符,并保持「同一个 id 在不同事件里是同一个占位符」。

    这条很重要:``item.id`` 的 uuid 部分在 ``:message`` 与 ``:reasoning`` 两个 item
    之间是**共享**的,它标记的是「同一段 agent 输出」。打码后必须继续共享,
    否则「item 相对关系」这一 fixture 要点就丢了。
    """

    def __init__(self) -> None:
        self._map: Dict[str, str] = {}
        self._counters: Dict[str, int] = {"uuid": 0, "call": 0}

    def map(self, raw: str) -> str:
        if raw in self._map:
            return self._map[raw]
        if raw.startswith("call_"):
            self._counters["call"] += 1
            token = f"call_{self._counters['call']}"
        else:
            self._counters["uuid"] += 1
            token = f"u{self._counters['uuid']}"
        self._map[raw] = token
        return token


def _map_item_id(raw: str, mapper: _IdMapper) -> str:
    """``<uuid>:message`` → ``u1:message``;``call_xxx`` → ``call_1``。"""
    if ":" in raw:
        head, _, suffix = raw.rpartition(":")
        return f"{mapper.map(head)}:{suffix}"
    return mapper.map(raw)


def sanitize_line(obj: Dict[str, Any], mapper: _IdMapper) -> Dict[str, Any]:
    """脱敏单条事件。"""
    out: Dict[str, Any] = OrderedDict()
    for key, value in obj.items():
        if key in _ENVELOPE_IDS:
            out[key] = _ENVELOPE_PLACEHOLDERS[key]
        elif key == "item" and isinstance(value, dict):
            out[key] = _sanitize_item(value, mapper)
        elif key == "model" and isinstance(value, dict):
            out[key] = _sanitize_model(value)
        elif key == "result" and isinstance(value, dict):
            out[key] = _sanitize_result(value, mapper)
        else:
            out[key] = value
    return out


def _sanitize_model(model: Dict[str, Any]) -> Dict[str, Any]:
    """模型标识:只打码 ``modelId``,provider 之类保留。"""
    out = dict(model)
    if "modelId" in out:
        out["modelId"] = REDACTED
    return out


def _sanitize_item(item: Dict[str, Any], mapper: _IdMapper) -> Dict[str, Any]:
    """脱敏 item。``content`` / ``contentDelta`` 在这里先原样留着,随后由调用方重建。"""
    out: Dict[str, Any] = OrderedDict()
    for key, value in item.items():
        if key == "id" and isinstance(value, str):
            out[key] = _map_item_id(value, mapper)
        elif key == "toolCall" and isinstance(value, dict):
            out[key] = _sanitize_tool_call(value, mapper)
        elif key in ("contentDelta", "content"):
            out[key] = value  # 后续统一重建
        else:
            out[key] = value
    return out


def _sanitize_tool_call(tool_call: Dict[str, Any], mapper: _IdMapper) -> Dict[str, Any]:
    """脱敏 toolCall:id 打码,input / output / error 的字符串值全打码。"""
    out: Dict[str, Any] = OrderedDict()
    for key, value in tool_call.items():
        if key == "id" and isinstance(value, str):
            out[key] = mapper.map(value)
        elif key in ("input", "output", "error"):
            out[key] = _redact_strings(value)
        else:
            out[key] = value
    return out


def _sanitize_result(result: Dict[str, Any], mapper: _IdMapper) -> Dict[str, Any]:
    """脱敏 exec.completed 的 result。``output`` 稍后由最终消息重建。"""
    out: Dict[str, Any] = OrderedDict()
    for key, value in result.items():
        if key in _ENVELOPE_IDS:
            out[key] = _ENVELOPE_PLACEHOLDERS[key]
        elif key == "model" and isinstance(value, dict):
            out[key] = _sanitize_model(value)
        elif key == "output":
            out[key] = value  # 后续重建
        else:
            out[key] = value
    return out


def sanitize_stream(raw_lines: List[str]) -> List[str]:
    """脱敏整条流。返回新的 NDJSON 行列表。"""
    events: List[Dict[str, Any]] = []
    for lineno, raw in enumerate(raw_lines, start=1):
        text = raw.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SanitizeError(f"第 {lineno} 行不是合法 JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise SanitizeError(f"第 {lineno} 行不是 JSON object")
        events.append(obj)

    # --- 1. 收集每条 item 的 delta 拼接(原文),用于重建 content / output ---
    deltas: Dict[str, List[str]] = {}
    item_types: Dict[str, str] = {}
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str):
            continue
        item_types[item_id] = str(item.get("type") or "")
        delta = item.get("contentDelta")
        if isinstance(delta, str) and delta:
            deltas.setdefault(item_id, []).append(delta)

    def join_deltas(item_id: str) -> str:
        return "".join(deltas.get(item_id, []))

    # --- 2. 重建 contentDelta / content(等长填充) ---
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str):
            continue
        raw_delta = item.get("contentDelta")
        if isinstance(raw_delta, str) and raw_delta:
            item["contentDelta"] = _filler(raw_delta)
        if "content" in item and isinstance(item.get("content"), str):
            raw_content = item["content"]
            joined = join_deltas(item_id)
            if joined and joined != raw_content:
                raise SanitizeError(
                    f"item {item_id} 的 delta 拼接与 content 不一致,拒绝产出 fixture"
                )
            item["content"] = _filler(raw_content)

    # --- 3. 重建 result.output ---
    message_ids = [
        item_id
        for item_id in item_types
        if item_types[item_id] == "agent_message"
    ]
    final_output_raw = ""
    if message_ids:
        last_id = message_ids[-1]
        final_output_raw = join_deltas(last_id)

    for event in events:
        if event.get("type") != "exec.completed":
            continue
        result = event.get("result")
        if not isinstance(result, dict):
            continue
        raw_output = result.get("output")
        if not isinstance(raw_output, str):
            continue
        if message_ids:
            final_content = join_deltas(message_ids[-1])
            if final_content and raw_output != final_content:
                raise SanitizeError(
                    "最后一条 agent_message 的 content 与 exec.completed.result.output "
                    "不一致,拒绝产出 fixture"
                )
            result["output"] = _filler(final_output_raw)
        else:
            result["output"] = _filler(raw_output)

    # --- 4. 逐行脱敏 ---
    mapper = _IdMapper()
    lines: List[str] = []
    for event in events:
        sanitized = sanitize_line(event, mapper)
        lines.append(json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")))
    return lines


def main(argv: List[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        print("用法: python sanitize_stream_json.py <raw.ndjson> <sanitized.ndjson>")
        return 2
    src = Path(argv[1])
    dst = Path(argv[2])
    raw_lines = src.read_text(encoding="utf-8").splitlines()
    try:
        out_lines = sanitize_stream(raw_lines)
    except SanitizeError as exc:
        print(f"脱敏失败: {exc}", file=sys.stderr)
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"{src.name} ({len(raw_lines)} 行) -> {dst.name} ({len(out_lines)} 行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
