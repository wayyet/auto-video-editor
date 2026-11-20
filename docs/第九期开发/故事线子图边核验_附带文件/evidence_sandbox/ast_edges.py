"""静态解析 graph.py::_build_state_graph 中的 add_node / add_edge / add_conditional_edges。

用法(在仓库目录之外执行,仓库路径作为参数传入):
    venv/bin/python -I ast_edges.py <repo_root> [--json out.json]

只读:仅 ast.parse 读取源码,不 import 仓库代码。
输出每个调用的 行号 / 种类 / 所处 if 条件 / 参数文本。
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


def main() -> int:
    repo = Path(sys.argv[1])
    out_json = None
    if "--json" in sys.argv:
        out_json = Path(sys.argv[sys.argv.index("--json") + 1])

    src_path = repo / "graph.py"
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_build_state_graph"
    )

    records: list[dict] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.conds: list[str] = []

        def visit_If(self, node: ast.If) -> None:  # noqa: N802
            cond = ast.unparse(node.test)
            self.conds.append(cond)
            for s in node.body:
                self.visit(s)
            self.conds.pop()
            if node.orelse:
                self.conds.append(f"not ({cond})")
                for s in node.orelse:
                    self.visit(s)
                self.conds.pop()

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in (
                "add_node",
                "add_edge",
                "add_conditional_edges",
            ):
                rec = {
                    "kind": f.attr,
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "conds": list(self.conds),
                    "args": [ast.unparse(a) for a in node.args],
                }
                records.append(rec)
            self.generic_visit(node)

    Visitor().visit(fn)
    records.sort(key=lambda r: r["line"])

    # ---- 汇总 ----
    n_node = sum(1 for r in records if r["kind"] == "add_node")
    n_edge = sum(1 for r in records if r["kind"] == "add_edge")
    n_cond = sum(1 for r in records if r["kind"] == "add_conditional_edges")
    print(f"# graph.py = {src_path}")
    print(f"# _build_state_graph: lines {fn.lineno}-{fn.end_lineno}")
    print(f"# add_node={n_node}  add_edge={n_edge}  add_conditional_edges={n_cond}")
    print()
    for r in records:
        cond = " && ".join(r["conds"]) if r["conds"] else "-"
        args = " | ".join(a.replace("\n", " ") for a in r["args"])
        args = " ".join(args.split())
        print(f"{r['line']:>4}-{r['end_line']:<4} {r['kind']:<22} if[{cond}]  {args}")

    if out_json:
        out_json.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n# wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
