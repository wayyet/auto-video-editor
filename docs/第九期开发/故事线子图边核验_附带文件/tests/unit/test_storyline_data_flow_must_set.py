"""Storyline 数据流守卫:每个节点读取的上游产物,其生产者必须在该节点的「必经集合」里;
屏障的每个来源必须「每轮必跑」。

背景:第5期依赖约简(a9c0288 / 156daad)把 fan-in 屏障约简成更少的来源、并把检索节点并行化。
约简成立的前提是"被删来源必在保留来源的祖先链上"。本测试把这个前提固化成可执行的结构守卫:
以后任何人再改 add_edge / 屏障,只要某节点读取的上游产物不再被"必经保证",或屏障里混进了
"有时跑有时不跑"的节点,本测试立刻变红 —— 不依赖节点跑出来的次数,也不依赖"碰巧先完成"的超步时序。

名词:
- 必经集合 must(n):去掉 QA→GC 回边后,节点 n 启动前"本轮必然已完成"的节点集合。
  普通边 / 条件出口 = 一个独立触发(OR),来源任一完成即触发;
  列表屏障 = 一个汇合触发(AND),所有来源完成才触发。
  must(n) = 各触发集合的交集;汇合触发的集合 = 各来源的 must ∪ {来源} 的并集。
- 每轮必跑集合:从重试回路起点 GC 出发,只沿「普通边」与「来源全部必到的屏障」能走到的节点。
  条件出口不算 —— 路由可能选别的出口,那条路径上的节点"有时跑有时不跑"(D10)。
- 读取表(UPSTREAM_READS):节点代码里读取的上游产物(键 → 生产者)。其中 storyline_web_topic_artifact
  是软读取(缺失按"无检索参考"降级),但仍要求"先于 GS 完成",否则会读到上一轮的旧文档。
"""
from __future__ import annotations

import pytest
from langgraph.graph import END

import config
import graph as graph_module

START = "__start__"
_LM = "storyline_load_media"
_SS = "storyline_split_shots"
_UC = "storyline_understand_clips"
_FC = "storyline_filter_clips"
_GC = "storyline_group_clips"
_STR = "storyline_script_template_recommendation"
_SW = "storyline_search_web_topic"
_GS = "storyline_generate_script"
_GV = "storyline_generate_voiceover"
_TXT = "storyline_recommend_text"
_SB = "storyline_select_bgm"
_RTR = "storyline_recommend_transition"
_GAT = "storyline_generate_ai_transition"
_PTP = "storyline_plan_timeline_pro"
_PTAT = "storyline_plan_timeline_ai_transition"
_QA = "storyline_qa_gate"
_JOIN = "storyline_join"

#: 消费者 → {它读取的 state 键: [生产该键的节点]}。来源:各节点源码对 state.get(...) /
#: _read_json_artifact(state, ...) / _read_groups / _read_media 的读取(2026-11-18,基线 e29dd26 逐节点核对)。
#: "最终时间线"的生产者随 AI 开关切换,用占位 "FINAL_TIMELINE" 表示。
UPSTREAM_READS: dict[str, dict[str, list[str]]] = {
    _SS: {"storyline_media_artifact": [_LM]},
    _UC: {"storyline_media_artifact": [_LM], "storyline_shots_artifact": [_SS]},
    _FC: {"storyline_understanding_artifact": [_UC]},
    _GC: {"storyline_filtered_clips": [_FC]},
    _STR: {"storyline_understanding_artifact": [_UC], "storyline_groups_artifact": [_GC]},
    # build_web_topic_query 读 overall(UC)与 groups[].theme(GC)
    _SW: {"storyline_understanding_artifact": [_UC], "storyline_groups_artifact": [_GC]},
    _GS: {
        "storyline_groups_artifact": [_GC],
        "storyline_understanding_artifact": [_UC],
        "storyline_script_template_artifact": [_STR],
        "storyline_web_topic_artifact": [_SW],  # 软读取,但必须先于 GS 完成
    },
    _GV: {"storyline_script_artifact": [_GS]},
    _TXT: {"storyline_groups_artifact": [_GC], "storyline_script_artifact": [_GS]},
    _SB: {
        "storyline_groups_artifact": [_GC],
        "storyline_script_artifact": [_GS],
        "storyline_voiceover_artifact": [_GV],
    },
    _RTR: {"storyline_groups_artifact": [_GC], "storyline_bgm_selection": [_SB]},
    _GAT: {
        "storyline_groups_artifact": [_GC],
        "storyline_media_artifact": [_LM],
        "storyline_transition_plan": [_RTR],
    },
    _PTP: {
        "storyline_groups_artifact": [_GC],
        "storyline_media_artifact": [_LM],
        "storyline_voiceover_artifact": [_GV],
        "storyline_script_artifact": [_GS],
        "storyline_bgm_selection": [_SB],
        "storyline_transition_plan": [_RTR],
        "storyline_text_style_plan": [_TXT],
    },
    _PTAT: {"storyline_timeline_plan": [_PTP], "storyline_ai_transition_artifact": [_GAT]},
    _QA: {"storyline_timeline_plan": ["FINAL_TIMELINE"]},
    _JOIN: {"storyline_timeline_plan": ["FINAL_TIMELINE"]},
}


@pytest.fixture(autouse=True)
def _deterministic_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """与 test_storyline_topology_baseline.py 同口径:建图开关取确定值。"""
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)


def _build(monkeypatch: pytest.MonkeyPatch, ai_on: bool):
    monkeypatch.setattr(config, "STORYLINE_ENABLE_AI_TRANSITION", ai_on)
    monkeypatch.setattr(
        "storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION", ai_on
    )
    return graph_module._build_state_graph()


def must_sets(g) -> dict[str, set[str]]:
    """每个 storyline 节点的必经集合(只沿入边向上游回溯,所以只会遍历 storyline 链)。"""
    triggers: dict[str, list[tuple[str, ...]]] = {}
    for s, t in g.edges:  # 普通边:任一来源完成即触发(OR)
        triggers.setdefault(t, []).append((s,))
    for srcs, t in g.waiting_edges:  # 列表屏障:全部来源完成才触发(AND)
        triggers.setdefault(t, []).append(tuple(srcs))
    for src, named in g.branches.items():  # 条件出口:每个出口算一条独立触发
        for spec in named.values():
            for dst in (getattr(spec, "ends", None) or {}).values():
                triggers.setdefault(dst, []).append((src,))
    # 去掉唯一的回边 QA→GC(重试 = 新的一轮,不算本轮前置)
    triggers[_GC] = [tr for tr in triggers.get(_GC, []) if tr != (_QA,)]

    memo: dict[str, set[str]] = {START: set()}

    def solve(n: str, stack: tuple[str, ...] = ()) -> set[str]:
        if n in memo:
            return memo[n]
        assert n not in stack, f"去掉 QA→GC 回边后仍有环:{' → '.join(stack + (n,))}"
        per_trigger: list[set[str]] = []
        for srcs in triggers.get(n, []):
            u: set[str] = set()
            for s in srcs:
                u |= solve(s, stack + (n,)) | {s}
            per_trigger.append(u)
        memo[n] = set.intersection(*per_trigger) if per_trigger else set()
        return memo[n]

    return {n: solve(n) for n in list(g.nodes) if n.startswith("storyline_")}


def check_upstream_reads(g, *, ai_on: bool) -> list[str]:
    """返回违规描述列表(空 = 全部满足)。供本文件断言,也供"过度约简"负对照复用。"""
    must = must_sets(g)
    final_timeline = _PTAT if ai_on else _PTP
    problems: list[str] = []
    for consumer, reads in UPSTREAM_READS.items():
        if not ai_on and consumer in (_GAT, _PTAT):
            continue  # AI 关:两节点注册但不接线(见 topology baseline 的 ai_off 用例)
        for key, producers in reads.items():
            for p in producers:
                p = final_timeline if p == "FINAL_TIMELINE" else p
                if p not in must[consumer]:
                    problems.append(
                        f"{consumer} 读取 {key},但生产者 {p} 不在它的必经集合内 —— "
                        "该产物可能尚未写出或停留在上一轮"
                    )
    return problems


def unconditional_loop_nodes(g) -> set[str]:
    """每轮必跑集合:从 GC 出发,只沿普通边与"来源全部必到"的屏障前进;条件出口不算。"""
    reached = {_GC}
    grew = True
    while grew:
        grew = False
        for s, t in g.edges:
            if s in reached and t not in reached:
                reached.add(t)
                grew = True
        for srcs, t in g.waiting_edges:
            if t not in reached and all(x in reached for x in srcs):
                reached.add(t)
                grew = True
    return reached


def check_barrier_sources(g) -> list[str]:
    """D10 守卫:Storyline 屏障的每个来源都必须每轮必跑(在重试回路体内,且不经条件出口)。

    条件触发的节点(路由可能不放行)、重试点上游的节点(SS 等,重试轮不会重跑)进屏障,
    重试轮屏障凑不齐 → 图静默 END(runner 按 next==() 判"完成",没有草稿也没有失败)。
    """
    every_round = unconditional_loop_nodes(g)
    problems: list[str] = []
    for srcs, target in g.waiting_edges:
        if not target.startswith("storyline_"):
            continue
        for s in srcs:
            if s not in every_round:
                problems.append(
                    f"屏障 {sorted(srcs)}→{target} 的来源 {s} 不是每轮必跑"
                    "(重试点上游节点,或只经条件出口触发)"
                )
    return problems


@pytest.mark.parametrize("ai_on", [True, False])
def test_every_upstream_read_has_its_producer_in_must_set(
    monkeypatch: pytest.MonkeyPatch, ai_on: bool
) -> None:
    """第5期依赖约简的安全前提:被约简的边不能让任何节点读到"没保证先完成"的产物。"""
    g = _build(monkeypatch, ai_on)
    problems = check_upstream_reads(g, ai_on=ai_on)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("ai_on", [True, False])
def test_barrier_sources_run_every_round(
    monkeypatch: pytest.MonkeyPatch, ai_on: bool
) -> None:
    """条件触发的节点、重试点上游的节点进屏障 → 重试轮屏障凑不齐 → 图静默 END。"""
    problems = check_barrier_sources(_build(monkeypatch, ai_on))
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# 守卫自检(负对照):故意把图改坏,守卫必须变红 —— 防止守卫自己悄悄失效
# ---------------------------------------------------------------------------
def _find_wait(g, srcs, dst):
    for s, t in list(g.waiting_edges):
        if t == dst and set(s) == set(srcs):
            return (s, t)
    raise AssertionError(f"未找到屏障 {srcs}->{dst}")


def _m_ptp_drop_txt(g):
    g.waiting_edges.discard(_find_wait(g, [_RTR, _TXT], _PTP))
    g.add_edge(_RTR, _PTP)


def _m_sb_from_gs(g):  # 20261113 文档的"任务A":精简为 GS→SB(丢掉 GV)
    g.edges.discard((_GV, _SB))
    g.add_edge(_GS, _SB)


def _m_rtr_from_gc(g):
    g.edges.discard((_SB, _RTR))
    g.add_edge(_GC, _RTR)


def _m_gat_from_gc(g):
    g.edges.discard((_RTR, _GAT))
    g.add_edge(_GC, _GAT)


def _m_ptat_only_ptp(g):
    g.waiting_edges.discard(_find_wait(g, [_PTP, _GAT], _PTAT))
    g.add_edge(_PTP, _PTAT)


def _m_gs_drop_sw(g):  # 阶段5 屏障丢掉检索来源:GS 读检索产物不再有结构保证
    g.waiting_edges.discard(_find_wait(g, [_STR, _SW], _GS))
    g.add_edge(_STR, _GS)


def _m_gs_independent(g):  # 阶段5 屏障拆成两条独立边(OR 语义,不再"等齐")
    g.waiting_edges.discard(_find_wait(g, [_STR, _SW], _GS))
    g.add_edge(_STR, _GS)
    g.add_edge(_SW, _GS)


def _m_ptp_independent(g):
    g.waiting_edges.discard(_find_wait(g, [_RTR, _TXT], _PTP))
    g.add_edge(_RTR, _PTP)
    g.add_edge(_TXT, _PTP)


def _m_sw_conditional_in_barrier(g):  # D10:检索节点改回条件触发,却仍留在 [STR,SW]→GS
    g.edges.discard((_GC, _SW))
    g.add_conditional_edges(_GC, lambda state: _SW, {_SW: _SW, END: END})


def _m_ss_in_barrier(g):  # 历史 D10 事故:重试点上游节点(SS)进屏障
    g.waiting_edges.discard(_find_wait(g, [_RTR, _TXT], _PTP))
    g.add_edge([_RTR, _TXT, _SS], _PTP)


_READ_MUTANTS = [
    ("PTP 只等 RTR(丢掉 TXT)", _m_ptp_drop_txt),
    ("SB 改成 GS→SB(丢掉 GV)", _m_sb_from_gs),
    ("RTR 改成 GC→RTR(丢掉 SB)", _m_rtr_from_gc),
    ("GAT 改成 GC→GAT(丢掉 RTR)", _m_gat_from_gc),
    ("PTAT 只等 PTP(丢掉 GAT)", _m_ptat_only_ptp),
    ("GS 屏障丢掉 SW", _m_gs_drop_sw),
    ("GS 屏障拆成两条独立边", _m_gs_independent),
    ("PTP 屏障拆成两条独立边", _m_ptp_independent),
]
_BARRIER_MUTANTS = [
    ("SW 改回条件触发但仍在屏障(D10)", _m_sw_conditional_in_barrier),
    ("SS(重试点上游)进 PTP 屏障", _m_ss_in_barrier),
]


@pytest.mark.parametrize("name,mutate", _READ_MUTANTS, ids=[m[0] for m in _READ_MUTANTS])
def test_guard_catches_over_reduction(monkeypatch: pytest.MonkeyPatch, name, mutate) -> None:
    g = _build(monkeypatch, True)
    mutate(g)
    assert check_upstream_reads(g, ai_on=True), f"守卫没有抓到:{name}"


@pytest.mark.parametrize(
    "name,mutate", _BARRIER_MUTANTS, ids=[m[0] for m in _BARRIER_MUTANTS]
)
def test_guard_catches_non_every_round_barrier_source(
    monkeypatch: pytest.MonkeyPatch, name, mutate
) -> None:
    g = _build(monkeypatch, True)
    mutate(g)
    assert check_barrier_sources(g), f"守卫没有抓到:{name}"
