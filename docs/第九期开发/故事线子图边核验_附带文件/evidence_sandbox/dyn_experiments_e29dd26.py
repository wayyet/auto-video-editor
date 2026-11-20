"""动态实测(e29dd26 基线版):真实拓扑 + 确定性桩节点(只读仓库;产物只写 scratchpad)。

第六期 P1-1 之后 JOIN 的成功出口随 ASSEMBLY_QC_GATE_ENABLED 而定(QC 开 → assembly_discover_and_probe,
QC 关 → generate_draft),所以:A–G 默认在 QC 开的真实图上跑、在 assembly_discover_and_probe 之前截停;
新增 H(建图/运行时 QC 开关不一致)与 I(QC 关的子矩阵,证明 Storyline 本体与 QC 开关无关)。

用法:
    venv/bin/python -I dyn_experiments_e29dd26.py <repo_root> <out_json> <tmp_dir> [旧基线 6679e54 的 compiled_topology.json]

实验:
  A  真实拓扑(HEAD)场景矩阵:检索产物 有/无 × AI 转场 开/关 × QA 重试 0/1/3 轮 = 12 组,
     统计每轮每个节点的执行次数 + 每个消费者读到的上游产物"是否本轮新鲜"。
  B1 反向还原"第5期阶段1 的 4 处约简"(还原成 6679e54 的屏障形态)→ 与 HEAD 逐超步比对。
  B2 反向还原"第5期阶段5 的检索并行化"(还原成 STR 条件路由 → SW → GS)→ 与 HEAD 比对(预期:
     检索开启时每轮多 1 个超步;其余等价)。并断言 B1+B2 全还原后的 Storyline 边集 == 6679e54 旧基线。
  C  负对照(把屏障拆成独立单源边 / 丢掉来源 / 让来源变慢):
       C1  [RTR,TXT]→PTP  拆成独立边        C2/C2b  [PTP,GAT]→PTAT 拆成独立边(含 GAT 变慢)
       C3  [STR,SW]→GS    拆成独立边        C4      [STR,SW]→GS    丢掉 SW
       (C3/C4 各含"SW 变慢 1 个超步"的变体,验证屏障才是跨时序变化的保证)
  D  D10:把 SW 改回"条件触发"(GC→SW|END)但仍留在屏障 [STR,SW]→GS,第 2 轮不触发 → 静默终止;
     对照:真实图里 SW 第 2 轮"软跳过"(每轮必跑、不写产物)→ 正常走完。
  E  storyline_join 实函数 × 8 种 timeline 文件状态 → _route_after_storyline_join 结果。
  F  generate_voiceover 失败不覆盖旧 artifact(真实节点 + InMemorySaver 两轮)。
  F2 同 F 的图级版本:在真实拓扑里让 GV 第 2 轮失败(keep=现状 / clear=写 "") → SB/PTP 读到什么。
  G  search_web_topic 软跳过不覆盖旧 artifact(真实节点两轮 + 图级 keep/clear 对照)。
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
out_json = Path(sys.argv[2]).resolve()
tmp_dir = Path(sys.argv[3]).resolve()
old_topo_path = Path(sys.argv[4]).resolve() if len(sys.argv) > 4 else None
tmp_dir.mkdir(parents=True, exist_ok=True)

sys.dont_write_bytecode = True
sys.path.insert(0, str(repo))

import config as C  # noqa: E402
import graph as G  # noqa: E402
import nodes.storyline.join_storyline as JN  # noqa: E402
import nodes.storyline.qa_gate as QA  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

STOP_NODE = "assembly_discover_and_probe"  # QC 开时 JOIN 之后的第一站
LM, SS, UC, FC = (
    "storyline_load_media",
    "storyline_split_shots",
    "storyline_understand_clips",
    "storyline_filter_clips",
)
GC, GS, GV = "storyline_group_clips", "storyline_generate_script", "storyline_generate_voiceover"
SB, RTR, TXT = "storyline_select_bgm", "storyline_recommend_transition", "storyline_recommend_text"
PTP, GAT, PTAT = (
    "storyline_plan_timeline_pro",
    "storyline_generate_ai_transition",
    "storyline_plan_timeline_ai_transition",
)
SW, STR = "storyline_search_web_topic", "storyline_script_template_recommendation"
QAG, JOIN = "storyline_qa_gate", "storyline_join"

TARGETS = {  # 注册名 -> (patch 对象, 属性名)
    LM: (G, "storyline_load_media_node"),
    SS: (G, "storyline_split_shots_node"),
    UC: (G, "storyline_understand_clips_node"),
    FC: (G, "storyline_filter_clips_node"),
    GC: (G, "storyline_group_clips_node"),
    STR: (G, "storyline_script_template_recommendation_node"),
    SW: (G, "storyline_search_web_topic_node"),
    GS: (G, "storyline_generate_script_node"),
    GV: (G, "storyline_generate_voiceover_node"),
    SB: (G, "storyline_select_bgm_node"),
    RTR: (G, "storyline_recommend_transition_node"),
    TXT: (G, "storyline_recommend_text_node"),
    PTP: (G, "storyline_plan_timeline_pro_node"),
    GAT: (G, "storyline_generate_ai_transition_node"),
    PTAT: (G, "storyline_plan_timeline_ai_transition_node"),
    QAG: (QA, "storyline_qa_gate_node"),
    JOIN: (JN, "storyline_join_node"),
}

# 桩节点产出:注册名 -> state 键(值带 @r<轮次> 戳)
PRODUCES = {
    LM: "storyline_media_artifact",
    SS: "storyline_shots_artifact",
    UC: "storyline_understanding_artifact",
    FC: "storyline_filtered_clips",
    GC: "storyline_groups_artifact",
    STR: "storyline_script_template_artifact",
    SW: "storyline_web_topic_artifact",
    GS: "storyline_script_artifact",
    GV: "storyline_voiceover_artifact",
    SB: "storyline_bgm_selection",
    RTR: "storyline_transition_plan",
    TXT: "storyline_text_style_plan",
    PTP: "storyline_timeline_plan",
    GAT: "storyline_ai_transition_artifact",
    PTAT: "storyline_timeline_plan",
}
DICT_KEYS = {"storyline_bgm_selection", "storyline_transition_plan", "storyline_text_style_plan"}
WEB_KEY = "storyline_web_topic_artifact"
GV_KEY = "storyline_voiceover_artifact"

# 消费者 -> 它实际读取的"循环体内上游产物键"(循环体外的 UC/LM/SS 产物不带轮次戳,不在此列)
CONSUMES = {
    STR: ["storyline_groups_artifact"],
    SW: ["storyline_groups_artifact"],
    GS: ["storyline_groups_artifact", "storyline_script_template_artifact", WEB_KEY],
    GV: ["storyline_script_artifact"],
    SB: ["storyline_script_artifact", GV_KEY],
    RTR: ["storyline_groups_artifact", "storyline_bgm_selection"],
    TXT: ["storyline_groups_artifact", "storyline_script_artifact"],
    PTP: [
        "storyline_groups_artifact",
        "storyline_script_artifact",
        GV_KEY,
        "storyline_bgm_selection",
        "storyline_transition_plan",
        "storyline_text_style_plan",
    ],
    GAT: ["storyline_groups_artifact", "storyline_transition_plan"],
    PTAT: ["storyline_timeline_plan", "storyline_ai_transition_artifact"],
    QAG: ["storyline_timeline_plan"],
}


def stamp_of(val):
    """从桩节点写入的值里取出 @r<轮次>;缺失返回 None。"""
    if isinstance(val, dict):
        val = val.get("stamp")
    if isinstance(val, str):
        m = re.search(r"@r(\d+)$", val)
        if m:
            return int(m.group(1))
    return None


class Harness:
    def __init__(self, *, web, qa_retries, fail_gv_round=None, gv_mode="keep",
                 sw_skip_rounds=(), sw_mode="keep"):
        self.web = web
        self.round = 0
        self.qa_retries = qa_retries
        self.qa_done = 0
        self.reads = defaultdict(list)  # node -> [(round, {key: stamp})]
        self.fail_gv_round = fail_gv_round
        self.gv_mode = gv_mode
        self.sw_skip_rounds = set(sw_skip_rounds)
        self.sw_mode = sw_mode

    def expected_missing(self, key, rnd):
        """该轮该键"本来就不该有产物"(生产者合法地没写)→ 读到 None 不算缺失。"""
        if key == WEB_KEY and (not self.web or rnd in self.sw_skip_rounds):
            return True
        if key == GV_KEY and self.fail_gv_round == rnd:
            return True
        return False

    def make_stub(self, name):
        key = PRODUCES.get(name)

        def stub(state):
            if name == GC:
                self.round += 1
            if name in CONSUMES:
                self.reads[name].append(
                    (self.round, {k: stamp_of(state.get(k)) for k in CONSUMES[name]})
                )
            if name == QAG:
                if self.qa_done < self.qa_retries:
                    self.qa_done += 1
                    return {"storyline_qa_route_signal": "storyline_qa_retry"}
                return {"storyline_qa_route_signal": "storyline_qa_done"}
            if name == JOIN:
                return {"storyline_plan": {"clips": [1]}}
            if name == GV and self.fail_gv_round == self.round:  # 模拟 GV 失败
                return {GV_KEY: ""} if self.gv_mode == "clear" else {}
            if name == SW and ((not self.web) or self.round in self.sw_skip_rounds):  # 软跳过
                return {WEB_KEY: ""} if (self.sw_mode == "clear" and self.web) else {}
            stamp = f"{key}@r{self.round}"
            val = {"stamp": stamp} if key in DICT_KEYS else stamp
            return {key: val}

        stub.__name__ = f"stub_{name}"
        return stub


def run(*, web, ai, retries, mutate=None, fail_gv_round=None, gv_mode="keep",
        sw_skip_rounds=(), sw_mode="keep", recursion_limit=500, qc_build=True, qc_run=None):
    h = Harness(web=web, qa_retries=retries, fail_gv_round=fail_gv_round, gv_mode=gv_mode,
                sw_skip_rounds=sw_skip_rounds, sw_mode=sw_mode)
    saved = {n: getattr(o, a) for n, (o, a) in TARGETS.items()}
    saved_ai = G.is_ai_transition_enabled
    saved_qc = C.ASSEMBLY_QC_GATE_ENABLED
    try:
        for n, (o, a) in TARGETS.items():
            setattr(o, a, h.make_stub(n))
        G.is_ai_transition_enabled = lambda: ai
        C.ASSEMBLY_QC_GATE_ENABLED = qc_build          # 建图时的 QC 开关
        g = G._build_state_graph()
        if mutate:
            mutate(g, ai)
        C.ASSEMBLY_QC_GATE_ENABLED = qc_build if qc_run is None else qc_run   # 运行时的 QC 开关
        compiled = g.compile(checkpointer=InMemorySaver(), interrupt_before=[STOP_NODE, "generate_draft"])
        cfg = {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": recursion_limit}
        events = []
        err = None
        try:
            for ev in compiled.stream({"session_id": "s"}, cfg, stream_mode="debug"):
                if ev["type"] == "task":
                    p = ev["payload"]
                    events.append((ev["step"], p["name"], tuple(p["triggers"])))
        except Exception as e:  # noqa: BLE001
            err = repr(e)[:200]
        snap = compiled.get_state(cfg)
        return h, events, tuple(snap.next), err
    finally:
        for n, (o, a) in TARGETS.items():
            setattr(o, a, saved[n])
        G.is_ai_transition_enabled = saved_ai
        C.ASSEMBLY_QC_GATE_ENABLED = saved_qc


def summarize(h, events, nxt, err):
    per_round = defaultdict(Counter)
    rnd = 0
    steps = defaultdict(list)
    for step, name, _trig in events:
        if name == GC:
            rnd += 1
        per_round[rnd][name] += 1
        steps[step].append(name)
    sig = [sorted(steps[s]) for s in sorted(steps)]
    bad = []
    for node, lst in h.reads.items():
        for r, stamps in lst:
            for k, st in stamps.items():
                if st is None:
                    if not h.expected_missing(k, r):
                        bad.append({"node": node, "round": r, "key": k, "read_stamp": None, "kind": "缺失"})
                elif st != r:
                    bad.append({"node": node, "round": r, "key": k, "read_stamp": st, "kind": "陈旧"})
    return {
        "rounds": rnd,
        "per_round": {r: dict(c) for r, c in per_round.items()},
        "next": list(nxt),
        "error": err,
        "max_step": max(steps) if steps else 0,
        "stale_reads": bad,
        "signature": sig,
        "triggers": sorted({(n, t) for _s, n, t in events if n in (GS, SB, RTR, PTP, GAT, PTAT)}),
    }


LOOP_NODES_BASE = [GC, STR, SW, GS, GV, TXT, SB, RTR, PTP, QAG]  # SW 每轮必跑(软跳过在节点内部)


def expected_per_round(ai):
    exp = {n: 1 for n in LOOP_NODES_BASE}
    if ai:
        exp[GAT] = 1
        exp[PTAT] = 1
    return exp


# ---------------------------------------------------------------- 变体(改建造器)
def _find_wait(g, srcs, dst):
    for s, t in list(g.waiting_edges):
        if t == dst and set(s) == set(srcs):
            return (s, t)
    raise AssertionError(f"未找到屏障 {srcs}->{dst}")


def mut_pre_reduction(g, ai):
    """B1:把"第5期阶段1 的 4 处约简"还原成 6679e54 的屏障形态(反向对照)。"""
    g.edges.discard((GV, SB))
    g.add_edge([GS, GV], SB)
    g.edges.discard((SB, RTR))
    g.add_edge([GC, SB], RTR)
    g.waiting_edges.discard(_find_wait(g, [RTR, TXT], PTP))
    g.add_edge([GC, GS, GV, SB, RTR, TXT], PTP)
    if ai:
        g.edges.discard((RTR, GAT))
        g.add_edge([GC, RTR], GAT)


def make_mut_pre_stage5(web):
    """B2:把"第5期阶段5 的检索并行化"还原成 6679e54 的挂法:
    GC→STR 普通边 + STR 条件边(→SW | →GS)+ SW→GS 普通边;去掉 GC→SW 与 [STR,SW]→GS 屏障。"""

    def mut(g, ai):
        g.edges.discard((GC, SW))
        g.waiting_edges.discard(_find_wait(g, [STR, SW], GS))

        def _storyline_route_after_template_rec(state):
            return SW if web else GS  # 桩环境下"开关开 且 检索词非空"= web

        g.add_conditional_edges(STR, _storyline_route_after_template_rec, {SW: SW, GS: GS})
        g.add_edge(SW, GS)

    return mut


def mut_ptp_independent(g, ai):
    """C1:[RTR,TXT]→PTP 拆成两条独立单源边(OR 语义)。"""
    g.waiting_edges.discard(_find_wait(g, [RTR, TXT], PTP))
    g.add_edge(RTR, PTP)
    g.add_edge(TXT, PTP)


def mut_ptat_independent(g, ai):
    """C2:[PTP,GAT]→PTAT 拆成两条独立单源边(仅 AI 开时存在)。"""
    g.waiting_edges.discard(_find_wait(g, [PTP, GAT], PTAT))
    g.add_edge(PTP, PTAT)
    g.add_edge(GAT, PTAT)


def _delay_gat(g):
    """C2b 辅助:在 RTR→GAT 之间插入 1 个直通节点,模拟"GAT 路径变长"。"""
    g.edges.discard((RTR, GAT))
    g.add_node("gat_delay_probe", lambda state: {})
    g.add_edge(RTR, "gat_delay_probe")
    g.add_edge("gat_delay_probe", GAT)


def mut_gat_delayed_barrier(g, ai):
    _delay_gat(g)


def mut_gat_delayed_independent(g, ai):
    _delay_gat(g)
    mut_ptat_independent(g, ai)


def _delay_sw(g):
    """C3/C4 辅助:在 GC→SW 之间插入 1 个直通节点,模拟"检索路径变长"(如日后加了前置步骤)。"""
    g.edges.discard((GC, SW))
    g.add_node("sw_delay_probe", lambda state: {})
    g.add_edge(GC, "sw_delay_probe")
    g.add_edge("sw_delay_probe", SW)


def mut_gs_independent(g, ai):
    """C3:[STR,SW]→GS 拆成两条独立单源边(OR 语义)。"""
    g.waiting_edges.discard(_find_wait(g, [STR, SW], GS))
    g.add_edge(STR, GS)
    g.add_edge(SW, GS)


def mut_gs_drop_sw(g, ai):
    """C4:[STR,SW]→GS 丢掉 SW(只保留 STR→GS)。"""
    g.waiting_edges.discard(_find_wait(g, [STR, SW], GS))
    g.add_edge(STR, GS)


def mut_sw_delayed_barrier(g, ai):
    _delay_sw(g)


def mut_sw_delayed_independent(g, ai):
    _delay_sw(g)
    mut_gs_independent(g, ai)


def mut_sw_delayed_drop(g, ai):
    _delay_sw(g)
    mut_gs_drop_sw(g, ai)


def make_mut_sw_conditional_in_barrier(trigger_rounds):
    """D:违反 D10 —— SW 改回"条件触发"(GC→SW|END,仅 trigger_rounds 轮触发),仍留在屏障 [STR,SW]→GS。"""

    def mut(g, ai):
        g.edges.discard((GC, SW))
        counter = {"n": 0}

        def route_sw(state):
            counter["n"] += 1
            return SW if counter["n"] in trigger_rounds else END

        g.add_conditional_edges(GC, route_sw, {SW: SW, END: END})

    return mut


def storyline_edge_sets(g):
    reg = lambda n: str(n).startswith("storyline_")  # noqa: E731
    plain = sorted(e for e in g.edges if reg(e[0]) or reg(e[1]))
    wait = sorted((sorted(s), t) for s, t in g.waiting_edges if reg(t) or any(reg(x) for x in s))
    cond = sorted(
        (src, sorted((getattr(spec, "ends", None) or {}).values()))
        for src, named in g.branches.items()
        if reg(src)
        for spec in named.values()
    )
    return plain, wait, cond


def build_plain_graph(ai, qc=True):
    saved = G.is_ai_transition_enabled
    saved_qc = C.ASSEMBLY_QC_GATE_ENABLED
    G.is_ai_transition_enabled = lambda: ai
    C.ASSEMBLY_QC_GATE_ENABLED = qc
    try:
        return G._build_state_graph()
    finally:
        G.is_ai_transition_enabled = saved
        C.ASSEMBLY_QC_GATE_ENABLED = saved_qc


def main() -> int:
    import importlib.metadata as md

    result: dict = {}
    result["env"] = {
        "python": sys.version.split()[0],
        "langgraph": md.version("langgraph"),
        "langgraph-checkpoint": md.version("langgraph-checkpoint"),
        "default_recursion_limit": __import__("langgraph._internal._config", fromlist=["x"]).DEFAULT_RECURSION_LIMIT,
    }

    # ---------------- A:真实拓扑场景矩阵 ----------------
    A = {}
    all_ok = True
    for web in (False, True):
        for ai in (True, False):
            for retries in (0, 1, 3):
                h, ev, nxt, err = run(web=web, ai=ai, retries=retries)
                s = summarize(h, ev, nxt, err)
                exp = expected_per_round(ai)
                rounds_ok = s["rounds"] == retries + 1
                counts_ok = all(
                    s["per_round"].get(r, {}).get(n, 0) == c
                    for r in range(1, retries + 2)
                    for n, c in exp.items()
                )
                extra = [
                    (r, n)
                    for r in range(1, retries + 2)
                    for n in s["per_round"].get(r, {})
                    if n not in exp and n in (GAT, PTAT)
                ]
                ends_ok = s["next"] == [STOP_NODE] and not s["error"]
                ok = rounds_ok and counts_ok and not extra and ends_ok and not s["stale_reads"]
                all_ok &= ok
                A[f"web{int(web)}_ai{int(ai)}_retry{retries}"] = {
                    "ok": ok,
                    "rounds": s["rounds"],
                    "max_step": s["max_step"],
                    "next": s["next"],
                    "error": s["error"],
                    "stale_reads": s["stale_reads"],
                    "per_round_first": s["per_round"].get(1),
                    "extra_unexpected": extra,
                    "signature": s["signature"],
                    "triggers": s["triggers"] if retries == 0 else None,
                }
    result["A_matrix"] = A
    result["A_all_ok"] = all_ok

    # ---------------- B:反向还原对照 ----------------
    recon = {}
    old_topo = json.loads(old_topo_path.read_text(encoding="utf-8")) if old_topo_path and old_topo_path.exists() else None
    if old_topo:
        for ai in (True, False):
            g_re = build_plain_graph(ai, qc=False)  # QC 关:JOIN 出口与旧基线同形
            make_mut_pre_stage5(True)(g_re, ai)
            mut_pre_reduction(g_re, ai)
            got_plain, got_wait, got_cond = storyline_edge_sets(g_re)
            oc = old_topo["combos"]["ai1_qc1" if ai else "ai0_qc1"]
            reg = lambda n: str(n).startswith("storyline_")  # noqa: E731
            want_plain = sorted(tuple(e) for e in oc["plain"] if reg(e[0]) or reg(e[1]))
            want_wait = sorted((sorted(s), t) for s, t in oc["waiting"] if reg(t) or any(reg(x) for x in s))
            want_cond = sorted((c["src"], sorted(c["ends"].values())) for c in oc["cond"] if reg(c["src"]))
            recon[f"ai{int(ai)}"] = {
                "plain_equal": got_plain == want_plain,
                "waiting_equal": got_wait == want_wait,
                "cond_equal": got_cond == want_cond,
            }
    result["B_reconstruction_matches_6679e54"] = recon

    B1, B2 = {}, {}
    b1_same = True
    for web in (False, True):
        for ai in (True, False):
            for retries in (0, 3):
                key = f"web{int(web)}_ai{int(ai)}_retry{retries}"
                h0, e0, n0, er0 = run(web=web, ai=ai, retries=retries)
                s0 = summarize(h0, e0, n0, er0)
                # B1
                h1, e1, n1, er1 = run(web=web, ai=ai, retries=retries, mutate=mut_pre_reduction)
                s1 = summarize(h1, e1, n1, er1)
                same = s0["signature"] == s1["signature"] and s0["per_round"] == s1["per_round"]
                ok1 = same and not s0["stale_reads"] and not s1["stale_reads"] and s0["next"] == s1["next"] == [STOP_NODE]
                b1_same &= ok1
                B1[key] = {
                    "same_signature_and_counts": same,
                    "stale_head": len(s0["stale_reads"]),
                    "stale_restored": len(s1["stale_reads"]),
                    "max_step_head": s0["max_step"],
                    "max_step_restored": s1["max_step"],
                }
                # B2
                h2, e2, n2, er2 = run(web=web, ai=ai, retries=retries, mutate=make_mut_pre_stage5(web))
                s2 = summarize(h2, e2, n2, er2)
                B2[key] = {
                    "max_step_head": s0["max_step"],
                    "max_step_pre_stage5": s2["max_step"],
                    "steps_saved_total": s2["max_step"] - s0["max_step"],
                    "per_round_head": s0["per_round"].get(1),
                    "per_round_pre_stage5": s2["per_round"].get(1),
                    "stale_head": len(s0["stale_reads"]),
                    "stale_pre_stage5": len(s2["stale_reads"]),
                    "next_pre_stage5": s2["next"],
                    "signature_head_first_steps": s0["signature"][:8],
                    "signature_pre_stage5_first_steps": s2["signature"][:8],
                }
    result["B1_pre_reduction_vs_head"] = B1
    result["B1_all_equivalent"] = b1_same
    result["B2_pre_stage5_vs_head"] = B2

    # ---------------- C:负对照 ----------------
    C_res = {}
    for retries in (0, 1):
        h, ev, nxt, err = run(web=False, ai=True, retries=retries, mutate=mut_ptp_independent)
        s = summarize(h, ev, nxt, err)
        C_res[f"C1_ptp_independent_retry{retries}"] = {
            "per_round": s["per_round"],
            "ptp_runs(step,triggers)": [(st, tuple(t)) for st, n, t in ev if n == PTP],
            "bad_reads": s["stale_reads"],
            "next": s["next"],
            "error": s["error"],
        }
    h0, e0, n0, er0 = run(web=False, ai=True, retries=1)
    s0 = summarize(h0, e0, n0, er0)
    h, ev, nxt, err = run(web=False, ai=True, retries=1, mutate=mut_ptat_independent)
    s = summarize(h, ev, nxt, err)
    C_res["C2_ptat_independent_retry1"] = {
        "same_signature_as_head": s["signature"] == s0["signature"],
        "per_round": s["per_round"],
        "bad_reads": s["stale_reads"],
        "next": s["next"],
        "ptat_runs(step,triggers)": [(st, tuple(t)) for st, n, t in ev if n == PTAT],
    }
    for tag, mut in (("barrier", mut_gat_delayed_barrier), ("independent", mut_gat_delayed_independent)):
        h, ev, nxt, err = run(web=False, ai=True, retries=1, mutate=mut)
        s = summarize(h, ev, nxt, err)
        C_res[f"C2b_gat_delayed_{tag}_retry1"] = {
            "per_round": {r: {k: v for k, v in c.items() if k != "gat_delay_probe"} for r, c in s["per_round"].items()},
            "ptat_runs(step,triggers)": [(st, tuple(t)) for st, n, t in ev if n == PTAT],
            "bad_reads": s["stale_reads"],
            "next": s["next"],
            "error": s["error"],
        }
    # C3 / C4:新屏障 [STR,SW]→GS(检索产物"有":web=True)
    for tag, mut in (
        ("C3_gs_independent", mut_gs_independent),
        ("C3b_sw_delayed_barrier(对照)", mut_sw_delayed_barrier),
        ("C3c_sw_delayed_independent", mut_sw_delayed_independent),
        ("C4_gs_drop_sw", mut_gs_drop_sw),
        ("C4b_sw_delayed_drop_sw", mut_sw_delayed_drop),
    ):
        h, ev, nxt, err = run(web=True, ai=False, retries=1, mutate=mut)
        s = summarize(h, ev, nxt, err)
        C_res[f"{tag}_retry1"] = {
            "same_signature_as_head": s["signature"] == summarize(*run(web=True, ai=False, retries=1))["signature"],
            "per_round": {r: {k: v for k, v in c.items() if k != "sw_delay_probe"} for r, c in s["per_round"].items()},
            "gs_runs(step,triggers)": [(st, tuple(t)) for st, n, t in ev if n == GS],
            "bad_reads": s["stale_reads"],
            "next": s["next"],
            "error": s["error"],
        }
    result["C_negative_controls"] = C_res

    # ---------------- D:D10(条件触发节点放进屏障)----------------
    h, ev, nxt, err = run(web=True, ai=True, retries=1, mutate=make_mut_sw_conditional_in_barrier((1,)))
    s = summarize(h, ev, nxt, err)
    result["D_d10_conditional_sw_in_barrier"] = {
        "rounds": s["rounds"],
        "per_round": s["per_round"],
        "next": s["next"],
        "error": s["error"],
        "reached_qa_times": sum(1 for _st, n, _t in ev if n == QAG),
        "gs_runs": sum(1 for _st, n, _t in ev if n == GS),
    }
    h, ev, nxt, err = run(web=True, ai=True, retries=1, sw_skip_rounds={2})
    s = summarize(h, ev, nxt, err)
    result["D_control_real_graph_sw_soft_skip_round2"] = {
        "rounds": s["rounds"],
        "per_round": s["per_round"],
        "next": s["next"],
        "error": s["error"],
        "reached_qa_times": sum(1 for _st, n, _t in ev if n == QAG),
        "gs_runs": sum(1 for _st, n, _t in ev if n == GS),
        "bad_reads": s["stale_reads"],
    }

    # ---------------- E:storyline_join × timeline 文件状态 ----------------
    E = []
    jd = tmp_dir / "join_cases"
    jd.mkdir(exist_ok=True)
    valid_tl = {
        "schema_version": "1.0",
        "job_id": "j",
        "created_at_ms": 0,
        "source_media": [
            {"media_id": "m1", "file_uri": "file:///a.mp4", "duration_ms": 10000, "media_type": "video"}
        ],
        "clips": [
            {
                "clip_id": "c1",
                "source_media_id": "m1",
                "source_in_ms": 0,
                "source_out_ms": 5000,
                "timeline_in_ms": 0,
                "timeline_out_ms": 5000,
                "semantic_tags": [],
            }
        ],
        "audio": {"bgm_ref": None, "voiceover": None},
        "subtitles": {"zh": None, "en": None},
        "options": {"enable_ai_transition": False, "enable_voiceover": True, "max_duration_ms": 35000},
    }
    bad_tl = dict(valid_tl, clips=[dict(valid_tl["clips"][0], source_out_ms=999999)])
    cases = [
        ("无 storyline_timeline_plan 键", None),
        ("路径指向不存在的文件", "MISSING"),
        ("文件内容不是合法 JSON", "not json"),
        ("文件内容 null", "null"),
        ("文件内容 {} (合法 JSON·空对象)", "{}"),
        ("文件内容 [] (合法 JSON·空数组)", "[]"),
        ("合法但违反契约(source_out 越界)", json.dumps(bad_tl)),
        ("完全合法的 timeline", json.dumps(valid_tl)),
    ]
    for i, (label, content) in enumerate(cases):
        st = {"session_id": "s1", "video_input_path": "/tmp/x.mp4",
              "storyline_outputs_root": str(jd), "storyline_session_id": "s1"}
        if content == "MISSING":
            st["storyline_timeline_plan"] = str(jd / "nope.json")
        elif content is not None:
            p = jd / f"case{i}.json"
            p.write_text(content, encoding="utf-8")
            st["storyline_timeline_plan"] = str(p)
        delta = JN.storyline_join_node(st)
        merged = dict(st)
        merged.update({k: v for k, v in delta.items() if k != "status_log"})
        _saved_qc = C.ASSEMBLY_QC_GATE_ENABLED
        try:
            C.ASSEMBLY_QC_GATE_ENABLED = True
            route_on = G._route_after_storyline_join(merged)
            C.ASSEMBLY_QC_GATE_ENABLED = False
            route_off = G._route_after_storyline_join(merged)
        finally:
            C.ASSEMBLY_QC_GATE_ENABLED = _saved_qc
        E.append(
            {
                "case": label,
                "plan_truthy": bool(delta.get("storyline_plan")),
                "route": route_on,
                "route_qc_off": route_off,
                "error_log_tail": (delta.get("error_log") or [""])[-1][:90] if delta.get("error_log") else None,
            }
        )
    result["E_join_route_cases"] = E

    # ---------------- F:GV 失败不覆盖旧 artifact(真实节点)----------------
    import nodes.storyline.node_generate_voiceover as GVN
    from state import WorkflowState

    fd = tmp_dir / "gv_case"
    fd.mkdir(exist_ok=True)
    script = fd / "script.json"
    script.write_text(json.dumps({"group_scripts": [{"narration": "你好"}]}), encoding="utf-8")
    calls = {"n": 0}

    def fake_generate_voiceover(*, group_scripts, output_dir, user_request):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("TTS 服务不可用(模拟第 2 轮失败)")
        return {"voiceover": [{"duration": 3000, "marker": "ROUND1"}], "stub_count": 0, "errors": []}

    orig = (GVN.generate_voiceover, GVN.install_tts_client_from_env)
    GVN.generate_voiceover = fake_generate_voiceover
    GVN.install_tts_client_from_env = lambda: None
    try:
        gg = StateGraph(WorkflowState)
        gg.add_node("gv", GVN.storyline_generate_voiceover_node)
        gg.add_edge(START, "gv")
        gg.add_edge("gv", END)
        cg = gg.compile(checkpointer=InMemorySaver())
        cfg = {"configurable": {"thread_id": "gv1"}}
        base = {"session_id": "s", "storyline_outputs_root": str(fd), "storyline_script_artifact": str(script)}
        cg.invoke(base, cfg)
        v1 = cg.get_state(cfg).values.get(GV_KEY)
        marker1 = json.loads(Path(v1).read_text(encoding="utf-8"))["voiceover"][0].get("marker")
        cg.invoke({"session_id": "s"}, cfg)  # 第 2 轮:TTS 失败
        v2 = cg.get_state(cfg).values.get(GV_KEY)
        marker2 = json.loads(Path(v2).read_text(encoding="utf-8"))["voiceover"][0].get("marker") if v2 else None
        errs = cg.get_state(cfg).values.get("error_log")
        result["F_gv_failure_stale"] = {
            "round1_artifact": v1,
            "round2_artifact_after_failure": v2,
            "same_path": v1 == v2,
            "file_content_marker_after_round2_failure": marker2,
            "round1_marker": marker1,
            "error_log_tail": (errs or [""])[-1][:100] if errs else None,
        }
    finally:
        GVN.generate_voiceover, GVN.install_tts_client_from_env = orig

    # ---------------- F2:图级 GV 失败(keep=现状 / clear=写 "")----------------
    F2 = {}
    for mode in ("keep", "clear"):
        h, ev, nxt, err = run(web=False, ai=False, retries=1, fail_gv_round=2, gv_mode=mode)
        s = summarize(h, ev, nxt, err)
        F2[mode] = {
            "next": s["next"],
            "bad_reads(陈旧/缺失)": s["stale_reads"],
            "SB_reads_round2": [stamps for r, stamps in h.reads[SB] if r == 2],
            "PTP_reads_round2_voiceover": [stamps.get(GV_KEY) for r, stamps in h.reads[PTP] if r == 2],
        }
    result["F2_gv_failure_in_graph"] = F2

    # ---------------- G:SW 软跳过(真实节点两轮 + 图级 keep/clear)----------------
    import nodes.storyline.node_search_web_topic as NSW
    from nodes.storyline._common import _read_json_artifact

    gd = tmp_dir / "sw_case"
    gd.mkdir(exist_ok=True)
    qcalls = {"n": 0}
    ccalls = {"n": 0}

    def fake_query(_state):
        qcalls["n"] += 1
        return "话题A" if qcalls["n"] == 1 else ""  # 第 2 轮检索词消失(极端假设)

    def fake_cap(**kw):
        ccalls["n"] += 1
        return {"status": "ok", "search_web_topic": [{"title": "T1"}], "query_hash": "h", "error_code": None}

    orig_sw = (NSW.build_web_topic_query, NSW.cap_topic, C.STORYLINE_ENABLE_WEB_TOPIC)
    NSW.build_web_topic_query, NSW.cap_topic = fake_query, fake_cap
    C.STORYLINE_ENABLE_WEB_TOPIC = True
    try:
        gg = StateGraph(WorkflowState)
        gg.add_node("sw", NSW.storyline_search_web_topic_node)
        gg.add_edge(START, "sw")
        gg.add_edge("sw", END)
        cg = gg.compile(checkpointer=InMemorySaver())
        cfg = {"configurable": {"thread_id": "sw1"}}
        cg.invoke({"session_id": "s", "storyline_outputs_root": str(gd)}, cfg)
        a1 = cg.get_state(cfg).values.get(WEB_KEY)
        cg.invoke({"session_id": "s"}, cfg)  # 第 2 轮:检索词为空 → 软跳过
        v2 = dict(cg.get_state(cfg).values)
        a2 = v2.get(WEB_KEY)
        result["G_sw_skip_stale_real_node"] = {
            "round1_artifact": a1,
            "round2_artifact_after_skip": a2,
            "same_path": a1 == a2,
            "network_calls": ccalls["n"],
            "round2_status_tail": (v2.get("status_log") or [])[-2:],
            "downstream_GS_reads_doc(第2轮)": _read_json_artifact(v2, WEB_KEY) is not None,
        }
    finally:
        NSW.build_web_topic_query, NSW.cap_topic, C.STORYLINE_ENABLE_WEB_TOPIC = orig_sw

    G2 = {}
    for mode in ("keep", "clear"):
        h, ev, nxt, err = run(web=True, ai=False, retries=1, sw_skip_rounds={2}, sw_mode=mode)
        s = summarize(h, ev, nxt, err)
        G2[mode] = {
            "next": s["next"],
            "bad_reads(陈旧/缺失)": s["stale_reads"],
            "GS_reads_round2_web": [stamps.get(WEB_KEY) for r, stamps in h.reads[GS] if r == 2],
        }
    result["G2_sw_skip_in_graph"] = G2


    # ---------------- H:建图时与运行时的 QC 开关不一致(路由函数运行时读开关,路径映射建图时按开关生成)----------------
    H = {}
    for label, qb, qr in (
        ("一致·QC开", True, None),
        ("一致·QC关", False, None),
        ("建图QC关→运行时改成开", False, True),
        ("建图QC开→运行时改成关", True, False),
    ):
        h, ev, nxt, err = run(web=False, ai=True, retries=0, qc_build=qb, qc_run=qr)
        H[label] = {"next": list(nxt), "error": err}
    result["H_qc_flag_mismatch"] = H

    # ---------------- I:QC 关的子矩阵(Storyline 本体与 QC 开关无关:步数、触发次数、新鲜度应与 QC 开完全一致)----------------
    I = {}
    for ai in (True, False):
        for retries in (0, 3):
            ha, eva, na, era = run(web=True, ai=ai, retries=retries, qc_build=True)
            hb, evb, nb, erb = run(web=True, ai=ai, retries=retries, qc_build=False)
            sa, sb = summarize(ha, eva, na, era), summarize(hb, evb, nb, erb)
            I[f"ai{int(ai)}_retry{retries}"] = {
                "same_signature": sa["signature"] == sb["signature"],
                "same_per_round": sa["per_round"] == sb["per_round"],
                "max_step_qc_on": sa["max_step"], "max_step_qc_off": sb["max_step"],
                "next_qc_on": sa["next"], "next_qc_off": sb["next"],
                "stale_qc_on": len(sa["stale_reads"]), "stale_qc_off": len(sb["stale_reads"]),
                "error_qc_off": sb["error"],
            }
    result["I_qc_off_submatrix"] = I

    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ---------------- 打印摘要 ----------------
    print("环境:", result["env"])
    print("\nA 场景矩阵 全部通过:", result["A_all_ok"])
    for k, v in A.items():
        print(f"  {k:<22} ok={v['ok']}  rounds={v['rounds']}  max_step={v['max_step']}  next={v['next']}  bad_reads={len(v['stale_reads'])}")
    print("\nB 全还原(B1+B2)后的 Storyline 边集 == 6679e54 旧基线:", recon or "(未提供旧拓扑 JSON,跳过)")
    print("B1 反向还原阶段1(4 处约简)与 HEAD 逐超步一致:", result["B1_all_equivalent"])
    for k, v in B1.items():
        print(f"  {k:<22} {v}")
    print("B2 反向还原阶段5(检索并行化)vs HEAD:")
    for k, v in B2.items():
        print(
            f"  {k:<22} max_step HEAD={v['max_step_head']} 还原={v['max_step_pre_stage5']} "
            f"(HEAD 少 {v['steps_saved_total']} 个超步) 陈旧读 HEAD={v['stale_head']} 还原={v['stale_pre_stage5']} next={v['next_pre_stage5']}"
        )
    print("\nC 负对照")
    for k, v in C_res.items():
        extra = {kk: vv for kk, vv in v.items() if kk not in ("bad_reads",)}
        print(f"  {k}: 陈旧/缺失读取={len(v['bad_reads'])}  详情={extra}")
        for b in v["bad_reads"][:3]:
            print("      ·", b)
    print("\nD D10 复现:", result["D_d10_conditional_sw_in_barrier"])
    print("D 对照(真实图 SW 第2轮软跳过):", result["D_control_real_graph_sw_soft_skip_round2"])
    print("\nE join 路由")
    for r in E:
        print(f"  {r['case']:<34} plan非空={r['plan_truthy']!s:<5} QC开 → {r['route']:<30} QC关 → {r['route_qc_off']}")
    print("\nF GV 失败陈旧(真实节点):", result["F_gv_failure_stale"])
    print("F2 图级:", json.dumps(F2, ensure_ascii=False)[:900])
    print("\nH QC 开关不一致:")
    for k, v in H.items():
        print(f"  {k:<24} next={v['next']}  error={v['error']}")
    print("I QC 关子矩阵:")
    for k, v in I.items():
        print(f"  {k:<14} {v}")
    print("\nG SW 软跳过(真实节点):", result["G_sw_skip_stale_real_node"])
    print("G2 图级:", json.dumps(G2, ensure_ascii=False)[:900])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
