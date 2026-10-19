"""Prompt 加载器 — 阶段 2/3 用。

vendored copy ``openstoryline/prompts/tasks/<kind>/zh/{system,user}.md`` 是一份,
本模块读 ``storyline_capabilities/prompts/<kind>/<lang>/<part>.md``。

阶段 2 起所有 B/C 类能力都从这里读 prompt,不再使用 vendored 路径。

占位符语法(D6 修复,2026-10-19):
模板正文用的是 **双花括号** ``{{name}}``(Jinja2 风格,与 vendored
``open_storyline.utils.prompts.get_prompt`` 的 ``re.sub(r"{{(.*?)}}")`` 一致)。
旧版加载器只认单花括号 ``{name}``,导致 ``{{user_request}}`` 这类占位符
**原样残留**进 prompt,下游模型拿到的是空素材信息(见集成文档 D6)。
现在两种语法都支持,且**未提供的变量保持原样**——宁可让调用方看到残留占位符,
也不要静默把空数据喂给模型。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional


PROMPTS_ROOT: Path = Path(__file__).resolve().parent


# ``{{name}}``(主)与 ``{name}`` / ``{name:?}``(旧写法)一次正则走完,
# 避免两轮 replace 互相污染(替换值里若含 ``{x}`` 不会被二次替换)。
_PLACEHOLDER_RE = re.compile(
    r"\{\{\s*(?P<double>[A-Za-z_][A-Za-z0-9_]*)\s*\}\}"
    r"|\{(?P<single>[A-Za-z_][A-Za-z0-9_]*)(?::\?)?\}"
)

# 只认双花括号:注入的 JSON 文本里常有单花括号,用它做"残留"判定会误报。
_UNRESOLVED_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def load_prompt(
    kind: str,
    part: str,
    *,
    lang: str = "zh",
    prompts_root: Optional[Path] = None,
) -> str:
    """读 ``<root>/<kind>/<lang>/<part>.md`` 文本。

    ``part`` 是文件 stem(不含扩展),例如 ``system`` / ``user`` /
    ``system_detail`` / ``user_overall``。
    失败时返回空串(单元测试与阶段 2 stub LLM 都能接受)。
    """
    root = prompts_root or PROMPTS_ROOT
    path = root / kind / lang / f"{part}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def unresolved_placeholders(text: str) -> list[str]:
    """返回文本里**仍未渲染**的 ``{{name}}`` 占位符名(去重,保持出现顺序)。

    只查双花括号:注入值里的 JSON / 字典字面量含单花括号,不算残留。
    """
    names: list[str] = []
    for m in _UNRESOLVED_RE.finditer(text or ""):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


def render_prompt(
    kind: str,
    part: str,
    *,
    lang: str = "zh",
    prompts_root: Optional[Path] = None,
    **variables: Any,
) -> str:
    """读 + 渲染变量。

    两种占位符语法都支持:

    - ``{{name}}`` —— vendored OpenStoryline 模板的实际写法(主)
    - ``{name}`` / ``{name:?}`` —— 旧版单花括号写法(兼容)

    **未提供的变量保持原样**(不替换成空串)。调用方应把模板需要的变量全部传入;
    可用 ``unresolved_placeholders(rendered)`` 自检是否还有残留。
    """
    tpl = load_prompt(kind, part, lang=lang, prompts_root=prompts_root)
    if not tpl:
        return tpl

    def _sub(m: re.Match[str]) -> str:
        name = m.group("double") or m.group("single")
        if name in variables:
            return str(variables[name])
        return m.group(0)

    return _PLACEHOLDER_RE.sub(_sub, tpl)


__all__ = [
    "PROMPTS_ROOT",
    "load_prompt",
    "render_prompt",
    "unresolved_placeholders",
]
