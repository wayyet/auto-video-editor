"""Prompt 加载器 — 阶段 2/3 用。

vendored copy ``openstoryline/prompts/tasks/<kind>/zh/{system,user}.md`` 是一份,
本模块读 ``storyline_capabilities/prompts/<kind>/<lang>/<part>.md``。

阶段 2 起所有 B/C 类能力都从这里读 prompt,不再使用 vendored 路径。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional


PROMPTS_ROOT: Path = Path(__file__).resolve().parent


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


def render_prompt(
    kind: str,
    part: str,
    *,
    lang: str = "zh",
    **variables: Any,
) -> str:
    """读 + 简单 ``{name}`` 变量替换(plan §5 阶段 2 决策 1:保留 zh/en 双语结构)。"""
    tpl = load_prompt(kind, part, lang=lang)
    if not tpl or not variables:
        return tpl
    out = tpl
    for k, v in variables.items():
        out = out.replace("{" + k + "}", str(v))
        out = out.replace("{" + k + ":?}" , str(v))  # 容错 optional
    return out


__all__ = ["PROMPTS_ROOT", "load_prompt", "render_prompt"]
