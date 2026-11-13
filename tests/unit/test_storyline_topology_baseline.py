"""Storyline 拓扑基线冻结(2026-11-14 依赖约简阶段1 重录)—— 单元级全量快照,不执行图。

依据《auto-video-editor-langgraph-dependency-audit-and-plan》(第5期)§4/§10 阶段1:
四组 fan-in 屏障传递性约简 —— ``[GS, GV]→SB`` 缩为 ``GV→SB`` 单源边(GV 已等
GS)、``[GC, SB]→RTR`` 缩为 ``SB→RTR`` 单源边(SB 祖先链含 GC)、PTP 六源缩为
``[RTR, TXT]`` 两源(RTR 链覆盖其余四源)、GAT ``[GC, RTR]`` 缩为 ``RTR→GAT``
单源边。被删的源都在保留源的祖先链上,等待属重复约束;单元素列表屏障会被
LangGraph 折叠成普通边,故约简后的 SB/RTR/GAT 以普通边形式出现在
``_STORYLINE_NORMAL_EDGES_BASE``。PTAT ``[PTP, GAT]`` 双源与 QA 单源**保留不动**
(两份输入来自独立分支,不能约简)。

用户决策记录:本阶段推翻 2026-11-13 边审计「任务B 保留 [GS, GV]→SB 双源屏障」
的结案(当时计划默认的任务A 是"精简为 GS→SB";本次约简保留的是 GV 源 ——
配音时长消费不丢,删的是被 GV 传递保证的 GS 源,方向与旧任务A 相反)。

图真源 = ``graph._build_state_graph()`` 的 builder 边(不 compile),与
``test_storyline_edge_governance_topology.py`` / wiring 集成测试同口径:

- ``builder.edges``:普通边(source, target)二元组集合;
- ``builder.waiting_edges``:列表 fan-in 屏障((源元组, 目标));
- ``builder.branches[src]``:条件路由({分支名: Branch},``Branch.ends`` 为
  {标签: 目的地})。

快照范围 = Storyline 逻辑子流程(storyline_* 节点 + START 入边 + 条件出口);
外层草稿流水线(node_09 汇合屏障等)不在本快照内,由 wiring 集成测试锁定。
"""
from __future__ import annotations

import pytest

import config
import graph as graph_module

# ---------------------------------------------------------------------------
# 17 个 storyline_* 节点(2026-11-13 审计 §2.1;render_video 已随 2026-11-12
# 边治理阶段5 整体删除,search_media 已随 2026-11-10 整体删除)
# ---------------------------------------------------------------------------
_LM = "storyline_load_media"
_WT = "storyline_search_web_topic"
_SS = "storyline_split_shots"
_GAT = "storyline_generate_ai_transition"
_UC = "storyline_understand_clips"
_FC = "storyline_filter_clips"
_GC = "storyline_group_clips"
_GS = "storyline_generate_script"
_STR = "storyline_script_template_recommendation"
_GV = "storyline_generate_voiceover"
_SB = "storyline_select_bgm"
_RTR = "storyline_recommend_transition"
_TXT = "storyline_recommend_text"
_PTP = "storyline_plan_timeline_pro"
_PTAT = "storyline_plan_timeline_ai_transition"
_QA = "storyline_qa_gate"
_JOIN = "storyline_join"

STORYLINE_NODES: set[str] = {
    _LM, _WT, _SS, _GAT, _UC, _FC, _GC, _GS, _STR,
    _GV, _SB, _RTR, _TXT, _PTP, _PTAT, _QA, _JOIN,
}

#: 素材主链 + 模板前置 + 检索直连 + 文案 fan-out + 约简后的单源链
#: (与 AI 开关无关的普通边部分;2026-11-14 约简:原双源屏障 [GS,GV]→SB 与
#: [GC,SB]→RTR 的目标只剩唯一入边,单元素屏障被 LangGraph 折叠回普通边)
_STORYLINE_NORMAL_EDGES_BASE: set[tuple[str, str]] = {
    ("__start__", _LM),
    (_LM, _SS),
    (_SS, _UC),
    (_UC, _FC),
    (_FC, _GC),
    (_GC, _STR),
    (_WT, _GS),
    (_GS, _GV),
    (_GS, _TXT),
    (_GV, _SB),   # 2026-11-14 依赖约简阶段1:GV 唯一入边(GV 已等 GS)
    (_SB, _RTR),  # 2026-11-14 依赖约简阶段1:SB 唯一入边(SB 祖先链含 GC)
}

#: 与 AI 开关无关的 fan-in 屏障(仅剩 PTP 两源;历史:2026-11-12 曾有三道 ——
#: BGM 双源/普通转场双源/PTP 六源,2026-11-14 依赖约简阶段1 缩并)
_STORYLINE_BARRIERS_BASE: set[tuple[frozenset[str], str]] = {
    (frozenset({_RTR, _TXT}), _PTP),  # 2026-11-14 依赖约简阶段1:六源缩两源
}

#: 条件路由出口(三种,其余 storyline 节点不得有条件路由)
_STR_CONDITIONAL_EXITS = {_WT, _GS}
_QA_CONDITIONAL_EXITS = {_GC, _JOIN}
_JOIN_CONDITIONAL_EXITS = {"generate_draft", "__end__"}


def _build():
    """与 wiring 集成测试 / 边治理 topology 单测同口径:builder 不 compile。"""
    return graph_module._build_state_graph()


def _edge_pairs(g) -> set[tuple[str, str]]:
    return {(s, t) for s, t in g.edges}


def _barriers(g) -> set[tuple[frozenset[str], str]]:
    return {(frozenset(s), t) for s, t in getattr(g, "waiting_edges", set())}


def _conditional_targets(g, src: str) -> set[str]:
    targets: set[str] = set()
    for _name, spec in getattr(g, "branches", {}).get(src, {}).items():
        ends = getattr(spec, "ends", None)
        if ends:
            targets.update(ends.values())
    return targets


def _assert_storyline_section(g, *, ai_on: bool) -> None:
    """AI 单档下的 storyline 段全量快照断言(普通边 / 屏障 / 条件路由 / 边界)。"""
    edges = _edge_pairs(g)

    # 普通边:两端均为 storyline_*(QA 的最终时间线入边随开关切换;
    # AI 开时 GAT 还有一条 RTR 单源边)
    storyline_edges = {
        (s, t) for s, t in edges
        if s.startswith("storyline_") and t.startswith("storyline_")
    }
    final_timeline_edge = (_PTAT if ai_on else _PTP, _QA)
    expected_edges = (_STORYLINE_NORMAL_EDGES_BASE - {("__start__", _LM)}) | {final_timeline_edge}
    if ai_on:
        expected_edges |= {(_RTR, _GAT)}   # 2026-11-14 依赖约简阶段1:GAT 单源边
    assert storyline_edges == expected_edges, (
        f"storyline 普通边漂移(ai_on={ai_on}),实际 {sorted(storyline_edges)}"
    )

    # START 入边:唯一入口 LM,无其他 storyline 节点直挂 START
    start_edges = {(s, t) for s, t in edges if s == "__start__" and t.startswith("storyline_")}
    assert start_edges == {("__start__", _LM)}, f"START 直挂漂移:实际 {sorted(start_edges)}"

    # 边界:storyline 节点与外层节点之间不得出现普通边
    # (唯一交接口 = START→LM 与 JOIN 的条件出口,均已在上/下断言锁定)
    mixed = {
        (s, t)
        for s, t in edges
        if (s.startswith("storyline_") ^ t.startswith("storyline_")) and s != "__start__"
    }
    assert not mixed, f"storyline 段与外层出现普通边直连:{sorted(mixed)}"

    # 屏障:AI 开 = PTP 两源 + PTAT 双源;AI 关 = 仅 PTP 两源
    # (2026-11-14 依赖约简阶段1:BGM/转场/GAT 三道屏障缩为单源普通边)
    storyline_barriers = {bt for bt in _barriers(g) if bt[0] and bt[1].startswith("storyline_")}
    expected_barriers = set(_STORYLINE_BARRIERS_BASE)
    if ai_on:
        expected_barriers |= {
            (frozenset({_PTP, _GAT}), _PTAT),     # AI 开:基础时间线 + AI 转场结果
        }
    assert storyline_barriers == expected_barriers, (
        f"storyline 屏障形状漂移(ai_on={ai_on}),实际 {sorted(storyline_barriers)}"
    )

    # 条件路由:恰好三种源,出口集合逐一锁定
    branch_sources = {
        src for src in getattr(g, "branches", {}) if src.startswith("storyline_")
    }
    assert branch_sources == {_STR, _QA, _JOIN}, (
        f"storyline 条件路由源漂移:实际 {sorted(branch_sources)}"
    )
    assert _conditional_targets(g, _STR) == _STR_CONDITIONAL_EXITS
    assert _conditional_targets(g, _QA) == _QA_CONDITIONAL_EXITS
    assert _conditional_targets(g, _JOIN) == _JOIN_CONDITIONAL_EXITS


@pytest.fixture(autouse=True)
def _deterministic_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """与 wiring 集成测试 / 边治理 topology 单测同口径:建图开关取确定值。"""
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    monkeypatch.setattr(config, "STORYLINE_ENABLE_AI_TRANSITION", True)
    monkeypatch.setattr(
        "storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION", True
    )


def test_storyline_node_inventory_frozen() -> None:
    """阶段0 冻结:恰好 17 个 storyline_* 节点注册,与审计 §2.1 清单一致。"""
    g = _build()
    registered = {n for n in getattr(g, "nodes", {}) if n.startswith("storyline_")}
    assert registered == STORYLINE_NODES, (
        f"storyline 节点清单漂移:多 {sorted(registered - STORYLINE_NODES)},"
        f"少 {sorted(STORYLINE_NODES - registered)}"
    )


def test_ai_on_topology_baseline_frozen() -> None:
    """AI 开档全量快照(默认档):普通边 / 屏障 5 道 / 条件路由三源。"""
    _assert_storyline_section(_build(), ai_on=True)


def test_ai_off_topology_baseline_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI 关档全量快照:QA 直等 PTP,屏障少 GAT/PTAT 两道。

    ⚠️ 已知现状(20261113 审计 §6.4,可选清理项,未授权执行):GAT/PTAT 的
    ``add_node`` 在开关分支外,关档时仍会注册 —— 但**不接任何边**(普通边 /
    屏障 / 条件路由均无,下方逐一断言)。把它们移入开关分支属图卫生优化,
    与阶段0 "冻结现状"的口径冲突,留待后续计划单独决策。
    """
    monkeypatch.setattr(config, "STORYLINE_ENABLE_AI_TRANSITION", False)
    monkeypatch.setattr(
        "storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION", False
    )
    g = _build()
    registered = {n for n in getattr(g, "nodes", {}) if n.startswith("storyline_")}
    assert registered == STORYLINE_NODES, (
        f"storyline 节点清单漂移:实际 {sorted(registered)}"
    )
    _assert_storyline_section(g, ai_on=False)

    # AI 两节点关档时不接任何边(注册但未接线 —— 审计 §6.4 记录的现状)
    for s, t in _edge_pairs(g):
        assert _GAT not in (s, t), f"AI 关档残留普通边 {s}→{t}"
        assert _PTAT not in (s, t), f"AI 关档残留普通边 {s}→{t}"
    for srcs, t in getattr(g, "waiting_edges", set()):
        assert _GAT not in srcs and t != _GAT, f"AI 关档残留屏障 {sorted(srcs)}→{t}"
        assert _PTAT not in srcs and t != _PTAT, f"AI 关档残留屏障 {sorted(srcs)}→{t}"
    for src in getattr(g, "branches", {}):
        assert _GAT not in _conditional_targets(g, src)
        assert _PTAT not in _conditional_targets(g, src)
