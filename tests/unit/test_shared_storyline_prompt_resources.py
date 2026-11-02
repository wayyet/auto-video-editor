"""Storyline 与 OpenStoryline 共用唯一 prompts/tasks 资源目录。"""

from pathlib import Path

from storyline_capabilities.prompts import PROMPTS_ROOT, load_prompt

REPO_ROOT = Path(__file__).resolve().parents[2]

def test_storyline_prompt_root_uses_shared_resource_tree() -> None:
    assert PROMPTS_ROOT.resolve() == (REPO_ROOT / "prompts" / "tasks").resolve()

def test_storyline_prompt_is_still_loadable() -> None:
    text = load_prompt("understand_clips", "system_detail", lang="zh")
    assert text.strip()

def test_legacy_prompt_copy_contains_no_markdown_resources() -> None:
    legacy_root = REPO_ROOT / "storyline_capabilities" / "prompts"
    assert list(legacy_root.rglob("*.md")) == []
