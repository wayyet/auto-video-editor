"""条件路由完备性静态核验(只读,仅 ast):路由函数的全部返回值 ⊆ 路径映射的键?

用法:
    venv/bin/python -I router_check.py <repo_root>

对 graph.py::_build_state_graph 里的每个 add_conditional_edges(src, router, path_map):
  1. 在 graph.py / nodes/storyline/qa_gate.py 中按名字找到路由函数定义;
  2. 收集函数内全部 return 表达式(字符串常量 / END / 条件表达式的两个分支);
  3. 与 path_map 的键对比:返回值是否都有映射(缺映射 = 运行时 KeyError / 无处可去),
     以及 path_map 里有没有"永远不会被返回"的键(死分支)。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
sources = {
    "graph.py": repo / "graph.py",
    "nodes/storyline/qa_gate.py": repo / "nodes/storyline/qa_gate.py",
    "nodes/node_07_speed_fit.py": repo / "nodes/node_07_speed_fit.py",
    "nodes/assembly/node_validate_render_qc.py": repo / "nodes/assembly/node_validate_render_qc.py",
}
trees = {k: ast.parse(v.read_text(encoding="utf-8")) for k, v in sources.items()}

funcs: dict[str, tuple[str, ast.FunctionDef]] = {}
for fname, t in trees.items():
    for n in ast.walk(t):
        if isinstance(n, ast.FunctionDef):
            funcs.setdefault(n.name, (fname, n))


def ret_values(fn: ast.FunctionDef):
    out, dynamic = set(), []

    def val(e):
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            out.add(e.value)
        elif isinstance(e, ast.Name) and e.id == "END":
            out.add("__end__")
        elif isinstance(e, ast.IfExp):
            val(e.body)
            val(e.orelse)
        else:
            dynamic.append(ast.unparse(e))

    for n in ast.walk(fn):
        if isinstance(n, ast.Return) and n.value is not None:
            val(n.value)
    return out, dynamic


def walk_map(node):
    if isinstance(node, ast.Dict):
        res = {}
        for k, v in zip(node.keys, node.values):
            kk = (
                "__end__"
                if isinstance(k, ast.Name) and k.id == "END"
                else (k.value if isinstance(k, ast.Constant) else ast.unparse(k))
            )
            vv = (
                "__end__"
                if isinstance(v, ast.Name) and v.id == "END"
                else (v.value if isinstance(v, ast.Constant) else ast.unparse(v))
            )
            res[kk] = vv
        return res
    return None


g_tree = trees["graph.py"]
build = next(
    n for n in g_tree.body if isinstance(n, ast.FunctionDef) and n.name == "_build_state_graph"
)
rows = []
for n in ast.walk(build):
    if (
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "add_conditional_edges"
    ):
        src = ast.unparse(n.args[0])
        router_expr = ast.unparse(n.args[1])
        router_name = router_expr.split(".")[-1]
        pmap = walk_map(n.args[2])
        fname, fn = funcs.get(router_name, (None, None))
        if fn is None:
            rows.append((n.lineno, src, router_expr, "未找到定义", pmap, None, None))
            continue
        rets, dyn = ret_values(fn)
        rows.append((n.lineno, src, f"{fname}:{fn.lineno} {router_name}", "ok", pmap, rets, dyn))

print(f"{'行':>4}  源节点 → 路由函数")
all_ok = True
for line, src, rdesc, status, pmap, rets, dyn in sorted(rows):
    print(f"\n[{line}] {src}  ←  {rdesc}")
    if status != "ok":
        print("   ", status)
        all_ok = False
        continue
    keys = set(pmap or {})
    missing = sorted(rets - keys)
    unused = sorted(keys - rets)
    print("    路径映射键 :", sorted(keys))
    print("    函数返回值 :", sorted(rets), ("动态:" + str(dyn)) if dyn else "")
    print("    返回值缺映射:", missing or "无", "| 映射里从不被返回:", unused or "无")
    if missing or unused or dyn:
        all_ok = False
print("\n全部条件路由完备且无死分支:", all_ok)
