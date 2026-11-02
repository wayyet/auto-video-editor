from pathlib import Path
from typing import Dict, Any

import jy_common.prompt_core as prompt_core

# 2026-11-01 目录扁平化:提示词目录改为按本模块位置推导的**绝对路径**,
# 不再依赖进程当前工作目录 —— 用户从任意目录手工启动 Web(或跑测试)时,
# prompts/tasks 都能解析到仓库根下的同一位置。
# 本文件位于 <repo>/src/open_storyline/utils/prompts.py,parents[3] 即仓库根。
REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = REPO_ROOT / "prompts" / "tasks"


class PromptBuilder:
    """Builder for fixed templates with dynamic inputs"""

    def __init__(self, prompts_dir: Path = PROMPTS_DIR):
        self.prompts_dir = prompts_dir
        self._cache: Dict[str, str] = {}

    def _load_template(self, task: str, role: str, lang: str) -> str:
        """Load template file(带缓存;读取/缓存实现收敛到 jy_common.prompt_core)"""
        # prompts/tasks/filter_clips/zh/system.md
        return prompt_core.cached(
            self._cache,
            f"{task}:{role}:{lang}",
            lambda: prompt_core.read_template_text(
                prompt_core.template_path(self.prompts_dir, task, lang, role),
                missing_file="raise",
            ),
        )

    def render(self, task: str, role: str, lang: str = "zh", **variables: Any) -> str:
        """Render single template"""
        template = self._load_template(task, role, lang)
        return prompt_core.substitute(
            template, variables, syntax="raw_double", missing="raise"
        )

    def build(self, task: str, lang: str = "zh", **user_vars: Any) -> Dict[str, str]:
        """
        Build a complete prompt pair

        Args:
            task: Task name, e.g., "filter_clips"
            lang: Language, defaults to "zh"
            **user_vars: User input variables (passed to user.md template)

        Returns:
            {"system": "...", "user": "..."}

        Example:
            builder.build(
                "filter_clips",
                clip_data="...",
                requirements="Keep exciting clips"
            )
        """
        return {
            "system": self.render(task, "system", lang),
            "user": self.render(task, "user", lang, **user_vars)
        }


# Global singleton
_builder = PromptBuilder()


def get_prompt(name: str, lang: str = "zh", **kwargs:Any) -> str:
    """
    获取单个 prompt

    Args:
        name: "task.role" 格式，如 "filter_clips.system"
        lang: 语言
        **kwargs: 模板变量

    Example:
        get_prompt("filter_clips.system")
        get_prompt("filter_clips.user", clip_data="...")
    """
    parts = name.split(".")
    if len(parts) != 2:
        raise ValueError(f"Invalid format: '{name}', expected 'task/role'")

    task, role = parts
    return _builder.render(task, role, lang, **kwargs)


def build_prompts(task: str, lang: str = "zh", **user_vars: Any) -> Dict[str, str]:
    """
    Get a single prompt.

    Args:
        name: Format "task.role", e.g., "filter_clips.system"
        lang: Language, defaults to "zh"
        **kwargs: Template variables

    Example:
        get_prompt("filter_clips.system")
        get_prompt("filter_clips.user", clip_data="...")
    """
    return _builder.build(task, lang, **user_vars)
