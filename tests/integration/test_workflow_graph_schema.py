"""图 Schema 服务与编译图一致性测试(设计《工作流流程图与实时节点追踪》§9.4 阶段1)。

验收口径(设计 §9.4 任务 3):

(a) schema 节点/边集合与编译图**逐点一致** —— 边真源是
    ``compiled_graph.builder`` 的真实边集(edges / waiting_edges / branches),
    并用独立构建的参照集做交叉断言(见下);另用一条护栏测试钉死
    ``get_graph()`` 画图伪边的行为偏差(模块 docstring 详述);
(b) QC 开关两态、fusion 开关两态下 ``reachable`` / 边数量正确
    (fusion 开 = 恰好多 21 条"锚点 → 侧支"边,侧支边在关时不存在);
(c) 每个节点都有元数据(中文名 / 分组 / 描述非空,checkpoint 取值合法)。

关于 ``get_graph()`` 的画图伪边(langgraph 1.2.9 实测,``pregel/_draw.py``):
它按静态模拟推边,会把所有"只入不出"的 termini 节点**合成**普通 ``→__end__``
边,并漏掉"有出边节点"的普通直连 ``→__end__`` 边。因此 schema 以 builder 为
真源;护栏测试断言:drawn − schema 恰为 termini 合成集,schema − drawn 恰为
被画图丢失的真直连集 —— langgraph 升级改变该行为时,这里会先红。

本测试只构图、不执行图(不 invoke),符合设计 §11.5"避免为可视化重复执行图"。
"""

from __future__ import annotations

import pytest

import config
import graph as graph_mod
from config import make_checkpointer
from graph import build_graph
from monitoring.workflow_graph import (
    CHECKPOINT_MARKS,
    SCHEMA_VERSION,
    WORKFLOW_ID,
    build_graph_schema,
)
from nodes.storyline.fusion import FUSION_SPECS, anchor_of

ASSEMBLY_NODES = {
    "assembly_discover_and_probe",
    "assembly_asr_and_visual_observe",
    "assembly_build_timeline",
    "assembly_validate_render_qc",
    "assembly_repair_loop",
    "assembly_write_report",
}

STORYLINE_NODES = {s.node_name for s in FUSION_SPECS}


def _normalize(raw: str) -> str:
    return {"__start__": "START", "__end__": "END"}.get(raw, raw)


def _make(monkeypatch, *, fusion: bool, qc: bool, mode: str | None = None):
    """按开关构图(与 test_human_fusion_graph.py 同款惯例),返回 (compiled, schema)。

    mode 用 ``graph._STORYLINE_MODE_OVERRIDE`` contextvar 注入 —— 与
    ``graph._build_state_graph`` 构图时读的是同一个变量,构图与 schema 解析
    必须包在同一个 token 生命周期里。
    """
    monkeypatch.setattr(config, "STORYLINE_HUMAN_FUSION", fusion)
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", qc)
    token = graph_mod._STORYLINE_MODE_OVERRIDE.set(mode) if mode else None
    try:
        compiled = build_graph(
            checkpointer=make_checkpointer("memory"),
            thread_id="schema-test",
            start_heartbeat_thread=False,
        )
        schema = build_graph_schema(compiled, config)
    finally:
        if token is not None:
            graph_mod._STORYLINE_MODE_OVERRIDE.reset(token)
    return compiled, schema


def _make_schema(monkeypatch, **kw) -> dict:
    return _make(monkeypatch, **kw)[1]


def _edge_triples(schema: dict) -> set[tuple[str, str, str]]:
    return {(e["from"], e["to"], e["kind"]) for e in schema["edges"]}


def _edge_pairs(schema: dict) -> set[tuple[str, str]]:
    return {(e["from"], e["to"]) for e in schema["edges"]}


def _builder_truth(compiled) -> tuple[set[str], set[tuple[str, str, str]]]:
    """测试侧**独立**从 builder 构造真源参照(不复用被测代码的实现)。"""
    b = compiled.builder
    nodes = {"START", "END"} | set(b.nodes.keys())
    triples: set[tuple[str, str, str]] = set()
    for s, d in b.edges:
        triples.add((_normalize(s), _normalize(d), "normal"))
    for srcs, d in b.waiting_edges:
        for s in srcs:
            triples.add((_normalize(s), _normalize(d), "normal"))
    for src, named in b.branches.items():
        for _name, spec in named.items():
            for _k, d in (getattr(spec, "ends", None) or {}).items():
                triples.add((_normalize(src), _normalize(d), "conditional"))
    return nodes, triples


# ===========================================================================
# (a) 与编译图逐点一致(边真源 = builder;独立参照)
# ===========================================================================
@pytest.mark.parametrize("fusion", [True, False])
@pytest.mark.parametrize("qc", [True, False])
def test_schema_matches_builder_truth(monkeypatch, fusion, qc):
    compiled, schema = _make(monkeypatch, fusion=fusion, qc=qc)

    truth_nodes, truth_edges = _builder_truth(compiled)
    schema_node_ids = {n["id"] for n in schema["nodes"]}
    assert schema_node_ids == truth_nodes
    assert len(schema["nodes"]) == len(truth_nodes)
    assert _edge_triples(schema) == truth_edges
    assert len(schema["edges"]) == len(truth_edges)

    # 边 id 唯一
    edge_ids = [e["id"] for e in schema["edges"]]
    assert len(edge_ids) == len(set(edge_ids))


def test_schema_matches_drawn_graph_with_documented_artifacts(monkeypatch):
    """护栏:get_graph() 的画图偏差恰好等于"termini 合成 END 边 + 丢失的真直连"。

    langgraph 升级改变画图行为时本测试先红 —— 这是有意的(设计 §11.10 升级护栏)。
    """
    compiled_on, schema_on = _make(monkeypatch, fusion=True, qc=True)
    drawn_on = {
        (_normalize(e.source), _normalize(e.target),
         "conditional" if getattr(e, "conditional", False) else "normal")
        for e in compiled_on.get_graph().edges
    }
    schema_triples_on = _edge_triples(schema_on)

    # drawn − schema = 只入不出节点的合成 →END 边(fusion-on:21 个侧支节点)
    # langgraph 规则显式排除 END 自身(if d != END)
    schema_targets = {t for _, t, _ in schema_triples_on}
    schema_sources = {s for s, _, _ in schema_triples_on}
    termini = (schema_targets - schema_sources) - {"END"}
    expected_synthetic = {(t, "END", "normal") for t in termini}
    assert drawn_on - schema_triples_on == expected_synthetic
    assert len(expected_synthetic) == 21

    # schema − drawn = 有出边节点的普通直连 END(画图漏掉的**真**边)
    assert schema_triples_on - drawn_on == {("join_before_delivery", "END", "normal")}

    # fusion-off 构建同规则(此时死胡同只有 3 个 auto 旁支,join 是 terminus)
    compiled_off, schema_off = _make(monkeypatch, fusion=False, qc=True)
    drawn_off = {
        (_normalize(e.source), _normalize(e.target),
         "conditional" if getattr(e, "conditional", False) else "normal")
        for e in compiled_off.get_graph().edges
    }
    schema_triples_off = _edge_triples(schema_off)
    off_targets = {t for _, t, _ in schema_triples_off}
    off_sources = {s for s, _, _ in schema_triples_off}
    expected_synthetic_off = {(t, "END", "normal")
                              for t in (off_targets - off_sources) - {"END"}}
    assert drawn_off - schema_triples_off == expected_synthetic_off
    assert schema_triples_off - drawn_off == set()


def test_schema_header_and_flags(monkeypatch):
    schema = _make_schema(monkeypatch, fusion=True, qc=True)
    assert schema["workflow_id"] == WORKFLOW_ID
    assert schema["schema_version"] == SCHEMA_VERSION
    assert schema["mode"] == "human"
    assert schema["flags"]["ASSEMBLY_QC_GATE_ENABLED"] is True
    assert schema["flags"]["STORYLINE_HUMAN_FUSION"] is True

    schema_off = _make_schema(monkeypatch, fusion=False, qc=False)
    assert schema_off["flags"]["ASSEMBLY_QC_GATE_ENABLED"] is False
    assert schema_off["flags"]["STORYLINE_HUMAN_FUSION"] is False


# ===========================================================================
# (b) 开关两态
# ===========================================================================
@pytest.mark.parametrize("fusion", [True, False])
def test_qc_off_makes_assembly_nodes_unreachable(monkeypatch, fusion):
    off = _make_schema(monkeypatch, fusion=fusion, qc=False)
    by_id = {n["id"]: n for n in off["nodes"]}
    for nid in ASSEMBLY_NODES:
        assert by_id[nid]["reachable"] is False, nid
        assert by_id[nid]["enabled"] is False, nid
    pairs = _edge_pairs(off)
    touched = {p for p in pairs if p[0] in ASSEMBLY_NODES or p[1] in ASSEMBLY_NODES}
    assert touched == set(), f"QC 关闭时不得有任何 assembly 边:{touched}"
    # 应急直连边存在
    assert ("generate_draft", "node_06_human_reorder") in pairs

    on = _make_schema(monkeypatch, fusion=fusion, qc=True)
    by_id_on = {n["id"]: n for n in on["nodes"]}
    for nid in ASSEMBLY_NODES:
        assert by_id_on[nid]["reachable"] is True, nid
        assert by_id_on[nid]["enabled"] is True, nid
    pairs_on = _edge_pairs(on)
    assert ("generate_draft", "assembly_discover_and_probe") in pairs_on


def test_fusion_on_adds_exactly_21_side_edges(monkeypatch):
    off = _make_schema(monkeypatch, fusion=False, qc=True)
    on = _make_schema(monkeypatch, fusion=True, qc=True)

    pairs_off = _edge_pairs(off)
    pairs_on = _edge_pairs(on)

    # 开:恰好多出 21 条"锚点 → 侧支"边
    expected_sides = {(anchor_of(s, qc_enabled=True), s.node_name) for s in FUSION_SPECS}
    assert len(expected_sides) == 21
    assert pairs_on - pairs_off == expected_sides

    # 关:侧支边一条都不存在
    assert pairs_off & expected_sides == set()

    # 开启时侧支节点经锚点可达
    by_id = {n["id"]: n for n in on["nodes"]}
    for s in FUSION_SPECS:
        assert by_id[s.node_name]["reachable"] is True, s.node_name

    # 开启时 storyline 节点属于 human 链;关闭时属 auto 链
    for s in FUSION_SPECS:
        assert by_id[s.node_name]["modes"] == ["human"], s.node_name
    by_id_off = {n["id"]: n for n in off["nodes"]}
    for s in FUSION_SPECS:
        assert by_id_off[s.node_name]["modes"] == ["auto"], s.node_name
        assert by_id_off[s.node_name]["reachable"] is True  # auto 链已接线


def test_fusion_off_wires_full_auto_chain(monkeypatch):
    off = _make_schema(monkeypatch, fusion=False, qc=True)
    pairs = _edge_pairs(off)
    # auto 链内部边(fusion 开启时不接线)
    assert ("storyline_generate_voiceover", "storyline_select_bgm") in pairs
    assert ("storyline_filter_clips", "storyline_group_clips") in pairs
    # qa 回环(条件边)
    kinds = {(e["from"], e["to"]): e["kind"] for e in off["edges"]}
    assert kinds[("storyline_qa_gate", "storyline_group_clips")] == "conditional"
    assert kinds[("open_preview", "storyline_load_media")] == "conditional"

    on = _make_schema(monkeypatch, fusion=True, qc=True)
    pairs_on = _edge_pairs(on)
    assert ("storyline_generate_voiceover", "storyline_select_bgm") not in pairs_on
    assert ("storyline_filter_clips", "storyline_group_clips") not in pairs_on


def test_auto_mode_build(monkeypatch):
    schema = _make_schema(monkeypatch, fusion=True, qc=True, mode="auto")
    assert schema["mode"] == "auto"
    # fusion 只在 human 模式生效:即使开关为开,auto 构建也不接侧支边
    by_id = {n["id"]: n for n in schema["nodes"]}
    for s in FUSION_SPECS:
        assert by_id[s.node_name]["modes"] == ["auto"], s.node_name
    pairs = _edge_pairs(schema)
    assert (anchor_of(next(iter(FUSION_SPECS)), qc_enabled=True),
            next(iter(FUSION_SPECS)).node_name) not in pairs
    # auto 链已接线
    assert ("storyline_generate_voiceover", "storyline_select_bgm") in pairs


# ===========================================================================
# (c) 元数据完备
# ===========================================================================
@pytest.mark.parametrize("fusion", [True, False])
@pytest.mark.parametrize("qc", [True, False])
def test_every_node_has_metadata(monkeypatch, fusion, qc):
    schema = _make_schema(monkeypatch, fusion=fusion, qc=qc)
    for n in schema["nodes"]:
        assert n["name"], f"{n['id']} 缺中文名"
        assert n["group"], f"{n['id']} 缺分组"
        assert n["description"], f"{n['id']} 缺描述"
        assert n["kind"] in ("task", "terminal"), n["id"]
        assert n["checkpoint"] is None or n["checkpoint"] in CHECKPOINT_MARKS, n["id"]
        assert isinstance(n["modes"], list), n["id"]
        assert isinstance(n["reachable"], bool) and isinstance(n["enabled"], bool), n["id"]

    # 关卡编号映射正确
    by_id = {n["id"]: n for n in schema["nodes"]}
    assert by_id["checkpoint0_storyline_plan"]["checkpoint"] == "⓪"
    assert by_id["node_06_human_reorder"]["checkpoint"] == "①"
    assert by_id["node_12_human_add_bgm"]["checkpoint"] == "②"
    assert by_id["node_checkpoint3_layout_review"]["checkpoint"] == "③"
    assert by_id["import_video"]["checkpoint"] is None
    assert by_id["import_video"]["waiting_kind"] == "web_file_handshake"


def test_clean_cache_registered_but_isolated(monkeypatch):
    schema = _make_schema(monkeypatch, fusion=True, qc=True)
    by_id = {n["id"]: n for n in schema["nodes"]}
    assert "clean_cache" in by_id
    assert by_id["clean_cache"]["reachable"] is False
    assert by_id["clean_cache"]["enabled"] is False
    assert by_id["clean_cache"]["modes"] == []
    pairs = _edge_pairs(schema)
    assert not any("clean_cache" in p for p in pairs)


def test_start_end_reachable_and_terminal_kind(monkeypatch):
    schema = _make_schema(monkeypatch, fusion=True, qc=True)
    by_id = {n["id"]: n for n in schema["nodes"]}
    assert by_id["START"]["kind"] == "terminal"
    assert by_id["END"]["kind"] == "terminal"
    assert by_id["START"]["reachable"] is True
    assert by_id["END"]["reachable"] is True
