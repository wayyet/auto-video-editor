"""校验 Agent 提交的结果:先解析 JSON,再用 JSON Schema(draft-07)检查。"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class NotJSON(ValueError):
    """提交的文本不是合法 JSON。"""


class InvalidSchema(ValueError):
    """任务自带的 json_schema 本身不是合法的 draft-07 schema。

    这是**代码 bug**,不是 Agent 的错:schema 非法意味着校验根本不可信。
    所以这里 fail closed —— 抛异常,由调用方直接把任务关掉,而不是静默放行。
    """


def parse_json_value(text: str) -> Any:
    """严格解析 JSON;只容忍一层 ``` 代码围栏。解析失败抛 NotJSON。"""
    s = text.lstrip("\ufeff").strip()   # 开头的 BOM 不算内容
    m = _FENCE.match(s)
    if m:
        s = m.group(1)
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise NotJSON(f"不是合法 JSON:{e.msg}(第 {e.lineno} 行第 {e.colno} 列)") from e


def check_schema(schema: Optional[dict[str, Any]]) -> None:
    """schema 非空但自身非法 → 抛 :class:`InvalidSchema`。合法或为空则静默通过。"""
    if not schema:
        return
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as e:
        raise InvalidSchema(f"json_schema 不是合法的 draft-07 schema:{e.message}") from e


def validate_result(schema: Optional[dict[str, Any]], value: Any) -> list[str]:
    """返回错误列表;空列表 = 合格。

    schema 为空 → 不校验(纯文本任务)。
    schema 非法 → 抛 :class:`InvalidSchema`(**不再**静默返回 [] 当作通过)。
    """
    check_schema(schema)
    if not schema:
        return []
    errors = []
    for err in sorted(Draft7Validator(schema).iter_errors(value), key=lambda e: list(e.path)):
        where = "$" + "".join(
            f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.path
        )
        errors.append(f"{where}: {err.message}")
    return errors[:10]
