#!/usr/bin/env python3
"""采集双 Loader 提示词渲染 golden 基线（只在重构前运行一次）。

用途：冗余模块合并阶段 2A/2B 的前置（计划 2A.4 / 2B 前置）。对
``prompts/tasks/`` 与 ``storyline_capabilities/prompts/`` 下 48 组逐字一致的
模板，分别记录：

- 两份物理副本的 SHA-256（重构前必须一致，否则中止）；
- 两套 Loader 原文加载输出的 SHA-256；
- 全变量替换渲染输出的 SHA-256（变量名取自两种占位符语法的并集，
  值用确定性占位串 ``<名字>``，保证可复算）。

输出 JSON 到 ``tests/unit/golden/prompt_render_golden.json``，供重构后的
回归测试逐文件比对。脚本只读取仓库文件，除输出 JSON 外不修改任何内容。

用法（从仓库根目录运行，必须在删除重复副本之前执行）：
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
for _p in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from open_storyline.utils import prompts as osl_prompts  # noqa: E402
import storyline_capabilities.prompts as sc_prompts  # noqa: E402

SHARED_DIR = REPO_ROOT / "prompts" / "tasks"
COPY_DIR = REPO_ROOT / "storyline_capabilities" / "prompts"
OUT_PATH = REPO_ROOT / "tests" / "unit" / "golden" / "prompt_render_golden.json"

# open_storyline 侧现行渲染正则（宽松捕获花括号内任意内容，缺变量抛 KeyError）。
_OSL_DOUBLE_RE = re.compile(r"{{(.*?)}}")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def placeholder_names(raw: str) -> dict[str, str]:
    """返回 变量名 -> 注入用 dummy 值 的映射（两套语法的并集）。

    open_storyline 侧按 ``{{(.*?)}}`` 原样捕获（可能带空格），storyline 侧
    认 ``{{ident}}`` / ``{ident}`` / ``{ident:?}``；两侧 key 都注入 dummy，
    保证严格侧渲染也能完成。
    """
    names: dict[str, str] = {}
    for m in _OSL_DOUBLE_RE.finditer(raw):
        names[m.group(1)] = f"<{m.group(1)}>"
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
        copy_path = COPY_DIR / kind / lang / f"{part}.md"
        shared_bytes = path.read_bytes()
        copy_bytes = copy_path.read_bytes()
        if shared_bytes != copy_bytes:
            print(f"ERROR: 两份副本内容不一致，拒绝采集：{rel}", file=sys.stderr)
            return 1

        raw = shared_bytes.decode("utf-8")
        variables = placeholder_names(raw)

        loaded_sc = sc_prompts.load_prompt(kind, part, lang=lang)
        rendered_sc = sc_prompts.render_prompt(kind, part, lang=lang, **variables)
        # open_storyline 侧为严格语义（有占位符必须给变量），加载正确性由
        # 全变量渲染覆盖，不再单独做无变量加载。
        rendered_osl = osl_prompts.get_prompt(f"{kind}.{part}", lang=lang, **variables)

        entries.append(
            {
                "rel": rel,
                "kind": kind,
                "lang": lang,
                "part": part,
                "sha_shared_copy": hashlib.sha256(shared_bytes).hexdigest(),
                "sha_storyline_copy": hashlib.sha256(copy_bytes).hexdigest(),
                "sha_load_prompt": sha256_text(loaded_sc),
                "sha_render_prompt": sha256_text(rendered_sc),
                "sha_get_prompt_vars": sha256_text(rendered_osl),
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
                "重构前 golden 基线：prompts/tasks 与 storyline_capabilities/prompts "
                "两份物理副本当时逐字一致；两套 Loader 语义（严格/宽松、单/双花括号、"
                "缓存）未合并。sha_* 字段供重构后逐文件复算比对。"
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
