r"""导出 Storyline 子图的边清单(只读:只建图,不编译、不执行、不写文件)。

用法(在仓库根目录的 PowerShell 里执行):
    .\.venv\Scripts\python.exe scripts\audit_storyline_edges.py

输出 AI 转场开 / 关两种配置下的:普通边、汇合屏障、条件出口,并统计箭头总数。
按 assembly QC 开(默认姿态)逐条列出;末尾两行分别是默认姿态的合计和 assembly QC 关时的合计(作对照)。
期望(基线 e29dd26):
    汇总:AI 开 = 24,AI 关 = 21
    (对照)assembly QC 关:AI 开 = 23,AI 关 = 20
若已应用文档附录 A.5 的补丁(join 条件路由删除 END 出口),应为 23 / 20 与 22 / 19。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 本脚本放在 <仓库根>/scripts/ 下;脚本目录会进 sys.path,但仓库根不会,所以手动加
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as C  # noqa: E402
import graph as G  # noqa: E402

START, END = "__start__", "__end__"


def _short(name: str) -> str:
    return name.removeprefix("storyline_")


def _is_storyline_edge(src: str, dst: str) -> bool:
    return src.startswith("storyline_") or dst.startswith("storyline_")


def dump(ai_on: bool, qc_on: bool, verbose: bool = True) -> int:
    # 两个开关都在【建图时】读取:临时改掉,建完立刻还原
    original_ai, original_qc = G.is_ai_transition_enabled, C.ASSEMBLY_QC_GATE_ENABLED
    G.is_ai_transition_enabled = lambda: ai_on
    C.ASSEMBLY_QC_GATE_ENABLED = qc_on  # JOIN 的出口随它变(开:多一条 -> assembly)
    try:
        g = G._build_state_graph()  # 只拿 builder,不 compile
    finally:
        G.is_ai_transition_enabled, C.ASSEMBLY_QC_GATE_ENABLED = original_ai, original_qc

    # 1) 普通边:builder.edges = {(源, 目标)}
    plain = sorted((s, t) for s, t in g.edges if _is_storyline_edge(s, t))
    # 2) 汇合屏障:builder.waiting_edges = {((源1, 源2, ...), 目标)}
    barriers = sorted(
        (tuple(sorted(srcs)), dst)
        for srcs, dst in getattr(g, "waiting_edges", set())
        if dst.startswith("storyline_")
    )
    # 3) 条件出口:builder.branches = {源: {名字: BranchSpec(.ends = {标签: 目的地})}}
    conditional: list[tuple[str, str]] = []
    for src, specs in getattr(g, "branches", {}).items():
        if not src.startswith("storyline_"):
            continue
        for spec in specs.values():
            ends = getattr(spec, "ends", None) or {}
            conditional.extend((src, dst) for dst in ends.values())
    conditional.sort()

    total = len(plain) + sum(len(srcs) for srcs, _ in barriers) + len(conditional)
    if verbose:
        print(f"\n===== AI 转场 {'开' if ai_on else '关'}(assembly QC {'开' if qc_on else '关'})=====")
        print(f"普通边 {len(plain)} 条:")
        for s, t in plain:
            print(f"  {_short(s)} -> {_short(t)}")
        n_arrows = sum(len(srcs) for srcs, _ in barriers)
        print(f"汇合屏障 {len(barriers)} 个(共 {n_arrows} 条箭头):")
        for srcs, dst in barriers:
            print(f"  [{', '.join(_short(x) for x in srcs)}] -> {_short(dst)}")
        print(f"条件出口 {len(conditional)} 条:")
        for s, t in conditional:
            print(f"  {_short(s)} -?-> {_short(t)}")
        print(f"合计箭头 {total} 条")
    return total


if __name__ == "__main__":
    totals = {on: dump(on, True) for on in (True, False)}
    print(f"\n汇总:AI 开 = {totals[True]},AI 关 = {totals[False]}")
    qc_off = {on: dump(on, False, verbose=False) for on in (True, False)}
    print(f"(对照)assembly QC 关:AI 开 = {qc_off[True]},AI 关 = {qc_off[False]}")
