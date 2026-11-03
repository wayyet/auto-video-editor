#!/usr/bin/env python3
"""采集提示词渲染 golden 基线(单 Loader 口径;单流水线架构 2026-11-04 阶段2)。

背景:旧聊天运行时 ``open_storyline.utils.prompts`` 已随 src/open_storyline/
删除,项目收敛为唯一 Loader ``storyline_capabilities.prompts``(数据目录
``prompts/tasks/``)。本脚本对 48 组模板重新记录:

- 模板文件 SHA-256;
- ``load_prompt`` 原文加载输出 SHA-256;
- 全变量替换渲染输出 SHA-256(变量名取自占位符语法 ``{{name}}`` /
  ``{name}`` / ``{name:?}``,值用确定性占位串 ``<名字>``,保证可复算)。

输出 JSON 到 ``tests/unit/golden/prompt_render_golden.json``,供回归测试
逐文件比对。脚本只读取仓库文件,除输出 JSON 外不修改任何内容。

用法(从仓库根目录运行):
    python scripts/capture_prompt_golden.py
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import storyline_capabilities.prompts as sc_prompts  # noqa: E402

SHARED_DIR = REPO_ROOT / "prompts" / "tasks"
OUT_PATH = REPO_ROOT / "tests" / "unit" / "golden" / "prompt_render_golden.json"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def placeholder_names(raw: str) -> dict[str, str]:
    """返回 变量名 -> 注入用 dummy 值 的映射(三种占位符语法)。"""
    names: dict[str, str] = {}
    for m in sc_prompts._PLACEHOLDER_RE.finditer(raw):
        name = m.group("double") or m.group("single")
        names.setdefault(name, f"<{name}>")
    return names


def main() -> int:
    files = sorted(p for p in SHARED_DIR.rglob("*.md") if p.is_file())
    if not files:
        print("ERROR: prompts/tasks 下没有 .md 文件", file=sys.stderr)
        return 1

    entries = []
    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        kind, lang, part = path.relative_to(SHARED_DIR).parts[0], path.parts[-2], path.stem

        raw = path.read_bytes().decode("utf-8")
        variables = placeholder_names(raw)

        loaded_sc = sc_prompts.load_prompt(kind, part, lang=lang)
        rendered_sc = sc_prompts.render_prompt(kind, part, lang=lang, **variables)

        entries.append(
            {
                "rel": rel,
                "kind": kind,
                "lang": lang,
                "part": part,
                "sha_template": hashlib.sha256(path.read_bytes()).hexdigest(),
                "sha_load_prompt": sha256_text(loaded_sc),
                "sha_render_prompt": sha256_text(rendered_sc),
                "variable_count": len(variables),
                "variables": variables,
            }
        )

    try:
        base_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:
        base_commit = "unknown"

    payload = {
        "meta": {
            "note": (
                "单 Loader golden 基线(2026-11-04 阶段2 重录):旧聊天运行时"
                " open_storyline.utils.prompts 已删除,唯一 Loader 为 "
                "storyline_capabilities.prompts,数据目录 prompts/tasks/。"
                "sha_* 字段供后续重构逐文件复算比对。"
            ),
            "base_commit": base_commit,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "file_count": len(entries),
        },
        "files": entries,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK: 采集 {len(entries)} 份模板 golden -> {OUT_PATH.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
