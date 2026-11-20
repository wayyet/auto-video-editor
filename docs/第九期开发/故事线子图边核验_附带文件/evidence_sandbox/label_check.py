"""边标签注册表 / 节点元数据 与真实编译图的一致性检查(只读仓库)。

用法:
    venv/bin/python -I label_check.py <repo_root> <out_json>

做的事:
  1. 四种开关组合下建真实图,用 monitoring.workflow_graph.build_graph_schema 生成 schema。
  2. 列出 Storyline 区域内"没有标签"的边(按 4 组合并集)。
  3. 列出 _EDGE_LABELS 里"在 4 组合里都不存在的死键"。
  4. 对比 web/node_map/workflow.json 里 Storyline 边与真实图(AI 开)是否一致。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
out_json = Path(sys.argv[2]).resolve()
sys.dont_write_bytecode = True
sys.path.insert(0, str(repo))

import config as C  # noqa: E402
import graph as G  # noqa: E402
import monitoring.workflow_graph as WG  # noqa: E402


def build(ai_on: bool, qc_on: bool):
    orig_ai = G.is_ai_transition_enabled
    orig_qc = C.ASSEMBLY_QC_GATE_ENABLED
    try:
        G.is_ai_transition_enabled = lambda: ai_on
        C.ASSEMBLY_QC_GATE_ENABLED = qc_on
        return G._build_state_graph()
    finally:
        G.is_ai_transition_enabled = orig_ai
        C.ASSEMBLY_QC_GATE_ENABLED = orig_qc


def in_region(n: str) -> bool:
    return n.startswith("storyline_")


all_edges: dict[tuple[str, str], dict] = {}
per_combo: dict[str, list] = {}
for ai_on in (True, False):
    for qc_on in (True, False):
        key = f"ai{int(ai_on)}_qc{int(qc_on)}"
        g = build(ai_on, qc_on)
        compiled = g.compile()
        orig_qc = C.ASSEMBLY_QC_GATE_ENABLED
        try:
            C.ASSEMBLY_QC_GATE_ENABLED = qc_on
            schema = WG.build_graph_schema(compiled, C)
        finally:
            C.ASSEMBLY_QC_GATE_ENABLED = orig_qc
        rows = []
        for e in schema["edges"]:
            pair = (e["from"], e["to"])
            all_edges.setdefault(pair, {"kind": e["kind"], "label": e["label"], "combos": []})
            all_edges[pair]["combos"].append(key)
            rows.append((e["from"], e["to"], e["kind"], e["label"]))
        per_combo[key] = rows

region_edges = {p: v for p, v in all_edges.items() if in_region(p[0]) or in_region(p[1])}
unlabeled = sorted(p for p, v in region_edges.items() if not v["label"])
labeled = sorted(p for p, v in region_edges.items() if v["label"])
dead_keys = sorted(k for k in WG._EDGE_LABELS if k not in all_edges)

# 各组合下 Storyline 区域边数(含 START→入口 与 →generate_draft/END 出口)
region_count = {k: sum(1 for r in rows if in_region(r[0]) or in_region(r[1])) for k, rows in per_combo.items()}

# workflow.json 快照对照(AI 开)
snap_path = repo / "web" / "node_map" / "workflow.json"
snap = json.loads(snap_path.read_text(encoding="utf-8"))
snap_report = {"snapshot_keys": list(snap.keys()) if isinstance(snap, dict) else type(snap).__name__}


def find_edges(obj):
    """在快照里尽力找出 [{from,to,...}] 形式的边数组(结构不确定,宽松探测)。"""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "edges" and isinstance(v, list):
                out.append(v)
            else:
                out.extend(find_edges(v))
    elif isinstance(obj, list):
        for it in obj:
            out.extend(find_edges(it))
    return out


edge_lists = find_edges(snap)
snap_report["edge_array_lengths"] = [len(x) for x in edge_lists]
snap_pairs_sets = []
for lst in edge_lists:
    s = set()
    for e in lst:
        if isinstance(e, dict):
            a = e.get("from") or e.get("source") or e.get("src")
            b = e.get("to") or e.get("target") or e.get("dst")
            if a and b:
                s.add((a, b))
    snap_pairs_sets.append(s)
real_ai1 = {(r[0], r[1]) for r in per_combo["ai1_qc1"]}
real_ai1_region = {p for p in real_ai1 if in_region(p[0]) or in_region(p[1])}
snap_report["compare"] = []
for i, s in enumerate(snap_pairs_sets):
    sr = {p for p in s if in_region(p[0]) or in_region(p[1])}
    snap_report["compare"].append(
        {
            "edge_array_index": i,
            "snapshot_region_edges": len(sr),
            "real_ai1_region_edges": len(real_ai1_region),
            "only_in_snapshot": sorted(sr - real_ai1_region),
            "only_in_real": sorted(real_ai1_region - sr),
        }
    )

result = {
    "region_edge_count_per_combo": region_count,
    "region_edges_total_union": len(region_edges),
    "labeled": [list(p) + [region_edges[p]["label"]] for p in labeled],
    "unlabeled": [list(p) + [region_edges[p]["kind"]] for p in unlabeled],
    "dead_label_keys": [list(k) + [WG._EDGE_LABELS[k]] for k in dead_keys],
    "snapshot": snap_report,
}
out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

print("Storyline 区域边数(含入口/出口,按组合):", region_count)
print(f"区域边并集 {len(region_edges)} 条:有标签 {len(labeled)},无标签 {len(unlabeled)}")
print("\n[无标签的区域边]")
for p in unlabeled:
    print("  ", p[0], "->", p[1], f"({region_edges[p]['kind']})")
print("\n[_EDGE_LABELS 死键(4 组合里都不存在的边)]")
for k in dead_keys:
    print("  ", k[0], "->", k[1], "|", WG._EDGE_LABELS[k])
print("\n[workflow.json 快照对照]")
print("  ", json.dumps(snap_report, ensure_ascii=False)[:1500])
