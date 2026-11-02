#!/usr/bin/env python3
"""Safely deduplicate OpenStoryline MCP modules and shared prompt resources.

Run from the repository root:
    python scripts/refactor_dedupe_openstoryline_mcp.py          # dry run
    python scripts/refactor_dedupe_openstoryline_mcp.py --apply  # apply changes

This generated artifact is intended to be copied to:
    scripts/refactor_dedupe_openstoryline_mcp.py
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


MCP_ALIASES: tuple[tuple[str, str, str], ...] = (
    (
        "src/open_storyline/hooks/chat_middleware.py",
        "src/open_storyline/mcp/hooks/chat_middleware.py",
        "open_storyline.mcp.hooks.chat_middleware",
    ),
    (
        "src/open_storyline/hooks/node_interceptors.py",
        "src/open_storyline/mcp/hooks/node_interceptors.py",
        "open_storyline.mcp.hooks.node_interceptors",
    ),
    (
        "src/open_storyline/register_tools.py",
        "src/open_storyline/mcp/register_tools.py",
        "open_storyline.mcp.register_tools",
    ),
    (
        "src/open_storyline/sampling_handler.py",
        "src/open_storyline/mcp/sampling_handler.py",
        "open_storyline.mcp.sampling_handler",
    ),
    (
        "src/open_storyline/sampling_requester.py",
        "src/open_storyline/mcp/sampling_requester.py",
        "open_storyline.mcp.sampling_requester",
    ),
    (
        "src/open_storyline/server.py",
        "src/open_storyline/mcp/server.py",
        "open_storyline.mcp.server",
    ),
)

SHIM_HEADER = '''"""旧导入路径兼容层。

唯一实现位于 :mod:`{canonical}`。此文件不再保存业务实现；导入时直接
将旧模块名映射到规范模块对象，确保公开/私有符号、模块级状态和 monkeypatch
都指向同一份实现。
"""
from __future__ import annotations

import sys

'''


def fail(message: str) -> "NoReturn":
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write changes; without this option, only validate and print the plan",
    )
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    if not (repo / ".git").exists() or not (repo / "graph.py").is_file():
        fail("脚本必须放在仓库 scripts/ 目录内，并从真实 Git 仓库运行。")

    # 预检全部 MCP 文件。只允许把字节级完全相同的旧副本替换为兼容层。
    mcp_plan: list[tuple[Path, str, str]] = []
    for legacy_rel, canonical_rel, module in MCP_ALIASES:
        legacy = repo / legacy_rel
        canonical = repo / canonical_rel
        if not legacy.is_file() or not canonical.is_file():
            fail(f"MCP 文件缺失：{legacy_rel} 或 {canonical_rel}")
        if legacy.read_bytes() != canonical.read_bytes():
            fail(
                "拒绝覆盖已发生分歧的 MCP 文件："
                f"{legacy_rel} 与 {canonical_rel} 当前不再逐字相同。"
            )
        mcp_plan.append((legacy, module, legacy_rel))

    # 预检 48 对 Markdown 提示词。要求路径集合和每个文件字节完全一致。
    shared_prompts = repo / "prompts" / "tasks"
    duplicate_prompts = repo / "storyline_capabilities" / "prompts"
    if not shared_prompts.is_dir() or not duplicate_prompts.is_dir():
        fail("提示词目录缺失，拒绝执行提示词资源迁移。")

    canonical_files = {
        p.relative_to(shared_prompts).as_posix(): p
        for p in shared_prompts.rglob("*.md")
        if p.is_file()
    }
    duplicate_files = {
        p.relative_to(duplicate_prompts).as_posix(): p
        for p in duplicate_prompts.rglob("*.md")
        if p.is_file()
    }
    if set(canonical_files) != set(duplicate_files):
        missing = sorted(set(canonical_files) - set(duplicate_files))
        extra = sorted(set(duplicate_files) - set(canonical_files))
        fail(
            "两套提示词相对路径集合不一致，拒绝删除："
            f"仅 prompts/tasks 有 {missing[:5]}；仅 storyline_capabilities/prompts 有 {extra[:5]}"
        )
    different = [
        rel
        for rel in sorted(canonical_files)
        if canonical_files[rel].read_bytes() != duplicate_files[rel].read_bytes()
    ]
    if different:
        fail(f"发现内容不同的提示词，拒绝迁移：{different[:10]}")
    if len(canonical_files) != 48:
        fail(f"预期 48 份逐字重复提示词，实际为 {len(canonical_files)}；请重新审查后再执行。")

    loader = repo / "storyline_capabilities" / "prompts" / "__init__.py"
    if not loader.is_file():
        fail("storyline_capabilities/prompts/__init__.py 不存在。")
    loader_text = loader.read_text(encoding="utf-8")
    old_root = "PROMPTS_ROOT: Path = Path(__file__).resolve().parent"
    new_root = (
        "# 统一使用仓库根 prompts/tasks；资源不再在两个目录各保存一份。\n"
        "# 本文件位于 <repo>/storyline_capabilities/prompts/__init__.py，parents[2] 即仓库根。\n"
        "REPO_ROOT = Path(__file__).resolve().parents[2]\n"
        'PROMPTS_ROOT: Path = REPO_ROOT / "prompts" / "tasks"'
    )
    new_loader_doc = (
        '\"\"\"Prompt 加载器 — Storyline 能力层统一资源入口。\n\n'
        '仓库提示词 Markdown 的唯一物理目录为 ``prompts/tasks/<kind>/<lang>/<part>.md``。\n'
        '本模块保留 Storyline 能力层的公开 API 和渲染策略；OpenStoryline vendored\n'
        '调用方继续使用 ``open_storyline.utils.prompts``，两套 Loader 不合并，避免改变\n'
        '缺失文件、占位符和缓存等既有语义。\n'
        '\"\"\"\n'
    )
    if not loader_text.startswith('"""'):
        fail("提示词 Loader 的模块 docstring 结构已变化，拒绝盲目替换。")
    doc_end = loader_text.find('"""', 3)
    if doc_end < 0:
        fail("提示词 Loader 模块 docstring 不完整。")
    doc_end += 3
    current_doc = loader_text[:doc_end]
    if "唯一物理目录为 ``prompts/tasks/" in current_doc:
        loader_doc_changed = False
    elif "Prompt 加载器" in current_doc and "storyline_capabilities/prompts" in current_doc:
        loader_text = new_loader_doc + loader_text[doc_end:]
        loader_doc_changed = True
    else:
        fail("提示词 Loader 模块说明已变化，拒绝覆盖未知文档内容。")

    if old_root not in loader_text:
        if new_root not in loader_text:
            fail("提示词 Loader 根目录定义已变化，拒绝盲目替换。")
        loader_root_changed = False
    else:
        loader_root_changed = True
    loader_changed = loader_doc_changed or loader_root_changed

    test_path = repo / "tests_openstoryline" / "test_mcp_legacy_compat.py"
    prompt_test = repo / "tests" / "unit" / "test_shared_storyline_prompt_resources.py"
    if test_path.exists() or prompt_test.exists():
        fail("新增测试文件路径已存在；请先审查现有测试，不覆盖用户文件。")

    # 单独处理 server：兼容 `python -m open_storyline.server` 的旧启动方式。
    changes: list[tuple[Path, str]] = []
    for legacy, module, rel in mcp_plan:
        body = SHIM_HEADER.format(canonical=module)
        if module == "open_storyline.mcp.server":
            body += (
                "from open_storyline.mcp import server as _implementation\n\n"
                'if __name__ == "__main__":\n'
                "    _implementation.main()\n"
                "else:\n"
                "    sys.modules[__name__] = _implementation\n"
            )
        else:
            body += (
                f"from {module.rsplit('.', 1)[0]} import "
                f"{module.rsplit('.', 1)[1]} as _implementation\n\n"
                "sys.modules[__name__] = _implementation\n"
            )
        changes.append((legacy, body))

    print("将执行以下变更：")
    print("- 6 个旧 MCP 源码路径替换为兼容模块别名；唯一实现保留在 open_storyline/mcp/。")
    print("- storyline_capabilities/prompts 的 48 份 Markdown 改为共用 prompts/tasks 资源。")
    print("- 保留 storyline_capabilities/prompts/__init__.py 的 API 与渲染行为。")
    print("- 新增兼容性测试，验证旧/新 MCP import 返回同一模块对象。")
    print("- 不修改 graph.py、数据库结构、ASR/TTS、FFmpeg、字体或 workflow 监控模块。")
    if not args.apply:
        print("\nDRY RUN：预检通过，尚未修改任何文件。添加 --apply 才会写入。")
        return 0

    for path, body in changes:
        path.write_text(body, encoding="utf-8", newline="\n")

    if loader_changed:
        if old_root in loader_text:
            loader_text = loader_text.replace(old_root, new_root)
        loader.write_text(loader_text, encoding="utf-8", newline="\n")

    for path in duplicate_files.values():
        path.unlink()

    # 只清理由刚才删除的 .md 导致变空的目录；不删除 __init__.py 或其他资源。
    for directory in sorted(
        (p for p in duplicate_prompts.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass

    test_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_test.parent.mkdir(parents=True, exist_ok=True)

    test_path.write_text(
        "\"\"\"旧 MCP 导入路径必须与规范路径指向同一模块对象。\"\"\"\n\n"
        "from importlib import import_module\n\n"
        "import pytest\n\n"
        "@pytest.mark.parametrize(\n"
        "    (\"legacy\", \"canonical\"),\n"
        "    [\n"
        "        (\"open_storyline.hooks.chat_middleware\", \"open_storyline.mcp.hooks.chat_middleware\"),\n"
        "        (\"open_storyline.hooks.node_interceptors\", \"open_storyline.mcp.hooks.node_interceptors\"),\n"
        "        (\"open_storyline.register_tools\", \"open_storyline.mcp.register_tools\"),\n"
        "        (\"open_storyline.sampling_handler\", \"open_storyline.mcp.sampling_handler\"),\n"
        "        (\"open_storyline.sampling_requester\", \"open_storyline.mcp.sampling_requester\"),\n"
        "        (\"open_storyline.server\", \"open_storyline.mcp.server\"),\n"
        "    ],\n"
        ")\n"
        "def test_legacy_import_is_canonical_module(legacy: str, canonical: str) -> None:\n"
        "    assert import_module(legacy) is import_module(canonical)\n",
        encoding="utf-8",
        newline="\n",
    )

    prompt_test.write_text(
        "\"\"\"Storyline 与 OpenStoryline 共用唯一 prompts/tasks 资源目录。\"\"\"\n\n"
        "from pathlib import Path\n\n"
        "from storyline_capabilities.prompts import PROMPTS_ROOT, load_prompt\n\n"
        "REPO_ROOT = Path(__file__).resolve().parents[2]\n\n"
        "def test_storyline_prompt_root_uses_shared_resource_tree() -> None:\n"
        "    assert PROMPTS_ROOT.resolve() == (REPO_ROOT / \"prompts\" / \"tasks\").resolve()\n\n"
        "def test_storyline_prompt_is_still_loadable() -> None:\n"
        "    text = load_prompt(\"understand_clips\", \"system_detail\", lang=\"zh\")\n"
        "    assert text.strip()\n\n"
        "def test_legacy_prompt_copy_contains_no_markdown_resources() -> None:\n"
        "    legacy_root = REPO_ROOT / \"storyline_capabilities\" / \"prompts\"\n"
        "    assert list(legacy_root.rglob(\"*.md\")) == []\n",
        encoding="utf-8",
        newline="\n",
    )

    print("\nAPPLIED：6 组 MCP 重复实现已收敛，48 份重复提示词已移除副本。")
    print("请按执行设计运行回归测试；本脚本不会代替测试或自动提交 Git。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
