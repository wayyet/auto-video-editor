#!/usr/bin/env python3
"""重复文件审计脚本（只报告，不删除、不修改任何被扫描文件）。

用途：冗余模块合并重构的基线审计（阶段 0.6）与阶段后复核（如 2A.5）。
递归扫描仓库普通文件，按 SHA-256 分组，输出 JSON + Markdown 两份清单，
并把 docs 快照、测试 fixture、空 `__init__.py`/空文件与生产源码分开分类。

用法（从仓库根目录运行）：
    python scripts/audit_duplicate_files.py [输出目录] [--json-only]

输出目录缺省为 docs/refactor-baseline-20261103。
文件名带时间戳，便于保留多份历史审计结果对比。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 排除目录：版本控制/工具点目录、虚拟环境、缓存、运行日志与构建产物、运行期媒体数据、vendored 第三方。
# 以 "." 开头的目录（.git、.venv*、.video_agent、.storyline、.worktrees、.zcode 等）一律排除。
EXCLUDED_DIRS = {
    "__pycache__",
    "node_modules",
    "outputs",
    "out",
    "logs",
    "inputs",
    "drafts",
    "vendor",  # vendored 第三方（pyJianYingDraft），不属于本项目源码去重范围
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp"}

# 单文件哈希大小上限：超过则跳过并单独记录（防 OneDrive 云占位文件拖慢扫描）。
MAX_FILE_BYTES = 64 * 1024 * 1024

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def classify(rel_posix: str) -> str:
    """把一个仓库相对路径分类为审计关注类别。"""
    parts = rel_posix.split("/")
    name = parts[-1]
    if name == "__init__.py":
        return "empty_init_marker"
    if parts[0] == "docs":
        return "docs_snapshot"
    if "fixtures" in parts or "fixture" in name:
        return "test_fixture"
    if rel_posix.startswith("tests") or rel_posix.startswith("web/tests"):
        return "test_fixture"
    return "production_source"


def iter_files() -> "tuple[list[tuple[str, int, str]], list[str], list[str]]":
    """返回 ([(相对路径, 大小, sha256)], 跳过的大文件, 读取失败的文件)。"""
    scanned: list[tuple[str, int, str]] = []
    skipped_large: list[str] = []
    skipped_unreadable: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(REPO_ROOT)
        parts = set(rel.parts)
        if any(p.startswith(".") for p in parts):
            continue
        if parts & EXCLUDED_DIRS:
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        rel_posix = rel.as_posix()
        try:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                skipped_large.append(rel_posix)
                continue
            content = path.read_bytes()
        except OSError:
            skipped_unreadable.append(rel_posix)
            continue
        scanned.append((rel_posix, size, hashlib.sha256(content).hexdigest()))
    return scanned, skipped_large, skipped_unreadable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", nargs="?", default="docs/refactor-baseline-20261103")
    parser.add_argument("--json-only", action="store_true", help="只写 JSON，不写 Markdown")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = Path.cwd() / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    scanned, skipped_large, skipped_unreadable = iter_files()

    groups: dict[str, dict] = {}
    for rel, size, digest in scanned:
        entry = groups.setdefault(digest, {"sha256": digest, "size": size, "paths": []})
        entry["paths"].append(rel)

    duplicate_groups = [g for g in groups.values() if len(g["paths"]) > 1]
    for g in duplicate_groups:
        g["paths"].sort()
        categories = sorted({classify(p) for p in g["paths"]})
        g["categories"] = categories
        g["category"] = categories[0] if len(categories) == 1 else "mixed"
    duplicate_groups.sort(key=lambda g: (g["category"], g["paths"][0]))

    try:
        base_commit = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except Exception:
        base_commit = "unknown"

    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary = {
        "category_totals": {
            c: sum(1 for g in duplicate_groups if g["category"] == c)
            for c in sorted({g["category"] for g in duplicate_groups})
        },
        "scanned_files": len(scanned),
        "duplicate_groups": len(duplicate_groups),
        "duplicate_paths": sum(len(g["paths"]) for g in duplicate_groups),
        "skipped_large_files": sorted(skipped_large),
        "skipped_unreadable_files": sorted(skipped_unreadable),
        "excluded_dirs": sorted(EXCLUDED_DIRS),
        "base_commit": base_commit,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "groups": duplicate_groups,
    }

    json_path = out_dir / f"duplicate-files-audit-{now}.json"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"JSON: {json_path.relative_to(REPO_ROOT).as_posix()}")

    if not args.json_only:
        lines = [
            "# 重复文件审计报告",
            "",
            f">- 生成时间：{summary['generated_at']}",
            f">- 基准提交：`{base_commit}`",
            f">- 扫描文件数：{len(scanned)}（排除目录：所有点目录及 {', '.join(sorted(EXCLUDED_DIRS))}）",
            f">- 重复组：**{len(duplicate_groups)}** 组，涉及 "
            f"{sum(len(g['paths']) for g in duplicate_groups)} 个路径",
            f">- 跳过的大文件：{len(skipped_large)} 个；读取失败：{len(skipped_unreadable)} 个",
            "",
            "| 分类 | 重复组数 |",
            "|---|---:|",
        ]
        for cat, total in summary["category_totals"].items():
            lines.append(f"| {cat} | {total} |")
        lines.append("")
        for i, g in enumerate(duplicate_groups, 1):
            lines.append(
                f"## 组 {i}（{g['category']}，{g['size']} 字节 × {len(g['paths'])}）"
            )
            lines.append("")
            lines.append(f"`sha256: {g['sha256'][:16]}…`")
            lines.append("")
            for p in g["paths"]:
                lines.append(f"- `{p}`")
            lines.append("")
        md_path = out_dir / f"duplicate-files-audit-{now}.md"
        md_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"MD:   {md_path.relative_to(REPO_ROOT).as_posix()}")

    print(
        f"共 {len(duplicate_groups)} 组重复（"
        + ", ".join(f"{c}={n}" for c, n in summary["category_totals"].items())
        + "）；仅报告，未修改任何文件。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
