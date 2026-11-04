#!/usr/bin/env python3
"""静态导入检查:找出仓库内"指向不存在的模块或名字"的导入。

用法:  python check_imports.py <仓库根目录>

- 只用 ast 解析,不 import、不执行仓库代码。
- 导入根目录:<仓库根> 与 <仓库根>/src(与 agent_fastapi.py 的 sys.path 设置一致)。
- 只检查"第一段名字在仓库内能找到"的导入;标准库 / 第三方库自动跳过。
- 动态加载(importlib / 字符串派发 / config.toml 的 available_nodes)不在范围内,另用 grep 复核。
退出码:0 = 全部能解析;1 = 有解析不了的。
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

# ".worktrees" 为本机 git worktree 残留副本(git-ignored,非本仓库内容),与 .venv 同类跳过。
SKIP = {".git", ".worktrees", "docs", "__pycache__", "node_modules", "outputs", "logs", "inputs", "openstoryline-config", "vendor"}


def py_files(root: Path):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".venv")]
        for name in sorted(files):
            if name.endswith(".py"):
                yield Path(dirpath, name)


def targets(t: ast.AST) -> set[str]:
    if isinstance(t, ast.Name):
        return {t.id}
    if isinstance(t, (ast.Tuple, ast.List)):
        return set().union(*(targets(e) for e in t.elts)) if t.elts else set()
    return targets(t.value) if isinstance(t, ast.Starred) else set()


class Repo:
    def __init__(self, root: Path):
        self.roots = (root, root / "src")
        self.cache: dict[Path, tuple[set[str], bool]] = {}

    def find(self, parts: list[str]) -> Path | None:
        for base in self.roots:
            p = base.joinpath(*parts)
            if p.with_suffix(".py").is_file():
                return p.with_suffix(".py")
            if p.is_dir():
                return p
        return None

    def names(self, target: Path) -> tuple[set[str], bool]:
        """模块顶层能导入的名字;第二项 True = 无法确定(有 import * 或 __getattr__)。"""
        f = target if target.is_file() else target / "__init__.py"
        if f not in self.cache:
            names: set[str] = set()
            unknown = False
            if f.is_file():
                try:
                    unknown = self._scan(ast.parse(f.read_text(encoding="utf-8")).body, names)
                except (SyntaxError, UnicodeDecodeError):
                    unknown = True
            self.cache[f] = (names, unknown)
        return self.cache[f]

    def _scan(self, body: list[ast.stmt], names: set[str]) -> bool:
        unknown = False
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
                unknown |= n.name == "__getattr__"
            elif isinstance(n, ast.Assign):
                names.update(*(targets(t) for t in n.targets))
            elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
                names.update(targets(n.target))
            elif isinstance(n, ast.Import):
                names.update((a.asname or a.name).split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                unknown |= any(a.name == "*" for a in n.names)
                names.update(a.asname or a.name for a in n.names if a.name != "*")
            elif isinstance(n, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for block in (getattr(n, k, []) for k in ("body", "orelse", "finalbody")):
                    unknown |= self._scan(block, names)
                for h in getattr(n, "handlers", []):
                    unknown |= self._scan(h.body, names)
        return unknown

    def has(self, target: Path, name: str) -> bool:
        if target.is_dir() and ((target / f"{name}.py").is_file() or (target / name).is_dir()):
            return True  # 子模块
        names, unknown = self.names(target)
        return unknown or name in names


def check(path: Path, root: Path, repo: Repo) -> list[str]:
    rel = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as e:
        return [f"{rel}: 无法解析: {e}"]
    bad: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                parts = a.name.split(".")
                if repo.find(parts[:1]) and not repo.find(parts):
                    bad.append(f"{rel}:{n.lineno}: import {a.name}  (模块不存在)")
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if n.level == 0:
                if not repo.find(mod.split(".")[:1]):
                    continue  # 标准库 / 第三方
                target = repo.find(mod.split("."))
            else:
                base = path.parent
                for _ in range(n.level - 1):
                    base = base.parent
                t = base.joinpath(*mod.split(".")) if mod else base
                target = t.with_suffix(".py") if t.with_suffix(".py").is_file() else (t if t.is_dir() else None)
            label = f"from {'.' * n.level}{mod} import"
            if target is None:
                bad.append(f"{rel}:{n.lineno}: {label} ...  (模块不存在)")
                continue
            bad += [
                f"{rel}:{n.lineno}: {label} {a.name}  (模块里没有这个名字)"
                for a in n.names
                if a.name != "*" and not repo.has(target, a.name)
            ]
    return bad


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    repo = Repo(root)
    bad = [line for p in py_files(root) for line in check(p, root, repo)]
    if bad:
        print("\n".join(bad))
    print(f"未解析的导入: {len(bad)} 处")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
