#!/usr/bin/env python3
"""用 AST 解析 graph.py::_build_state_graph,提取全部节点 / 普通边 / 条件边(只读,不执行仓库代码)。

用法:
    python ast_graph.py <仓库根目录> <输出json路径>
例:
    python ast_graph.py E:\\Documents\\kuaishou\\auto-video-editor .\\graph_extract_new.json

输出 json 结构: {"nodes": [...], "edges": [...], "cond": [...]}
  - edges[].src 可以是 list(列表型 fan-in = 汇合屏障)
  - ctx 记录该语句所处的 if 条件(例如 _config.ASSEMBLY_QC_GATE_ENABLED)
"""
import ast
import json
import sys
from pathlib import Path

repo, out = Path(sys.argv[1]), Path(sys.argv[2])
src = (repo / "graph.py").read_text(encoding="utf-8")
tree = ast.parse(src)
fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_build_state_graph")


def lit(n):
    if isinstance(n, ast.Constant):
        return n.value
    if isinstance(n, ast.Name):
        return "$" + n.id  # START / END / 路由函数名
    if isinstance(n, ast.Attribute):
        return "$" + ast.unparse(n)
    if isinstance(n, (ast.List, ast.Tuple)):
        return [lit(e) for e in n.elts]
    if isinstance(n, ast.Dict):
        return {str(lit(k)): lit(v) for k, v in zip(n.keys, n.values)}
    return ast.unparse(n)


nodes, edges, cond = [], [], []


def walk(body, ctx):
    for st in body:
        if isinstance(st, ast.If):
            c = ast.unparse(st.test)
            walk(st.body, ctx + [c])
            walk(st.orelse, ctx + ["not(" + c + ")"])
        elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            c = st.value
            if isinstance(c.func, ast.Attribute) and c.func.attr in ("add_node", "add_edge", "add_conditional_edges"):
                a = [lit(x) for x in c.args]
                if c.func.attr == "add_node":
                    nodes.append({"name": a[0], "fn": a[1], "line": c.lineno})
                elif c.func.attr == "add_edge":
                    edges.append({"src": a[0], "dst": a[1], "line": c.lineno, "ctx": ctx})
                else:
                    cond.append({"src": a[0], "router": a[1], "map": a[2] if len(a) > 2 else None,
                                 "line": c.lineno, "ctx": ctx})


walk(fn.body, [])
out.write_text(json.dumps({"nodes": nodes, "edges": edges, "cond": cond}, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"注册节点 = {len(nodes)} | 普通边 = {len(edges)} | 条件边 = {len(cond)}  -> {out}")
