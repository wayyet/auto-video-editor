"""R6 实测:同一张"被改坏"的图,团队现有 D10 守卫 vs T1 守卫。只读,不写仓库。
用法: python -I team_guard_vacuous.py <repo_root> <ref_tests_dir>
改坏方式 = T1 的 _m_sw_conditional_in_barrier:SW 改回条件触发,却仍留在 [STR,SW]→GS 屏障里(历史 D10)。
团队守卫逻辑原样照抄自 tests/integration/test_unreachable_nodes_wiring.py:377-389(CONDITIONAL_NODES 取该文件 :63 的值 set())。
"""
import importlib.util
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
ref = Path(sys.argv[2]).resolve()
sys.dont_write_bytecode = True
sys.path.insert(0, str(repo))

import config as C  # noqa: E402
import graph as G  # noqa: E402

spec = importlib.util.spec_from_file_location("t1", ref / "test_storyline_data_flow_must_set.py")
T1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T1)

CONDITIONAL_NODES: set[str] = set()  # 与 tests/integration/test_unreachable_nodes_wiring.py:63 相同


def team_guard(g) -> list[str]:
    bad_all = []
    for sources, target in getattr(g, "waiting_edges", set()):
        bad = set(sources) & CONDITIONAL_NODES
        if bad:
            bad_all.append(f"条件节点 {sorted(bad)} 出现在通往 {target} 的 fan-in 屏障里")
    return bad_all


def build():
    o_ai, o_qc = G.is_ai_transition_enabled, C.ASSEMBLY_QC_GATE_ENABLED
    try:
        G.is_ai_transition_enabled = lambda: True
        C.ASSEMBLY_QC_GATE_ENABLED = True
        return G._build_state_graph()
    finally:
        G.is_ai_transition_enabled, C.ASSEMBLY_QC_GATE_ENABLED = o_ai, o_qc


for title, mut in (("原图(对照)", None), ("SW 改回条件触发但仍在屏障(历史 D10)", T1._m_sw_conditional_in_barrier),
                   ("SS(重试点上游)进 PTP 屏障(历史 D10 事故)", T1._m_ss_in_barrier)):
    g = build()
    if mut:
        mut(g)
    team = team_guard(g)
    t1 = T1.check_barrier_sources(g)
    print(f"{title}\n   团队 D10 守卫违规 {len(team)} 条 | T1 屏障守卫违规 {len(t1)} 条")
    for x in t1[:2]:
        print("      T1 ·", x[:140])
