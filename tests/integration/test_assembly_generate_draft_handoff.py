"""P1-7 集成:``assembly_write_report → generate_draft`` 最终计划交接。

核心断言(计划文档 P1-7 验收):
1. QC 开态整图跑到关卡① 前:write_report 已提交 final plan 三件套;
2. generate_draft **真实写盘**(不预置 draft_path)并消费 final plan ——
   草稿视频轨与候选计划逐段一致,status tag 标明计划来源;
3. 影响链闭环:final_plan_sha256(提交侧固化)+ mapped_draft_sha256
   (消费侧回写)+ 六节点 contribution 齐全且过契约校验。

复用 test_assembly_qc_graph 的 fixtures(patched_assembly_tools / base_state)。
注:stub 模式下 storyline 链对 1KB 假视频切不出镜头(join 产出 clips=0 的空
计划),而 node_05 对空 clips fail-closed —— 两个测试都在 join 出口把
storyline_plan 注入为共享 CanonicalTimeline fixture(3 clips,双源)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import config
from graph import _build_state_graph
from tests.integration.test_assembly_qc_graph import (  # noqa: F401 — pytest fixtures
    _patch_heartbeat,
    base_state,
    fake_video,
    patched_assembly_tools,
)
from tests.support.draft_influence import (
    load_canonical_sample,
)
from assembly_capabilities.influence_trace import (
    canonical_json_sha256,
    load_or_create_trace,
    validate_influence_trace,
)


@pytest.fixture
def handoff_state(base_state: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """去掉预置 draft_path 的 base_state:generate_draft 真实写盘到空目录。"""
    from draft_ops import atomic_writer, draft_publisher

    # integration 目录没有 unit conftest 的全局互斥桩 —— 这里显式注入
    # "剪映不在跑",保证写盘/发布链确定性(发布注册已由 JY_DRAFT_PUBLISH_ENABLED=0 关闭)
    monkeypatch.setattr(atomic_writer, "_default_proc_checker", lambda: False)
    monkeypatch.setattr(draft_publisher, "_default_proc_checker", lambda: False)
    fresh = tmp_path / "drafts" / "handoff"
    fresh.mkdir(parents=True, exist_ok=True)
    # graph.py 在导入时 from config import resolve_draft_dir —— config 与
    # graph 命名空间两处都要 patch(graph 绑定才真正被 generate_draft 读到)
    monkeypatch.setattr(config, "resolve_draft_dir", lambda _s: fresh)
    import graph as _gm

    monkeypatch.setattr(_gm, "resolve_draft_dir", lambda _s: fresh)
    state = dict(base_state)
    state.pop("draft_path", None)
    return state, fresh


def _inject_sample_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """join 出口注入共享 CanonicalTimeline 样例(stub 链对假视频切不出镜头)。

    graph.py 经 ``_join_storyline_mod.storyline_join_node`` 注册节点,patch
    目标是源模块 ``nodes.storyline.join_storyline`` 的属性。
    """
    from nodes.storyline import join_storyline as _join_mod

    real_join = _join_mod.storyline_join_node

    def _join_with_sample(state):  # noqa: ANN001 — LangGraph 节点签名
        out = real_join(state)
        if isinstance(out, dict) and "storyline_plan" in out:
            out = {**out, "storyline_plan": load_canonical_sample()}
        return out

    monkeypatch.setattr(_join_mod, "storyline_join_node", _join_with_sample)


def _invoke_to_before_node_06(state: dict, thread_id: str) -> dict:
    g = _build_state_graph().compile(
        checkpointer=InMemorySaver(),
        interrupt_before=["node_06_human_reorder"],
    )
    return g.invoke(
        state,
        config={"configurable": {"thread_id": thread_id}, "recursion_limit": 60},
    )


def test_final_plan_submitted_and_consumed_by_generate_draft(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    handoff_state,
) -> None:
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    monkeypatch.setattr(config, "ASSEMBLY_QC_MAX_RETRY", 1)
    _inject_sample_plan(monkeypatch)
    state, draft_dir = handoff_state

    out = _invoke_to_before_node_06(state, "assembly-handoff-p1-7")

    # ---- 提交侧:write_report 已提交 final plan 三件套 ----
    final_path = out.get("assembly_final_plan_path")
    assert final_path, f"final plan 未提交,status_log={out.get('status_log')[-8:]}"
    assert Path(final_path).is_file()
    final_sha = out.get("assembly_final_plan_sha256")
    assert final_sha
    candidate = json.loads(
        Path(out["assembly_candidate_plan_path"]).read_text(encoding="utf-8")
    )
    assert final_sha == canonical_json_sha256(candidate)
    assert "assembly_final_plan_submitted" in (out.get("status_log") or [])

    # ---- 消费侧:generate_draft 真实写盘,草稿视频轨与候选计划一致 ----
    draft_file = draft_dir / "draft_content.json"
    assert draft_file.is_file(), "generate_draft 应真实写出 draft_content.json"
    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    from tests.support.draft_influence import draft_video_summary

    summary = draft_video_summary(draft)
    clips = candidate.get("clips") or []
    assert len(summary) == len(clips), (
        f"视频轨段数({len(summary)})应与候选计划 clip 数({len(clips)})一致"
    )
    for seg, clip in zip(summary, clips):
        assert seg["source_start_us"] == int(clip["source_in_ms"]) * 1000
        assert seg["source_end_us"] == int(clip["source_out_ms"]) * 1000
    # 计划来源可观测
    assert any(
        "draft_from_assembly_final_plan" in t for t in (out.get("status_log") or [])
    )

    # ---- 影响链闭环 ----
    trace = load_or_create_trace(
        out["assembly_influence_trace_path"], out["assembly_base_plan_sha256"]
    )
    assert trace["final_plan_sha256"] == final_sha
    assert trace["mapped_draft_sha256"], "generate_draft 应回写 mapped_draft_sha256"
    node_ids = {c["node_id"] for c in trace["node_contributions"]}
    assert {
        "assembly_discover_and_probe",
        "assembly_visual_observe",
        "assembly_build_timeline",
        "assembly_validate_render_qc",
        "assembly_repair_loop",
        "assembly_write_report",
        "generate_draft",
    } <= node_ids
    assert validate_influence_trace(trace) == []
