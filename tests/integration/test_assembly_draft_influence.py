"""P1-7~P1-8 集成:assembly 六节点对最终剪映草稿的影响验证(影响链主文件)。

P1-7 首批场景:
- **未提交 final plan 时回退原始 storyline_plan**:build_timeline 不产出候选
  (旧路径兜底)→ write_report not_available → generate_draft 消费
  storyline_plan,主链不断,计划来源 tag 可观测,report.md 记录"无候选";
- **结构性错误候选不进关卡① 当交付物**:候选 QC 注入 broken_reference
  结构性 error → final plan 不被提交(路由 fail-closed 或报告 ⛔)。

P1-8 将在本文件继续追加六个影响场景(改 discover/observe/build/QC/repair/
report 输入 → 最终 draft 业务字段变化或放行/阻断效果)。
注:stub 模式下 storyline 链对 1KB 假视频切不出镜头(join 产出 clips=0),
而 node_05 对空 clips fail-closed —— 用 join 出口注入共享 CanonicalTimeline
样例(与 handoff 测试同一配方)。
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
    draft_video_summary,
    load_canonical_sample,
)


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


@pytest.fixture
def live_draft_state(base_state: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """去掉预置 draft_path:generate_draft 真实写盘(同 handoff 测试配方)。"""
    from draft_ops import atomic_writer, draft_publisher

    monkeypatch.setattr(atomic_writer, "_default_proc_checker", lambda: False)
    monkeypatch.setattr(draft_publisher, "_default_proc_checker", lambda: False)
    fresh = tmp_path / "drafts" / "live"
    fresh.mkdir(parents=True, exist_ok=True)
    # graph.py 在导入时 from config import resolve_draft_dir —— config 与
    # graph 命名空间两处都要 patch(graph 绑定才真正被 generate_draft 读到)
    monkeypatch.setattr(config, "resolve_draft_dir", lambda _s: fresh)
    import graph as _gm

    monkeypatch.setattr(_gm, "resolve_draft_dir", lambda _s: fresh)
    state = dict(base_state)
    state.pop("draft_path", None)
    return state, fresh


def test_no_candidate_falls_back_to_storyline_plan(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    live_draft_state,
) -> None:
    """build 不产候选 → 不提交 final plan → generate_draft 回退 storyline_plan。"""
    import graph as graph_mod

    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    monkeypatch.setattr(config, "ASSEMBLY_QC_MAX_RETRY", 2)
    _inject_sample_plan(monkeypatch)

    def _stub_build_timeline(state):  # noqa: ANN001 — LangGraph 节点签名
        """不产 candidate/timeline(复现 build 失败兜底场景)。"""
        return {
            "status_log": list(state.get("status_log", []) or [])
            + ["assembly_build_timeline_stub"]
        }

    monkeypatch.setattr(graph_mod, "assembly_build_timeline_node", _stub_build_timeline)

    g = _build_state_graph().compile(
        checkpointer=InMemorySaver(),
        interrupt_before=["node_06_human_reorder"],
    )
    state, draft_dir = live_draft_state
    out = g.invoke(
        state,
        config={
            "configurable": {"thread_id": "assembly-influence-fallback"},
            "recursion_limit": 60,
        },
    )

    # 提交侧:not_available,无 final plan
    assert "assembly_final_plan_path" not in out
    assert "assembly_final_plan_submitted" not in (out.get("status_log") or [])
    report = Path(out["assembly_report_path"]).read_text(encoding="utf-8")
    assert "无候选" in report

    # 消费侧:回退 storyline_plan,主链不断,草稿照常生成
    draft_file = draft_dir / "draft_content.json"
    assert draft_file.is_file(), "回退路径也必须产出草稿(主链不断)"
    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    summary = draft_video_summary(draft)
    plan = out.get("storyline_plan") or {}
    clips = plan.get("clips") or []
    assert len(summary) == len(clips), "回退草稿视频轨应与原始 storyline_plan 一致"
    for seg, clip in zip(summary, clips):
        assert seg["source_start_us"] == int(clip["source_in_ms"]) * 1000
    assert any(
        "draft_from_storyline_plan" in t for t in (out.get("status_log") or [])
    )


def test_structural_error_blocks_final_plan_not_gate(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    base_state: dict,
) -> None:
    """结构性错误候选 → write_report 拒绝提交(防御纵深,不进关卡① 当交付物)。"""
    from assembly_capabilities import draft_preview_adapter
    from nodes.assembly import node_validate_render_qc

    real_validate = draft_preview_adapter.validate_candidate_draft

    def _validate_with_structural(candidate_plan, draft_content, **kwargs):
        result = real_validate(candidate_plan, draft_content, **kwargs)
        result = dict(result)
        result["issues"] = list(result.get("issues") or []) + [
            {
                "severity": "error",
                "code": "broken_reference",
                "check": "track_references",
                "message": "悬空素材引用(注入)",
            }
        ]
        return result

    monkeypatch.setattr(
        draft_preview_adapter, "validate_candidate_draft", _validate_with_structural
    )
    # validate 节点命名空间绑定同步替换(dual-patch 模式)
    monkeypatch.setattr(
        node_validate_render_qc,
        "validate_candidate_draft",
        _validate_with_structural,
        raising=False,
    )
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    monkeypatch.setattr(config, "ASSEMBLY_QC_MAX_RETRY", 1)
    _inject_sample_plan(monkeypatch)

    g = _build_state_graph().compile(
        checkpointer=InMemorySaver(),
        interrupt_before=["node_06_human_reorder"],
    )
    out = g.invoke(
        base_state,
        config={"configurable": {"thread_id": "assembly-influence-blocked"}},
    )

    # 结构性错误 → validate escalated;达到上限后 fail-closed 路由(assembly_failed)
    # 或 write_report 阻断 —— 两种路径都不允许 final plan 被提交
    submitted = "assembly_final_plan_submitted" in (out.get("status_log") or [])
    assert not submitted, "结构性错误候选绝不能被提交为最终计划"
    if out.get("assembly_report_path"):
        report = Path(out["assembly_report_path"]).read_text(encoding="utf-8")
        assert "⛔" in report or "无候选" in report
