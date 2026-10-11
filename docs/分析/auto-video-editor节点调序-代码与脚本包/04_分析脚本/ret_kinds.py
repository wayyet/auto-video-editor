#!/usr/bin/env python3
"""静态分析:每个图节点是"返回整份 state",还是"只返回变更字段"。

为什么要查:LangGraph 同一超步(superstep,同一轮并行执行)里,两个节点不能写同一个
无 reducer(合并函数)的键,否则报 InvalidUpdateError。返回 `{**state, ...}` 的节点
会把 state 里"已有的所有键"都写一遍,最容易和并行节点撞车。

用法(只依赖 Python 标准库):
    python ret_kinds.py <仓库目录> <graph_extract.json>

输出每行:  节点名  FULL|DELTA|MIXED|UNKNOWN  入口函数 @ 文件
  FULL     所有 return 都是整份 state(`{**state, ...}` / `return state` / post_resume)
  DELTA    所有 return 都只含变更字段的字典
  MIXED    两种都有(需人工看)
  UNKNOWN  追不到定义(需人工看)
会沿着"本模块 / nodes.* 里的辅助函数"和 `asyncio.run(协程())` 追踪,最多 4 层。
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

MAX_DEPTH = 4


# ---------------------------------------------------------------- 基础判断
def full_state_dict(node: ast.AST | None) -> bool:
    """字典字面量里有 `**state`。"""
    return isinstance(node, ast.Dict) and any(
        k is None and isinstance(v, ast.Name) and v.id == "state" for k, v in zip(node.keys, node.values)
    )


def is_copy_of_state(value: ast.AST | None) -> bool:
    """`dict(state)` 或 `state.copy()`。"""
    if not isinstance(value, ast.Call):
        return False
    f = value.func
    if isinstance(f, ast.Name) and f.id == "dict":
        return bool(value.args) and isinstance(value.args[0], ast.Name) and value.args[0].id == "state"
    return (
        isinstance(f, ast.Attribute)
        and f.attr == "copy"
        and isinstance(f.value, ast.Name)
        and f.value.id == "state"
    )


def own_nodes(fn: ast.AST):
    """函数自己的语句,不进嵌套的 def / lambda / class。"""
    stack = list(getattr(fn, "body", []))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def own_returns(fn: ast.AST) -> list[ast.Return]:
    return sorted((n for n in own_nodes(fn) if isinstance(n, ast.Return)), key=lambda r: r.lineno)


def assigned_values(fn: ast.AST, var: str) -> list[ast.AST]:
    out: list[ast.AST] = []
    for n in own_nodes(fn):
        if isinstance(n, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == var for t in n.targets):
                out.append(n.value)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == var and n.value:
            out.append(n.value)
    return out


# ---------------------------------------------------------------- 模块索引
class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, tuple[dict[str, ast.AST], dict[str, tuple[str, str]]]] = {}

    def module(self, mod: str):
        """返回 (函数定义表, 导入表);导入表: 别名 -> (模块, 原名)。"""
        if mod not in self._cache:
            path = self.root / (mod.replace(".", "/") + ".py")
            defs: dict[str, ast.AST] = {}
            imps: dict[str, tuple[str, str]] = {}
            if path.exists():
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for n in ast.walk(tree):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        defs.setdefault(n.name, n)
                    elif isinstance(n, ast.ImportFrom) and n.module:
                        for a in n.names:
                            imps[a.asname or a.name] = (n.module, a.name)
            self._cache[mod] = (defs, imps)
        return self._cache[mod]

    def resolve(self, mod: str, name: str):
        """在 mod 里找函数 name;找不到就顺着 `from x import name` 追到定义。返回 (模块, 函数节点)。"""
        defs, imps = self.module(mod)
        if name in defs:
            return mod, defs[name]
        if name in imps:
            m2, orig = imps[name]
            if m2.startswith("nodes") or m2 == "graph":
                defs2, _ = self.module(m2)
                if orig in defs2:
                    return m2, defs2[orig]
        return mod, None


def merge(kinds: set[str]) -> str:
    kinds = {k for k in kinds if k != "NONE"}
    if not kinds:
        return "UNKNOWN"
    if kinds == {"FULL"}:
        return "FULL"
    if kinds == {"DELTA"}:
        return "DELTA"
    if "UNKNOWN" in kinds and len(kinds) == 1:
        return "UNKNOWN"
    return "MIXED"


def classify_value(repo: Repo, mod: str, fn: ast.AST, v: ast.AST | None, depth: int) -> str:
    if v is None:
        return "NONE"
    if full_state_dict(v):
        return "FULL"
    if isinstance(v, ast.Dict):
        return "DELTA"
    if isinstance(v, ast.IfExp):
        return merge({classify_value(repo, mod, fn, x, depth) for x in (v.body, v.orelse)})
    if isinstance(v, ast.Name):
        if v.id == "state":
            return "FULL"
        vals = assigned_values(fn, v.id)
        if not vals:
            return "UNKNOWN"
        if any(full_state_dict(x) or is_copy_of_state(x) for x in vals):
            return "FULL"
        return merge({classify_value(repo, mod, fn, x, depth) for x in vals})
    if isinstance(v, ast.Call):
        f = v.func
        name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
        if name == "post_resume":
            return "FULL"  # nodes/_checkpoint.py::post_resume 内部是 `{**state}`
        if name == "run" and isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "asyncio":
            return classify_value(repo, mod, fn, v.args[0], depth) if v.args else "UNKNOWN"
        if depth >= MAX_DEPTH or not name:
            return "UNKNOWN"
        m2, target = repo.resolve(mod, name)
        if target is None:
            return "UNKNOWN"
        return classify_function(repo, m2, target, depth + 1)
    return "UNKNOWN"


def classify_function(repo: Repo, mod: str, fn: ast.AST, depth: int = 0) -> str:
    rets = own_returns(fn)
    if not rets:
        return "UNKNOWN"
    return merge({classify_value(repo, mod, fn, r.value, depth) for r in rets})


# ---------------------------------------------------------------- 主程序
def main() -> None:
    repo_dir = Path(sys.argv[1]).resolve()
    ext = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    repo = Repo(repo_dir)
    rows = []
    for nd in ext["nodes"]:
        fn_name = nd["fn"].lstrip("$")
        mod, fn = repo.resolve("graph", fn_name)
        where = mod.replace(".", "/") + ".py"
        kind = classify_function(repo, mod, fn) if fn is not None else "UNKNOWN"
        orig = fn.name if fn is not None else fn_name
        rows.append((nd["name"], kind, orig, where))
    w = max(len(r[0]) for r in rows)
    for name, kind, orig, where in rows:
        print(f"{name:<{w}}  {kind:<7} {orig} @ {where}")


if __name__ == "__main__":
    main()
