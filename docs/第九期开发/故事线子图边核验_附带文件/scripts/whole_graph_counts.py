r"""整图箭头统计(只读:只建图,不编译、不执行、不写文件)。

用法(在仓库根目录的 PowerShell 里执行):
    .\.venv\Scripts\python.exe scripts\whole_graph_counts.py

按区域统计三种箭头:普通边 / 汇合屏障的每个来源 / 条件出口。
区域归属规则:箭头起点以 storyline_ 或 assembly_ 开头 → 计到该区域;
否则(外层节点或 __start__)按终点所在区域计。
期望(基线 e29dd26,AI 转场开 + assembly QC 开):节点 45 个,箭头 59 条
(storyline 24 / outer 27 / assembly 8)。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True
# 本脚本放在 <仓库根>/scripts/ 下;仓库根不在 sys.path 里,所以手动加(也可用第 1 个参数指定仓库根)
repo = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))

import config as C  # noqa: E402
import graph as G  # noqa: E402


def region(name: str) -> str:
    if name.startswith("storyline_"):
        return "storyline"
    if name.startswith("assembly_"):
        return "assembly"
    return "outer"


def count(ai_on: bool, qc_on: bool):
    # 两个开关都在【建图时】读取:临时改掉,建完立刻还原
    old_ai, old_qc = G.is_ai_transition_enabled, C.ASSEMBLY_QC_GATE_ENABLED
    G.is_ai_transition_enabled = lambda: ai_on
    C.ASSEMBLY_QC_GATE_ENABLED = qc_on
    try:
        g = G._build_state_graph()  # 只拿 builder,不 compile
    finally:
        G.is_ai_transition_enabled, C.ASSEMBLY_QC_GATE_ENABLED = old_ai, old_qc

    arrows = []  # (起点, 终点, 种类)
    for s, t in g.edges:
        arrows.append((s, t, "plain"))
    for srcs, t in g.waiting_edges:
        for s in srcs:
            arrows.append((s, t, "barrier"))
    for s, specs in g.branches.items():
        for spec in specs.values():
            for t in (getattr(spec, "ends", None) or {}).values():
                arrows.append((s, t, "cond"))

    by_region: Counter = Counter()
    by_kind: Counter = Counter()
    for s, t, kind in arrows:
        r = region(s) if s.startswith(("storyline_", "assembly_")) else region(t)
        by_region[r] += 1
        by_kind[(r, kind)] += 1
    ordered = {k: by_region[k] for k in ("storyline", "outer", "assembly") if k in by_region}  # 固定顺序,输出可复现
    return len(arrows), ordered, {f"{r}/{k}": v for (r, k), v in sorted(by_kind.items())}, len(g.nodes)


for ai, qc in ((True, True), (False, True), (True, False)):
    total, by_region, kinds, n_nodes = count(ai, qc)
    print(f"AI={ai} QC={qc}: 节点 {n_nodes} 个, 箭头 {total} 条, 按区域 {by_region}")
    print("   ", kinds)
