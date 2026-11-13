"""边治理阶段4(2026-11-12):模板前置拓扑防回归 —— 单元级结构断言,不执行图。

依据《子图边有效性审计与精简设计执行计划_20261112》§5.8/§9 阶段4:

- ``storyline_script_template_recommendation``(STR)必须位于
  ``storyline_generate_script`` **之前**的主链位置:
  ``group_clips → STR →(条件路由)→ search_web_topic | generate_script``;
- 不允许存在反向边 ``GS → STR`` —— 模板推荐若在文案生成之后执行,
  就无法影响刚生成的文案(§5.8 判定的"无效方向")。

屏障形状与全图邻居的完整锁定在
``tests/integration/test_unreachable_nodes_wiring.py``(NEIGHBORS/_barriers);
本文件只锁与边治理阶段4-7 相关的最小结构事实,避免双份维护。

图真源 = ``graph._build_state_graph()`` 的 builder 边(不 compile),
与 wiring 集成测试 ``_barriers`` / ``_builder_outgoing_sources`` 同口径:

- ``builder.edges``:普通边(source, target)二元组集合;
- ``builder.waiting_edges``:列表 fan-in 屏障((源元组, 目标));
- ``builder.branches[src]``:条件路由({分支名: Branch},``Branch.ends`` 为
  {标签: 目的地})。

阶段5/7 将在同一文件续加断言(render 删除、QA 单源、屏障形状)。
"""
from __future__ import annotations

import pytest

import config
import graph as graph_module

_GC = "storyline_group_clips"
_GS = "storyline_generate_script"
_GV = "storyline_generate_voiceover"
_SB = "storyline_select_bgm"
_STR = "storyline_script_template_recommendation"
_WT = "storyline_search_web_topic"
_RTR = "storyline_recommend_transition"
_TXT = "storyline_recommend_text"
_PTP = "storyline_plan_timeline_pro"
_GAT = "storyline_generate_ai_transition"
_PTAT = "storyline_plan_timeline_ai_transition"
_QA = "storyline_qa_gate"
_JOIN = "storyline_join"
_RV = "storyline_render_video"


def _build() -> tuple:
    """构造与 wiring 集成测试同口径的 builder(开关取默认值)。"""
    return graph_module._build_state_graph()


def _edge_pairs(g) -> set[tuple[str, str]]:
    """普通边(含 →END);屏障与条件路由不在此列。"""
    return {(s, t) for s, t in g.edges}


def _barrier_sources(g) -> set[str]:
    """所有 fan-in 屏障的源节点集合。"""
    return {s for srcs, _t in getattr(g, "waiting_edges", set()) for s in srcs}


def _barrier_targets(g) -> set[str]:
    """所有 fan-in 屏障的目标节点集合。"""
    return {t for _srcs, t in getattr(g, "waiting_edges", set())}


def _conditional_targets(g, src: str) -> set[str]:
    """``src`` 的条件路由目的地集合(经 add_conditional_edges 注册的 mapping)。"""
    targets: set[str] = set()
    for _name, spec in getattr(g, "branches", {}).get(src, {}).items():
        ends = getattr(spec, "ends", None)
        if ends:
            targets.update(ends.values())
    return targets


@pytest.fixture(autouse=True)
def _deterministic_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """与 wiring 集成测试同口径:建图开关取确定值,避免环境变量串扰。"""
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    monkeypatch.setattr(config, "STORYLINE_ENABLE_AI_TRANSITION", True)
    monkeypatch.setattr(
        "storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION", True
    )


# ---------------------------------------------------------------------------
# 阶段4:模板推荐前置(附件 §5.8 / §9 阶段4)
# ---------------------------------------------------------------------------
def test_no_reverse_edge_from_generate_script_to_template_rec() -> None:
    """不存在反向边 GS→STR:模板在文案之后执行就无法影响当轮文案(§5.8)。"""
    g = _build()
    assert (_GS, _STR) not in _edge_pairs(g)
    # GS 的任何屏障/条件路由也不得指向 STR
    assert _STR not in _barrier_targets(g)
    assert _STR not in _conditional_targets(g, _GS)


def test_template_rec_is_main_chain_node_before_generate_script() -> None:
    """STR 是主链节点:入边恰为 group_clips(普通边),出口 = 检索条件路由二选一。

    主链节点每轮必跑,因此不在任何屏障源里(与 search_web_topic 的 D10 纪律不同:
    STR 不是条件触发节点,但它也不作为屏障源 —— 它的下游用条件路由表达)。
    """
    g = _build()
    edges = _edge_pairs(g)

    ins = {s for s, t in edges if t == _STR}
    assert ins == {_GC}, f"STR 入边应恰为 group_clips,实际 {sorted(ins)}"
    # 普通出边应为空:出口走条件路由
    outs = {t for s, t in edges if s == _STR}
    assert outs == set(), f"STR 不应有普通出边,实际 {sorted(outs)}"

    cond = _conditional_targets(g, _STR)
    assert cond == {_WT, _GS}, f"STR 条件出口应为 search_web_topic|generate_script,实际 {sorted(cond)}"


def test_search_web_topic_hangs_off_template_rec_not_group_clips() -> None:
    """检索节点挂在 STR 出口(入边=STR、出边=GS 普通边),不是条件屏障(附件 §4.2/§5.2)。

    入边口径:builder 普通边 + "所有把 _WT 作为条件目的地的源" —— STR→WT 是
    条件边,不在 builder.edges 里(与 wiring 集成测试用 compiled get_graph()
    渲染条件边同效果,但这里直接读 builder 真源)。
    """
    g = _build()
    edges = _edge_pairs(g)
    normal_ins = {s for s, t in edges if t == _WT}
    conditional_ins = {
        src
        for src in getattr(g, "branches", {})
        if _WT in _conditional_targets(g, src)
    }
    outs = {t for s, t in edges if s == _WT}
    assert normal_ins == set(), f"search_web_topic 不应有普通入边,实际 {sorted(normal_ins)}"
    assert conditional_ins == {_STR}, f"search_web_topic 条件入边应恰为 STR,实际 {sorted(conditional_ins)}"
    assert outs == {_GS}, f"search_web_topic 出边应恰为 generate_script,实际 {sorted(outs)}"
    # D10:条件触发节点不得作为任何屏障源
    assert _WT not in _barrier_sources(g)
    assert _WT not in _conditional_targets(g, _GC)


# ---------------------------------------------------------------------------
# 阶段5:render_video 节点整体删除(用户指令;附件 §5.9)
# ---------------------------------------------------------------------------
def test_render_video_node_is_completely_gone() -> None:
    """节点注册/普通边/屏障/条件路由/开关/状态字段全部不存在。"""
    g = _build()
    assert _RV not in getattr(g, "nodes", {})
    for s, t in _edge_pairs(g):
        assert _RV not in (s, t), f"残留边 {s}→{t}"
    for srcs, t in getattr(g, "waiting_edges", set()):
        assert _RV not in srcs and t != _RV
    for src in getattr(g, "branches", {}):
        assert _RV not in _conditional_targets(g, src)
    # 开关与状态字段随节点一并删除
    assert not hasattr(config, "STORYLINE_ENABLE_RENDER_SMOKE_TEST")

    from state import WorkflowState

    for key in ("storyline_render_preview_path", "storyline_render_preview_report"):
        assert key not in WorkflowState.__annotations__


# ---------------------------------------------------------------------------
# 阶段7:QA 单源等待最终时间线 + 非重试出口直连 join + 屏障形状锁定
# ---------------------------------------------------------------------------
def test_qa_in_is_final_timeline_only_and_out_goes_straight_to_join() -> None:
    """QA 入边恰为最终时间线节点(默认档 AI 开=PTAT);条件出口 = {GC, join}。

    阶段2:QA 只等最终时间线(不再等 RTR/TXT);阶段5:render_video 删除后
    非重试出口直连 join —— join 也没有任何普通入边(QA→join 是条件边)。
    """
    g = _build()
    edges = _edge_pairs(g)
    normal_ins = {s for s, t in edges if t == _QA}
    barrier_ins = {s for srcs, t in getattr(g, "waiting_edges", set()) if t == _QA}
    assert normal_ins == {_PTAT}, f"QA 普通入边应恰为 PTAT,实际 {sorted(normal_ins)}"
    assert barrier_ins == set(), "QA 不应再作为屏障目标(阶段2 精简为单源)"
    assert _conditional_targets(g, _QA) == {_GC, _JOIN}, (
        f"QA 条件出口应为 group_clips|join,实际 {sorted(_conditional_targets(g, _QA))}"
    )
    assert {t for s, t in edges if s == _QA} == set(), "QA 不应有普通出边"

    join_normal_ins = {s for s, t in edges if t == _JOIN}
    join_cond_ins = {
        src for src in getattr(g, "branches", {}) if _JOIN in _conditional_targets(g, src)
    }
    assert join_normal_ins == set() and join_cond_ins == {_QA}


def test_ai_off_qa_waits_ptp_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI 关档:QA 单源直等 PTP(与 wiring 集成测试 barrier 集合同口径的单元级锁定)。"""
    monkeypatch.setattr(config, "STORYLINE_ENABLE_AI_TRANSITION", False)
    monkeypatch.setattr(
        "storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION", False
    )
    g = _build()
    assert {s for s, t in _edge_pairs(g) if t == _QA} == {_PTP}


def test_storyline_barrier_shapes_locked() -> None:
    """约简后屏障形状锁定(2026-11-14 依赖约简阶段1;只锁 storyline 段,不耦合外层 fan-in)。

    BGM/转场/GAT 三道屏障已缩为单源普通边(GV→SB、SB→RTR、RTR→GAT,
    形状由 wiring 的 NEIGHBORS 表锁定);屏障只剩 PTP 两源与 PTAT 双源。
    """
    g = _build()
    barriers = {(frozenset(s), t) for s, t in getattr(g, "waiting_edges", set())}
    storyline_barriers = {bt for bt in barriers if bt[0] and bt[1].startswith("storyline_")}
    assert storyline_barriers == {
        (frozenset({_RTR, _TXT}), _PTP),      # 2026-11-14 依赖约简:六源缩两源
        (frozenset({_PTP, _GAT}), _PTAT),     # AI 开:基础时间线 + AI 转场
    }
    # 条件触发节点永不进屏障(D10);主链前置节点 STR 也不作为屏障源
    assert _WT not in _barrier_sources(g)
    assert _STR not in _barrier_sources(g)
