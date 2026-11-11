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
_STR = "storyline_script_template_recommendation"
_WT = "storyline_search_web_topic"


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
