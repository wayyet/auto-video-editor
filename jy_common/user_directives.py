"""用户剪辑要求【】标记解析器(第八期·输入剪辑要求逐条生效,阶段1)。

设计文档: docs/第八期开发/auto-video-editor_输入剪辑要求逐条生效_设计审查与
分阶段执行计划_20261119.md §6.1(修订版,吸收 P1-05/P1-06/P2-01 审查结论)。

纯函数模块:不访问网络、不读写文件、不依赖 LangGraph。消费方
(``nodes/storyline/_common.py`` 检索词构造、``nodes/node_09_inject_fx.py``
开场转场加强、``nodes/storyline/node_generate_script.py`` 标题写入)只拿到
确定性结果,解析失败一律 fail-closed 回退既有行为。

契约(修订版 §6.1):
- 只识别四个**行首**标记:【主题】【开场】【检索】【标题】(允许前置空白);
- 字段值 = 标记行剩余部分 + 后续行,直到下一个**已知**行首标记或文末;
- 未知【xx】不是标记,作为普通文本留在当前字段值里,不解释为新功能;
- 值统一 ``strip()``;空值 = 未提供(键值为 ``None``),不写空串覆盖回退链;
- 同名标记重复 → 该字段无效(fail-closed)+ 解析警告,不静默取第一条/最后一条;
- 不带任何已知标记 → 解析结果为空,**不做自由文本兜底识别**(修订版 P1-06:
  无标记 = 行为与现状完全一致,自由文本意图识别留待二期另设开关);
- 解析诊断(重复字段警告)只进产物文件(如 ``script.json``),**不进**监控事件
  payload —— 创建台「提示词不进入监控事件」安全契约继续保留。
"""
from __future__ import annotations

import re
from typing import Any

from jy_common.hook_title import HOOK_TITLE_MAX_LEN

# 支持的行首标记 → 规范化字段名
FIELD_KEYS: dict[str, str] = {
    "主题": "theme",
    "开场": "opening",
    "检索": "search",
    "标题": "title",
}
_DIRECTIVE_FIELD_NAMES = tuple(FIELD_KEYS.values())

_MARKER_RE = re.compile(r"^[ \t]*【(主题|开场|检索|标题)】")
_COLLAPSE_WS_RE = re.compile(r"\s+")

# opening_boost 词表(修订版 §6.1;词表是封闭集合,测试锁定)
_SCOPE_WORDS = ("开头", "开场", "片头", "前3秒", "前三秒")
_INTENSITY_WORDS = ("炸", "震撼", "强烈", "冲击", "甩镜", "炫酷")
_NEGATION_WORDS = ("不要", "无需", "不需要", "别", "禁止", "不用")


def parse_user_directives(text: Any) -> dict[str, Any]:
    """解析【】标记为四字段 + 解析警告。

    Returns:
        ``{"theme": str|None, "opening": str|None, "search": str|None,
        "title": str|None, "warnings": list[str]}``。字段值为多行时保留
        换行原样收集(消费方各自做空白折叠);空值一律归一为 ``None``
        (未提供),消费方据此走各自回退链。纯函数:不修改入参、无副作用。
    """
    result: dict[str, Any] = {key: None for key in _DIRECTIVE_FIELD_NAMES}
    result["warnings"] = []
    if not isinstance(text, str) or not text.strip():
        return result

    seen: set[str] = set()
    conflict_keys: set[str] = set()
    current_key: str | None = None  # 当前正在收集的有效字段;冲突字段置 None
    for raw_line in text.splitlines():
        m = _MARKER_RE.match(raw_line)
        if m:
            key = FIELD_KEYS[m.group(1)]
            if key in seen:
                # 同名重复:该字段 fail-closed,警告只记一次
                if key not in conflict_keys:
                    conflict_keys.add(key)
                    result["warnings"].append(f"duplicate_directive:{key}")
                    result[key] = None
                current_key = None
                continue
            seen.add(key)
            current_key = key
            result[key] = raw_line[m.end() :].strip()
            continue
        # 非标记行:未知【xx】与普通文本都归入当前字段(无字段则忽略)
        if current_key is not None and raw_line.strip():
            prev = result[current_key]
            result[current_key] = f"{prev}\n{raw_line.strip()}" if prev else raw_line.strip()

    # 空值 = 未提供(修订版 §6.1 规则4)
    for key in _DIRECTIVE_FIELD_NAMES:
        value = result.get(key)
        if isinstance(value, str):
            result[key] = value.strip() or None
    return result


def opening_boost_requested(directives: Any) -> bool:
    """【开场】字段是否正向要求强动势开场转场(fail-closed)。

    触发条件(三者同时满足,修订版 §6.1):字段含开场范围词 + 「转场」 +
    强动势词;否定词优先 —— 命中任一否定词一律 ``False``。只检查【开场】
    字段:其他字段的「转场」「大字标题」不会误触(修订版 P1-06:无标记 =
    严格不触发)。
    """
    if not isinstance(directives, dict):
        return False
    opening = directives.get("opening")
    if not isinstance(opening, str) or not opening:
        return False
    if any(word in opening for word in _NEGATION_WORDS):
        return False
    has_scope = any(word in opening for word in _SCOPE_WORDS)
    has_transition = "转场" in opening
    has_intensity = any(word in opening for word in _INTENSITY_WORDS)
    return has_scope and has_transition and has_intensity


def normalize_hook_title(text: Any) -> tuple[str, bool]:
    """新链路标题规范化:空白折叠 → strip → 截断到 12 个 Unicode 字符。

    Returns:
        ``(规范化标题, 是否截断)``;空输入返回 ``("", False)``。发生截断时
        由调用方记录 ``title_truncated=true`` 留痕(修订版 P1-05:不静默截断)。

    Note:
        只用于**新数据写入端**(模板标题/LLM 标题写 ``state["hook_title"]``
        之前);不改变 ``pick_hook_title`` 对既有显式标题仅 ``strip()`` 不截断
        的兼容语义(该语义被 ``test_explicit_title_not_truncated`` 锁定)。
    """
    if not isinstance(text, str):
        return "", False
    collapsed = _COLLAPSE_WS_RE.sub(" ", text).strip()
    if not collapsed:
        return "", False
    if len(collapsed) <= HOOK_TITLE_MAX_LEN:
        return collapsed, False
    return collapsed[:HOOK_TITLE_MAX_LEN].strip(), True


def resolve_template_title(directives: Any) -> str | None:
    """取【标题】规范化值;未提供/空 → ``None``(消费方回退 LLM 标题)。"""
    if not isinstance(directives, dict):
        return None
    title, _truncated = normalize_hook_title(directives.get("title"))
    return title or None


__all__ = [
    "FIELD_KEYS",
    "normalize_hook_title",
    "opening_boost_requested",
    "parse_user_directives",
    "resolve_template_title",
]
