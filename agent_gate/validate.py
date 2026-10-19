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


def validate_result(schema: Optional[dict[str, Any]], value: Any) -> list[str]:
    """返回错误列表;空列表 = 合格。schema 为空或自身非法时不校验。"""
    if not schema:
        return []
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError:
        return []
    errors = []
    for err in sorted(Draft7Validator(schema).iter_errors(value), key=lambda e: list(e.path)):
        where = "$" + "".join(
            f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.path
        )
        errors.append(f"{where}: {err.message}")
    return errors[:10]
