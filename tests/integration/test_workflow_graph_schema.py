"""图 Schema 服务与编译图一致性测试(设计《工作流流程图与实时节点追踪》§9.4 阶段1;
2026-11-04 阶段3 auto-only 收敛版)。

验收口径(设计 §9.4 任务 3,阶段3 改写):

(a) schema 节点/边集合与编译图**逐点一致** —— 边真源是
    ``compiled_graph.builder`` 的真实边集(edges / waiting_edges / branches),
    并用独立构建的参照集做交叉断言(见下);另用一条护栏测试钉死
    ``get_graph()`` 画图伪边的行为偏差(模块 docstring 详述);
(b) QC 开关两态下 ``reachable`` / 边数量正确;START 直连
    ``storyline_load_media``(auto-only 单流水线入口);interrupt 关卡
    ①/②/③ 节点可达;human 模式 5 节点与 clean_cache 不再出现在 schema 中;
(c) 每个节点都有元数据(中文名 / 分组 / 描述非空,checkpoint 取值合法)。

关于 ``get_graph()`` 的画图伪边(langgraph 1.2.9 实测,``pregel/_draw.py``):
它按静态模拟推边,会把所有"只入不出"的 termini 节点**合成**普通 ``→__end__``
边,并漏掉"有出边节点"的普通直连 ``→__end__`` 边。因此 schema 以 builder 为
真源;护栏测试断言:drawn − schema 恰为 termini 合成集(当前拓扑 3 个 storyline
死胡同节点),schema − drawn 为空集(→END 真边所在节点的出边只有 →END 时,
画图合成边与真边重合)—— langgraph 升级改变该行为时,这里会先红。

本测试只构图、不执行图(不 invoke),符合设计 §11.5"避免为可视化重复执行图"。
"""

from __future__ import annotations

import pytest

import config
from config import make_checkpointer
from graph import build_graph
from monitoring.workflow_graph import (
    CHECKPOINT_MARKS,
    SCHEMA_VERSION,
    WORKFLOW_ID,
    build_graph_schema,
)

ASSEMBLY_NODES = {
    "assembly_discover_and_probe",
    "assembly_asr_and_visual_observe",
    "assembly_build_timeline",
    "assembly_validate_render_qc",
    "assembly_repair_loop",
    "assembly_write_report",
}

#: 阶段3 删除的 human 模式 5 节点 + 运维节点 + B1 移除的 storyline ASR 旁支 2 节点
#: (不得再出现在编译图与 schema 中)
REMOVED_NODES = {
    "clean_cache",
    "launch_openstoryline",
    "open_preview",
    "checkpoint0_storyline_plan",
    "import_video",
    "get_storyboard_plan",
    "storyline_local_asr",
    "storyline_speech_rough_cut",
}

#: interrupt 关卡节点 → 关卡编号(阶段3 后仅 ①②③)
CHECKPOINT_NODES = {
    "node_06_human_reorder": "①",
    "node_12_human_add_bgm": "②",
    "node_checkpoint3_layout_review": "③",
}


def _normalize(raw: str) -> str:
    return {"__start__": "START", "__end__": "END"}.get(raw, raw)


def _make(monkeypatch, *, qc: bool):
    """按 QC 开关构图,返回 (compiled, schema)。

    阶段3 起 STORYLINE_MODE 恒为 "auto"(config import 期校验),无需注入。
    """
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", qc)
    compiled = build_graph(
        checkpointer=make_checkpointer("memory"),
        thread_id="schema-test",
        start_heartbeat_thread=False,
    )
    schema = build_graph_schema(compiled, config)
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
@pytest.mark.parametrize("qc", [True, False])
def test_schema_matches_builder_truth(monkeypatch, qc):
    compiled, schema = _make(monkeypatch, qc=qc)

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
    """护栏:get_graph() 的画图偏差恰好等于"termini 合成 END 边"。

    langgraph 升级改变画图行为时本测试先红 —— 这是有意的(设计 §11.10 升级护栏)。
    """
    compiled, schema = _make(monkeypatch, qc=True)
    drawn = {
        (_normalize(e.source), _normalize(e.target),
         "conditional" if getattr(e, "conditional", False) else "normal")
        for e in compiled.get_graph().edges
    }
    schema_triples = _edge_triples(schema)

    # drawn − schema = 只入不出节点的合成 →END 边(当前拓扑 3 个 storyline 死胡同)
    # langgraph 规则显式排除 END 自身(if d != END)
    schema_targets = {t for _, t, _ in schema_triples}
    schema_sources = {s for s, _, _ in schema_triples}
    termini = (schema_targets - schema_sources) - {"END"}
    expected_synthetic = {(t, "END", "normal") for t in termini}
    assert drawn - schema_triples == expected_synthetic
    assert len(expected_synthetic) == 3

    # schema − drawn = 有出边节点的普通直连 END(画图漏掉的**真**边)
    # 当前拓扑中 →END 真边所在节点(join_before_delivery / escalate_guardrail_failure)
    # 出边只有 →END,画图合成边与真边重合,故差集为空
    assert schema_triples - drawn == set()

    # QC 关闭构建同规则
    compiled_off, schema_off = _make(monkeypatch, qc=False)
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
    schema = _make_schema(monkeypatch, qc=True)
    assert schema["workflow_id"] == WORKFLOW_ID
    assert schema["schema_version"] == SCHEMA_VERSION
    # auto-only:mode 恒为 "auto";flags 只剩 QC 开关
    assert schema["mode"] == "auto"
    assert schema["flags"] == {"ASSEMBLY_QC_GATE_ENABLED": True}

    schema_off = _make_schema(monkeypatch, qc=False)
    assert schema_off["mode"] == "auto"
    assert schema_off["flags"] == {"ASSEMBLY_QC_GATE_ENABLED": False}


# ===========================================================================
# (b) 开关两态 + auto-only 入口
# ===========================================================================
def test_qc_off_makes_assembly_nodes_unreachable(monkeypatch):
    off = _make_schema(monkeypatch, qc=False)
    by_id = {n["id"]: n for n in off["nodes"]}
    for nid in ASSEMBLY_NODES:
        assert by_id[nid]["reachable"] is False, nid
        assert by_id[nid]["enabled"] is False, nid
    pairs = _edge_pairs(off)
    touched = {p for p in pairs if p[0] in ASSEMBLY_NODES or p[1] in ASSEMBLY_NODES}
    assert touched == set(), f"QC 关闭时不得有任何 assembly 边:{touched}"
    # 应急直连边存在
    assert ("generate_draft", "node_06_human_reorder") in pairs

    on = _make_schema(monkeypatch, qc=True)
    by_id_on = {n["id"]: n for n in on["nodes"]}
    for nid in ASSEMBLY_NODES:
        assert by_id_on[nid]["reachable"] is True, nid
        assert by_id_on[nid]["enabled"] is True, nid
    pairs_on = _edge_pairs(on)
    assert ("generate_draft", "assembly_discover_and_probe") in pairs_on


def test_start_wires_directly_to_storyline_load_media(monkeypatch):
    """阶段3 auto-only:START 直连 storyline_load_media,旧 human 5 节点不复存在。"""
    schema = _make_schema(monkeypatch, qc=True)
    by_id = {n["id"]: n for n in schema["nodes"]}

    # START 直连(普通边)
    kinds = {(e["from"], e["to"]): e["kind"] for e in schema["edges"]}
    assert kinds[("START", "storyline_load_media")] == "normal"

    # 删除节点不复存在(schema 与编译图双侧,编译图侧由 builder_truth 测试覆盖)
    for nid in REMOVED_NODES:
        assert nid not in by_id, nid

    # storyline_* 节点全部属于 auto 链;非 terminal 节点 modes 一律 ["auto"]
    for n in schema["nodes"]:
        if n["kind"] == "terminal":
            assert n["modes"] == [], n["id"]
        else:
            assert n["modes"] == ["auto"], n["id"]

    # auto 链内部关键边仍接线
    pairs = _edge_pairs(schema)
    assert ("storyline_generate_voiceover", "storyline_select_bgm") in pairs
    assert ("storyline_filter_clips", "storyline_group_clips") in pairs


def test_storyline_asr_side_branch_removed(monkeypatch):
    """B1 ASR 移除:切镜后静态直连 understand_clips,ASR 旁支节点/边不复存在。"""
    schema = _make_schema(monkeypatch, qc=True)
    by_id = {n["id"]: n for n in schema["nodes"]}

    assert "storyline_local_asr" not in by_id
    assert "storyline_speech_rough_cut" not in by_id

    pairs = _edge_pairs(schema)
    # 静态确定性主边(不再是条件路由)
    assert ("storyline_split_shots", "storyline_understand_clips") in pairs
    # 旁支节点相关的任何边都不存在
    leftovers = [
        p for p in pairs
        if "storyline_local_asr" in p or "storyline_speech_rough_cut" in p
    ]
    assert leftovers == []


def test_interrupt_checkpoints_reachable(monkeypatch):
    """关卡 ①②③ 三个人工 interrupt 节点都在图中、可达、编号正确。"""
    schema = _make_schema(monkeypatch, qc=True)
    by_id = {n["id"]: n for n in schema["nodes"]}
    for nid, mark in CHECKPOINT_NODES.items():
        assert nid in by_id, nid
        assert by_id[nid]["checkpoint"] == mark, nid
        assert by_id[nid]["reachable"] is True, nid
        assert mark in CHECKPOINT_MARKS

    # 关卡③经条件边触发(布局检测命中),①②是主链必经
    kinds = {(e["from"], e["to"]): e["kind"] for e in schema["edges"]}
    assert kinds[("node_10b_inject_text_fx_en", "node_checkpoint3_layout_review")] == "conditional"


# ===========================================================================
# (c) 元数据完备
# ===========================================================================
@pytest.mark.parametrize("qc", [True, False])
def test_every_node_has_metadata(monkeypatch, qc):
    schema = _make_schema(monkeypatch, qc=qc)
    for n in schema["nodes"]:
        assert n["name"], f"{n['id']} 缺中文名"
        assert n["group"], f"{n['id']} 缺分组"
        assert n["description"], f"{n['id']} 缺描述"
        assert n["kind"] in ("task", "terminal"), n["id"]
        assert n["checkpoint"] is None or n["checkpoint"] in CHECKPOINT_MARKS, n["id"]
        assert isinstance(n["modes"], list), n["id"]
        assert isinstance(n["reachable"], bool) and isinstance(n["enabled"], bool), n["id"]

    # 关卡编号映射正确(阶段3:⓪ 已随 checkpoint0_storyline_plan 删除)
    by_id = {n["id"]: n for n in schema["nodes"]}
    assert by_id["node_06_human_reorder"]["checkpoint"] == "①"
    assert by_id["node_12_human_add_bgm"]["checkpoint"] == "②"
    assert by_id["node_checkpoint3_layout_review"]["checkpoint"] == "③"


def test_start_end_reachable_and_terminal_kind(monkeypatch):
    schema = _make_schema(monkeypatch, qc=True)
    by_id = {n["id"]: n for n in schema["nodes"]}
    assert by_id["START"]["kind"] == "terminal"
    assert by_id["END"]["kind"] == "terminal"
    assert by_id["START"]["reachable"] is True
    assert by_id["END"]["reachable"] is True
