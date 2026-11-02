"""Prompt 模板共享底层（冗余模块合并阶段 2B 抽取）。

两套 Prompt Facade —— ``open_storyline.utils.prompts``（OpenStoryline 侧，
严格语义）与 ``storyline_capabilities.prompts``（Storyline 能力层，宽松
语义）—— 在「路径解析 / UTF-8 读取 / 模板缓存 / 占位符替换」上的实现原本
各有一份。本模块把这部分收敛为唯一实现，**语义差异全部由策略参数承载**，
两套 Facade 的公开 API 与调用方可观察行为保持逐项不变：

==========================  ==============================  ==============================
策略                        取值                             现行使用方
==========================  ==============================  ==============================
文件缺失 ``missing_file``   ``"raise"`` / ``"empty"``       OSL 抛 FileNotFoundError；SC 返回空串
替换语法 ``syntax``         ``"raw_double"`` / ``"dual"``   OSL 仅 ``{{...}}`` 原样捕获；SC 兼容 ``{name}``/``{name:?}``
变量缺失 ``missing``        ``"raise"`` / ``"keep"``        OSL 抛 KeyError；SC 保留占位符原样
==========================  ==============================  ==============================

运行环境说明（ADR-001）：``open_storyline`` 独立虚拟环境按仓库惯例以**仓库根
为 cwd** 启动（``uvicorn agent_fastapi:app`` / ``python -m open_storyline.*``），
``sys.path[0]`` 即仓库根，因此本模块在两个虚拟环境中都可导入；``src/`` 不在
主项目运行时的 sys.path 上，故共享底层放在 ``jy_common`` 而非
``src/open_storyline``。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Union

__all__ = [
    "RAW_DOUBLE_BRACE_RE",
    "DUAL_SYNTAX_RE",
    "UNRESOLVED_DOUBLE_RE",
    "template_path",
    "read_template_text",
    "substitute",
    "unresolved_placeholders",
    "cached",
]


# ---------------------------------------------------------------------------
# 占位符正则（从两套 Facade 原样迁移，逐字符一致）
# ---------------------------------------------------------------------------

#: ``{{...}}`` 原样捕获（open_storyline.utils.prompts 现行 ``{{(.*?)}}``）：
#: 捕获组含空白等原文，缺变量按原键抛 KeyError，不容忍 ``{{ name }}``。
RAW_DOUBLE_BRACE_RE = re.compile(r"{{(.*?)}}")

#: 双花括号（主）+ 单花括号 ``{name}`` / ``{name:?}``（旧写法）一次正则走完
#: （storyline_capabilities.prompts 现行）：容忍空白；未提供变量保留原样。
DUAL_SYNTAX_RE = re.compile(
    r"\{\{\s*(?P<double>[A-Za-z_][A-Za-z0-9_]*)\s*\}\}"
    r"|\{(?P<single>[A-Za-z_][A-Za-z0-9_]*)(?::\?)?\}"
)

#: 残留检测只认双花括号：注入值里的 JSON / 字典字面量含单花括号，不算残留。
UNRESOLVED_DOUBLE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

_PATTERNS: Dict[str, re.Pattern[str]] = {
    "raw_double": RAW_DOUBLE_BRACE_RE,
    "dual": DUAL_SYNTAX_RE,
}


# ---------------------------------------------------------------------------
# 路径解析与读取
# ---------------------------------------------------------------------------


def template_path(root: Union[str, Path], task: str, lang: str, part: str) -> Path:
    """解析 ``<root>/<task>/<lang>/<part>.md``。"""
    return Path(root) / task / lang / f"{part}.md"


def read_template_text(path: Path, *, missing_file: str = "raise") -> str:
    """UTF-8 读取模板文本。

    ``missing_file="raise"`` —— 文件不存在抛 ``FileNotFoundError``，
    消息与 open_storyline 现行一致（``Template not found: <绝对路径>``）；
    ``missing_file="empty"`` —— 文件不存在返回空串（storyline 现行）。
    """
    if not path.exists():
        if missing_file == "empty":
            return ""
        if missing_file == "raise":
            raise FileNotFoundError(f"Template not found: {path}")
        raise ValueError(f"未知 missing_file 策略: {missing_file!r}")
    if missing_file not in ("raise", "empty"):
        raise ValueError(f"未知 missing_file 策略: {missing_file!r}")
    return path.read_text(encoding="utf-8")


def cached(cache: Dict[str, str], key: str, loader: Callable[[], str]) -> str:
    """按键缓存 ``loader()`` 结果；命中时返回**同一对象**（与现行缓存一致）。"""
    if key in cache:
        return cache[key]
    value = loader()
    cache[key] = value
    return value


# ---------------------------------------------------------------------------
# 占位符替换
# ---------------------------------------------------------------------------


def substitute(
    template: str,
    variables: Mapping[str, Any],
    *,
    syntax: str = "dual",
    missing: str = "keep",
) -> str:
    """按策略替换模板占位符。

    - ``syntax="raw_double"``：``{{(.*?)}}`` 原样捕获，名称按捕获组原文查键
      （``{{ name }}`` 的键是 ``" name "``，与 open_storyline 现行一致）；
    - ``syntax="dual"``：双/单花括号，strip 后匹配标识符；
    - ``missing="raise"``：缺变量抛 ``KeyError(名称)``（open_storyline 现行）；
    - ``missing="keep"``：缺变量保留占位符原文（storyline 现行）。

    变量值统一 ``str()`` 转换；替换单轮完成，注入值中的花括号不会被二次替换。
    """
    if syntax not in _PATTERNS:
        raise ValueError(f"未知 syntax 策略: {syntax!r}")
    if missing not in ("raise", "keep"):
        raise ValueError(f"未知 missing 策略: {missing!r}")
    pattern = _PATTERNS[syntax]

    def _sub(m: "re.Match[str]") -> str:
        if syntax == "raw_double":
            name = m.group(1)
        else:
            name = m.group("double") or m.group("single")
        if name in variables:
            return str(variables[name])
        if missing == "raise":
            raise KeyError(name)
        return m.group(0)

    return pattern.sub(_sub, template)


def unresolved_placeholders(text: str) -> "list[str]":
    """返回仍未渲染的 ``{{name}}`` 名称（去重，保持出现顺序）。"""
    names: "list[str]" = []
    for m in UNRESOLVED_DOUBLE_RE.finditer(text or ""):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names
