#!/usr/bin/env python3
"""导出"全节点清单"的原始数据:节点名 / 来源文件 / 入口函数 / 文档首行(只读)。

用法(必须用仓库 venv 的 python,因为要 import graph):
    <venv python> dump_inventory.py <仓库根目录> <graph_extract.json> <输出json>
说明:
  - 节点名、入口函数名来自 ast_graph.py 的提取结果(真实的 add_node 语句)。
  - 来源文件用 inspect 取得;定义在 _build_state_graph 内部的函数(如 generate_draft_wrapped)
    取不到,按"graph.py 内嵌"记录。
"""
import inspect
import json
import os
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
extract = Path(sys.argv[2]).resolve()   # 先转绝对路径:下面要 chdir 到仓库
out = Path(sys.argv[3]).resolve()
os.chdir(repo)
sys.path.insert(0, str(repo))
import graph as G  # noqa: E402

d = json.loads(extract.read_text(encoding="utf-8"))
inv = []
for i, nd in enumerate(d["nodes"], 1):
    fn = nd["fn"].lstrip("$")
    obj, ok = G, True
    for part in fn.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            ok = False
            break
    doc = ""
    src = "graph.py"
    if ok:
        lines = (inspect.getdoc(obj) or "").strip().splitlines()
        doc = lines[0] if lines else ""
        f = inspect.getsourcefile(obj)
        if f:
            src = Path(f).resolve().relative_to(repo).as_posix()
    inv.append({"n": i, "name": nd["name"], "file": src, "fn": fn, "doc": doc[:60]})
    print(f"{i:>2} {nd['name']:<42} {src:<52} {doc[:56]}")
out.write_text(json.dumps(inv, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\n共 {len(inv)} 个节点 -> {out}")
