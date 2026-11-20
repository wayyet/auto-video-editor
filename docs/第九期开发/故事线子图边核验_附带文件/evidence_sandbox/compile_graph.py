"""编译真实 LangGraph 图并导出拓扑 + 结构分析(只读,不写仓库)。

用法(必须在仓库目录之外执行;仓库路径通过参数传入):
    venv/bin/python -I compile_graph.py <repo_root> <out_json> <ast_edges_json>

做的事:
  1. 检查仓库顶层名没有遮蔽标准库,再把仓库根加入 sys.path(-I 模式不会自动加)。
  2. import graph,按 (AI 转场开关, assembly QC 开关) 四种组合调用
     graph._build_state_graph(),从 StateGraph 构建器读出权威拓扑:
       builder.edges           普通单源边
       builder.waiting_edges   列表式汇合屏障 (sources_tuple, target)
       builder.branches        条件边 {src: {name: BranchSpec(path, ends)}}
  3. 与 ast_edges.json(纯静态解析)交叉比对,证明两条独立路径得到同一边集。
  4. 结构分析:不可达节点、回边(环)、必经集合 must、屏障冗余源、D10 合规。
"""
from __future__ import annotations

import ast
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

START, END = "__start__", "__end__"


def die(msg: str) -> None:
    print("FATAL:", msg)
    raise SystemExit(2)


def main() -> int:
    repo = Path(sys.argv[1]).resolve()
    out_json = Path(sys.argv[2]).resolve()
    ast_json = Path(sys.argv[3]).resolve()

    # ---- 1. 防止仓库顶层名遮蔽标准库 ----
    tops = set()
    for p in repo.iterdir():
        if p.name.startswith("."):
            continue
        tops.add(p.stem if p.is_file() and p.suffix == ".py" else p.name)
    shadow = sorted(n for n in tops if n in sys.stdlib_module_names)
    print("仓库顶层名遮蔽标准库:", shadow or "无")
    if shadow:
        die(f"仓库顶层名与标准库同名: {shadow}")

    sys.dont_write_bytecode = True
    sys.path.insert(0, str(repo))

    import config as C  # noqa: E402
    import graph as G  # noqa: E402

    # ---- 2. 四种开关组合下建图 ----
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

    def router_name(spec) -> str:
        p = spec.path
        fn = getattr(p, "func", None) or getattr(p, "afunc", None)
        return getattr(fn, "__qualname__", None) or getattr(p, "name", None) or repr(p)

    def extract(g) -> dict:
        plain = sorted([a, b] for a, b in g.edges)
        waiting = sorted(
            ([list(s), t] for s, t in g.waiting_edges), key=lambda x: (x[1], x[0])
        )
        cond = []
        for src, d in sorted(g.branches.items()):
            for name, spec in d.items():
                cond.append(
                    {
                        "src": src,
                        "branch": name,
                        "router": router_name(spec),
                        "ends": {str(k): v for k, v in (spec.ends or {}).items()},
                    }
                )
        return {
            "nodes": sorted(g.nodes),
            "plain": plain,
            "waiting": waiting,
            "cond": cond,
        }

    combos = {}
    for ai_on in (True, False):
        for qc_on in (True, False):
            key = f"ai{int(ai_on)}_qc{int(qc_on)}"
            g = build(ai_on, qc_on)
            topo = extract(g)
            # 能真实 compile 才算有效拓扑(无 checkpointer,仅校验结构)
            compiled = g.compile()
            dg = compiled.get_graph()
            topo["compile_ok"] = True
            topo["get_graph_edge_pairs"] = sorted(
                {(e.source, e.target) for e in dg.edges}
            )
            topo["get_graph_edge_pairs"] = [list(x) for x in topo["get_graph_edge_pairs"]]
            combos[key] = topo
            print(
                f"[{key}] nodes={len(topo['nodes'])} plain={len(topo['plain'])} "
                f"waiting={len(topo['waiting'])} cond_branches={len(topo['cond'])} "
                f"get_graph_edges={len(topo['get_graph_edge_pairs'])}"
            )

    # ---- 3. 与纯静态 AST 结果交叉比对(默认组合 ai=1, qc=1) ----
    recs = json.loads(ast_json.read_text(encoding="utf-8"))

    def walk(node: ast.AST, ai_on: bool):
        """仅允许字符串常量 / 列表 / 字典 / START / END / _plan_for_qa,其余一律拒绝。"""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.List):
            return [walk(e, ai_on) for e in node.elts]
        if isinstance(node, ast.Dict):
            return {walk(k, ai_on): walk(v, ai_on) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.Name):
            if node.id == "START":
                return START
            if node.id == "END":
                return END
            if node.id == "_plan_for_qa":
                return (
                    "storyline_plan_timeline_ai_transition"
                    if ai_on
                    else "storyline_plan_timeline_pro"
                )
        raise ValueError(f"不允许的表达式: {ast.dump(node)[:80]}")

    def ast_edges(ai_on: bool, qc_on: bool):
        plain, waiting, cond = set(), set(), set()
        for r in recs:
            conds = r["conds"]
            ok = True
            for c in conds:
                if c == "_config.ASSEMBLY_QC_GATE_ENABLED":
                    ok &= qc_on
                elif c == "not (_config.ASSEMBLY_QC_GATE_ENABLED)":
                    ok &= not qc_on
                elif c == "is_ai_transition_enabled()":
                    ok &= ai_on
                else:
                    raise ValueError(f"未识别的 if 条件: {c}")
            if not ok:
                continue
            if r["kind"] == "add_edge":
                a = walk(ast.parse(r["args"][0], mode="eval").body, ai_on)
                b = walk(ast.parse(r["args"][1], mode="eval").body, ai_on)
                if isinstance(a, list):
                    waiting.add((tuple(a), b))
                else:
                    plain.add((a, b))
            elif r["kind"] == "add_conditional_edges":
                src = walk(ast.parse(r["args"][0], mode="eval").body, ai_on)
                m = walk(ast.parse(r["args"][2], mode="eval").body, ai_on)
                for ret, tgt in m.items():
                    cond.add((src, ret, tgt))
        return plain, waiting, cond

    crosscheck = {}
    for key, topo in combos.items():
        ai_on = key.startswith("ai1")
        qc_on = key.endswith("qc1")
        ap, aw, ac = ast_edges(ai_on, qc_on)
        bp = {tuple(x) for x in topo["plain"]}
        bw = {(tuple(s), t) for s, t in topo["waiting"]}
        bc = {
            (c["src"], k, v)
            for c in topo["cond"]
            for k, v in c["ends"].items()
        }
        # 列表屏障:构建器按调用顺序保存元组,AST 同序,这里按集合比较
        bw_n = {(tuple(sorted(s)), t) for s, t in bw}
        aw_n = {(tuple(sorted(s)), t) for s, t in aw}
        crosscheck[key] = {
            "plain_equal": ap == bp,
            "waiting_equal": aw_n == bw_n,
            "cond_equal": ac == bc,
            "plain_only_ast": sorted(ap - bp),
            "plain_only_builder": sorted(bp - ap),
            "waiting_only_ast": sorted(aw_n - bw_n),
            "waiting_only_builder": sorted(bw_n - aw_n),
            "cond_only_ast": sorted(ac - bc),
            "cond_only_builder": sorted(bc - ac),
        }
        print(
            f"[交叉比对 {key}] 普通边一致={crosscheck[key]['plain_equal']} "
            f"屏障一致={crosscheck[key]['waiting_equal']} "
            f"条件边一致={crosscheck[key]['cond_equal']}"
        )

    # ---- 4. 结构分析 ----
    def analyze(topo: dict) -> dict:
        nodes = set(topo["nodes"]) | {START, END}
        # 统一成带类型的边: (src, dst, kind)
        E = []
        for a, b in topo["plain"]:
            E.append((a, b, "plain"))
        for srcs, t in topo["waiting"]:
            for s in srcs:
                E.append((s, t, "join"))
        for c in topo["cond"]:
            for _ret, tgt in c["ends"].items():
                E.append((c["src"], tgt, "cond"))

        succ = defaultdict(list)
        for a, b, k in E:
            succ[a].append(b)
        for v in succ.values():
            v.sort()

        # 可达性
        def reach(root, adj):
            seen, stack = {root}, [root]
            while stack:
                x = stack.pop()
                for y in adj.get(x, []):
                    if y not in seen:
                        seen.add(y)
                        stack.append(y)
            return seen

        from_start = reach(START, succ)
        pred = defaultdict(list)
        for a, b, k in E:
            pred[b].append(a)
        to_end = reach(END, pred)

        unreachable = sorted(n for n in nodes if n not in from_start)
        cannot_end = sorted(
            n for n in nodes if n in from_start and n not in to_end and n != END
        )

        # 回边(DFS 栈内命中)
        back = set()
        state = {}

        def dfs(u):
            state[u] = 1
            for v in succ.get(u, []):
                if state.get(v) == 1:
                    back.add((u, v))
                elif v not in state:
                    dfs(v)
            state[u] = 2

        sys.setrecursionlimit(10000)
        dfs(START)

        # ---- must[n]:n 启动时"本轮必已完成"的节点集合(去掉回边后的 DAG)----
        # 触发(trigger)语义:
        #   plain/cond 入边  = 一个独立触发,来源任一完成即触发(OR)
        #   waiting 入边     = 一个汇合触发,所有 sources 完成才触发(AND)
        # must(n) = 各触发 must 的交集;汇合触发的 must = 各 source 的 must∪{source} 的并集
        triggers = defaultdict(list)  # n -> [ ("edge", src) | ("join", [srcs]) ]
        for a, b, k in E:
            if (a, b) in back or k == "join":
                continue
            triggers[b].append(("edge", [a]))
        for srcs, t in topo["waiting"]:
            if any((s, t) in back for s in srcs):
                continue
            triggers[t].append(("join", list(srcs)))

        must: dict[str, set] = {START: set()}
        order = []
        temp = set()
        perm = set()

        def topo_visit(n):
            if n in perm:
                return
            if n in temp:
                raise RuntimeError("去掉回边后仍有环: " + n)
            temp.add(n)
            for kind, srcs in triggers.get(n, []):
                for s in srcs:
                    if s in nodes:
                        topo_visit(s)
            temp.discard(n)
            perm.add(n)
            order.append(n)

        for n in sorted(from_start):
            topo_visit(n)

        for n in order:
            if n == START:
                continue
            tr = triggers.get(n, [])
            if not tr:
                must[n] = set()
                continue
            sets = []
            for kind, srcs in tr:
                if kind == "edge":
                    s = srcs[0]
                    sets.append(must.get(s, set()) | {s})
                else:
                    u = set()
                    for s in srcs:
                        u |= must.get(s, set()) | {s}
                    sets.append(u)
            inter = set.intersection(*sets) if sets else set()
            must[n] = inter

        # ---- 屏障冗余源 ----
        barrier_report = []
        for srcs, t in topo["waiting"]:
            minimal = []
            redundant = {}
            for s in srcs:
                implied_by = [
                    o
                    for o in srcs
                    if o != s and s in must.get(o, set())
                ]
                if implied_by:
                    redundant[s] = sorted(implied_by)
                else:
                    minimal.append(s)
            barrier_report.append(
                {
                    "target": t,
                    "sources": list(srcs),
                    "minimal_sources": minimal,
                    "redundant_sources": redundant,
                }
            )

        # ---- 多触发节点(可能多次触发的候选) ----
        multi = {}
        for n, tr in triggers.items():
            if len(tr) >= 2:
                multi[n] = [
                    {"kind": k, "sources": s}
                    for k, s in tr
                ]
        # 回边目标的额外触发
        back_targets = defaultdict(list)
        for a, b in back:
            back_targets[b].append(a)

        return {
            "edges_typed": [list(e) for e in sorted(E)],
            "unreachable_from_START": unreachable,
            "cannot_reach_END": cannot_end,
            "back_edges": sorted([list(x) for x in back]),
            "must": {n: sorted(v) for n, v in must.items()},
            "barriers": barrier_report,
            "multi_trigger_nodes": multi,
            "back_targets": {k: sorted(v) for k, v in back_targets.items()},
        }

    analysis = {k: analyze(v) for k, v in combos.items()}

    # ---- D10:屏障源必须"每轮必跑"(在 QA 回路体内且 ∈ must[QA]∪{QA}) ----
    d10 = {}
    for key, an in analysis.items():
        qa = "storyline_qa_gate"
        must_qa = set(an["must"].get(qa, [])) | {qa}
        gc = "storyline_group_clips"
        body = {n for n, m in an["must"].items() if gc in m} | {gc}
        rows = []
        for b in an["barriers"]:
            if not b["target"].startswith("storyline_"):
                continue
            for s in b["sources"]:
                rows.append(
                    {
                        "target": b["target"],
                        "source": s,
                        "in_loop_body": s in body,
                        "runs_every_round(must[QA])": s in must_qa,
                    }
                )
        d10[key] = rows

    result = {
        "repo": str(repo),
        "combos": combos,
        "crosscheck": crosscheck,
        "analysis": analysis,
        "d10": d10,
    }
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", out_json)

    # ---- 打印默认组合的关键结果 ----
    key = "ai1_qc1"
    an = analysis[key]
    print("\n===== 默认组合(AI 开 / QC 开)=====")
    print("不可达(从 START):", an["unreachable_from_START"])
    print("无法到达 END    :", an["cannot_reach_END"] if "cannot_reach_END" in an else an["cannot_reach_END"])
    print("回边:", an["back_edges"])
    print("\n-- 多触发节点(≥2 个独立触发) --")
    for n, tr in sorted(an["multi_trigger_nodes"].items()):
        print(" ", n, tr)
    print("\n-- 屏障冗余分析 --")
    for b in an["barriers"]:
        print(
            f"  → {b['target']}\n      sources={b['sources']}\n"
            f"      minimal={b['minimal_sources']}\n      redundant={b['redundant_sources']}"
        )
    print("\n-- D10 检查(Storyline 屏障源是否每轮必跑) --")
    bad = [r for r in d10[key] if not (r["in_loop_body"] and r["runs_every_round(must[QA])"])]
    print("  违规条数:", len(bad))
    for r in bad:
        print("  ", r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
