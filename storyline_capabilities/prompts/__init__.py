"""Prompt 加载器 — Storyline 能力层统一资源入口。

仓库提示词 Markdown 的唯一物理目录为 ``prompts/tasks/<kind>/<lang>/<part>.md``。
本模块保留 Storyline 能力层的公开 API 和渲染策略（旧 OpenStoryline 侧 Facade
``open_storyline.utils.prompts`` 已随 2026-11-04 单流水线架构收敛删除）。两套
Facade 的底层实现（路径解析 / UTF-8 读取 / 占位符替换 / 残留检测）收敛到
``jy_common.prompt_core``（阶段 2B），本模块以宽松策略参数调用；
缺失文件、占位符和缓存等既有语义不变。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import jy_common.prompt_core as _prompt_core


# 统一使用仓库根 prompts/tasks；资源不再在两个目录各保存一份。
# 本文件位于 <repo>/storyline_capabilities/prompts/__init__.py，parents[2] 即仓库根。
REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_ROOT: Path = REPO_ROOT / "prompts" / "tasks"


# 正则实现收敛到共享底层；这里保留模块级别名，按名引用不受影响。
# ``{{name}}``(主)与 ``{name}`` / ``{name:?}``(旧写法)一次正则走完,
# 避免两轮 replace 互相污染(替换值里若含 ``{x}`` 不会被二次替换)。
_PLACEHOLDER_RE = _prompt_core.DUAL_SYNTAX_RE

# 只认双花括号:注入的 JSON 文本里常有单花括号,用它做"残留"判定会误报。
_UNRESOLVED_RE = _prompt_core.UNRESOLVED_DOUBLE_RE


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
    return _prompt_core.read_template_text(
        _prompt_core.template_path(root, kind, lang, part),
        missing_file="empty",
    )


def unresolved_placeholders(text: str) -> list[str]:
    """返回文本里**仍未渲染**的 ``{{name}}`` 占位符名(去重,保持出现顺序)。

    只查双花括号:注入值里的 JSON / 字典字面量含单花括号,不算残留。
    """
    return _prompt_core.unresolved_placeholders(text)


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

    return _prompt_core.substitute(tpl, variables, syntax="dual", missing="keep")


__all__ = [
    "PROMPTS_ROOT",
    "load_prompt",
    "render_prompt",
    "unresolved_placeholders",
]
