"""Prompt 加载器 — Storyline 能力层统一资源入口。

仓库提示词 Markdown 的唯一物理目录为 ``prompts/tasks/<kind>/<lang>/<part>.md``。
本模块保留 Storyline 能力层的公开 API 和渲染策略；OpenStoryline vendored
调用方继续使用 ``open_storyline.utils.prompts``，两套 Loader 不合并，避免改变
缺失文件、占位符和缓存等既有语义。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional


# 统一使用仓库根 prompts/tasks；资源不再在两个目录各保存一份。
# 本文件位于 <repo>/storyline_capabilities/prompts/__init__.py，parents[2] 即仓库根。
REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_ROOT: Path = REPO_ROOT / "prompts" / "tasks"


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
