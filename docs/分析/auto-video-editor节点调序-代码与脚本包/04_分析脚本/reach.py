#!/usr/bin/env python3
"""对 ast_graph.py 的输出做可达性分析(human/auto 两种模式 × QC 通道开/关 = 4 种配置)。

用法:
    python reach.py <graph_extract.json>
输出: 每种配置下的可达/不可达节点;"四种配置都不可达"的节点即死节点。
注意: 静态分析把条件边映射里的所有目标都算作可达,所以"路由函数永不选中"的目标
(如 storyline_local_asr)会显示为可达,需另行结合路由函数判断。
"""
import json
import sys
from pathlib import Path

d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
nodes = [n["name"] for n in d["nodes"]]
norm = lambda x: {"$START": "__start__", "$END": "__end__"}.get(x, x)


def build(qc_on):
    adj = {}
    for e in d["edges"]:
        ctx = " ".join(e["ctx"])
        if "ASSEMBLY_QC_GATE_ENABLED" in ctx:
            is_not = "not(" in ctx
            if (qc_on and is_not) or ((not qc_on) and (not is_not)):
                continue
        for s in (e["src"] if isinstance(e["src"], list) else [e["src"]]):
            adj.setdefault(norm(s), set()).add(norm(e["dst"]))
    for c in d["cond"]:
        for _, v in (c["map"] or {}).items():
            adj.setdefault(norm(c["src"]), set()).add(norm(v))
    return adj


def reach(adj, mode):
    blocked = {("open_preview", "storyline_load_media")} if mode == "human" else {("open_preview", "checkpoint0_storyline_plan")}
    seen, stack = set(), ["__start__"]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        for t in adj.get(n, ()):
            if (n, t) not in blocked:
                stack.append(t)
    return seen - {"__start__", "__end__"}


res = {(qc, m): reach(build(qc), m) for qc in (True, False) for m in ("human", "auto")}
union = set().union(*res.values())
print(f"注册节点 {len(nodes)} | 任一配置可达 {len(union)} | 四种配置都不可达 {len(nodes) - len(union)}")
print("四种配置都不可达:", [n for n in nodes if n not in union])
for (qc, m), r in res.items():
    print(f"QC_GATE={qc!s:5} mode={m:5} 可达={len(r):2}  不可达={[n for n in nodes if n not in r]}")
