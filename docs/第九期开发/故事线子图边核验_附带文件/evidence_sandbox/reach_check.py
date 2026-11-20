"""四种开关组合下的可达性核验(只读:只建图,不编译、不执行、不写文件)。

用法(在仓库目录之外执行;仓库路径作为参数传入):
    python -I reach_check.py <仓库根目录>

对 (AI 转场开关 × assembly QC 开关) 四种组合,分别建图后统计:
  - 从 __start__ 不可达的节点;
  - 可达、但到不了 __end__ 的节点。
把普通边、汇合屏障的每个来源、条件出口的每个目标都当作一条可走的箭头。
期望(基线 e29dd26):默认组合(AI 开 / QC 开)全部可达;AI 关时 GAT、PTAT 不可达(注册但不接线,设计如此);
QC 关时 6 个 assembly 节点不可达(不接线,设计如此);任何组合下都没有"到不了 END"的节点。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True
repo = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo))  # -I 模式不会自动加仓库根

import config as C  # noqa: E402
import graph as G  # noqa: E402


def build(ai_on: bool, qc_on: bool):
    # 两个开关都在【建图时】读取:临时改掉,建完立刻还原
    old_ai, old_qc = G.is_ai_transition_enabled, C.ASSEMBLY_QC_GATE_ENABLED
    G.is_ai_transition_enabled = lambda: ai_on
    C.ASSEMBLY_QC_GATE_ENABLED = qc_on
    try:
        return G._build_state_graph()
    finally:
        G.is_ai_transition_enabled, C.ASSEMBLY_QC_GATE_ENABLED = old_ai, old_qc


def reach(start: str, adjacency: dict[str, set[str]]) -> set[str]:
    seen, stack = {start}, [start]
    while stack:
        node = stack.pop()
        for nxt in adjacency.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


for ai in (True, False):
    for qc in (True, False):
        g = build(ai, qc)
        arrows = list(g.edges)
        for srcs, dst in g.waiting_edges:
            arrows += [(s, dst) for s in srcs]
        for src, named in g.branches.items():
            for spec in named.values():
                arrows += [(src, dst) for dst in (getattr(spec, "ends", None) or {}).values()]
        succ: dict[str, set[str]] = {}
        pred: dict[str, set[str]] = {}
        for s, t in arrows:
            succ.setdefault(s, set()).add(t)
            pred.setdefault(t, set()).add(s)
        from_start = reach("__start__", succ)
        to_end = reach("__end__", pred)
        nodes = set(g.nodes)
        unreachable = sorted(n for n in nodes if n not in from_start)
        cannot_end = sorted(n for n in nodes if n in from_start and n not in to_end)
        print(f"AI={ai!s:5} QC={qc!s:5} 节点 {len(nodes)} 个 | 不可达 {len(unreachable)} 个 {unreachable} | 到不了 END {cannot_end}")
