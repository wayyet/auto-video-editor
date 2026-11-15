"""P1-8 集成:assembly 六节点对最终剪映草稿的影响验证(影响链主文件)。

六场景对照(计划文档 P1-8;每条都断言 draft_content.json 业务字段变化或
放行/阻断效果,不是"节点执行过"):

1. **discover → 草稿**(test_discover_black_ranges_change_final_draft):
   改 manifest 黑帧区间(discover 产物)→ build 确定性 trim op → 最终草稿
   clip_0001 源区间从 0-4000ms 变为 2500-4000ms,证据可追溯。
2. **visual evidence → 草稿**(test_visual_evidence_changes_selection_decision):
   固定素材与 manifest,只改观察 fixture(ok+sheet vs degraded)→ 脚本化
   LLM 按证据行分支选段 → 两份最终草稿段数不同(4 vs 3)。
3. **build patch → 草稿**(test_llm_selection_patch_changes_video_track):
   脚本化 LLM 选中未入编候选 → select op 追加 clip → 最终草稿 4 段且
   逐段与候选计划一致(含追加段 5-9s)。
4. **QC 阻断**(test_structural_error_blocks_final_plan_not_gate):结构性
   错误候选 → final plan 不被提交(路由 fail-closed 或报告 ⛔)。
5. **repair 生效**(test_repair_reverts_unsafe_candidate_to_baseline):
   第一轮 QC 注入 broken_reference(clip_ids 定位)→ repair restore 基线
   clip → 第二轮 QC 通过 → 最终草稿回到基线区间(修复结果可见)。
6. **report/hash 守卫**:final plan 文件缺失 / sha 漂移 / 基线漂移的
   fail-closed 由 unit 层覆盖(test_node_05_final_plan.py 用例 E);
   "不提交 → 回退 storyline_plan"由 test_no_candidate_falls_back_to_storyline_plan
   覆盖(本文件)。

统一验收:influence_trace 六节点 contribution 齐全(handoff 测试);
每场景断言最终 draft 业务字段;溯源链 = 最终 clip → patch op → 证据。
注:stub 模式下 storyline 链对 1KB 假视频切不出镜头(join 产出 clips=0),
而 node_05 对空 clips fail-closed —— 用 join 出口注入计划 fixture。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

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
from assembly_capabilities.influence_trace import canonical_json_sha256


# ---------------------------------------------------------------------------
# 共享:计划注入 / manifest 观察 stub / 脚本化 LLM / 整图驱动
# ---------------------------------------------------------------------------
def _inject_plan(monkeypatch: pytest.MonkeyPatch, plan: dict) -> None:
    """join 出口注入指定计划(stub 链对假视频切不出镜头,join 产出 clips=0)。

    graph.py 经 ``_join_storyline_mod.storyline_join_node`` 注册节点,patch
    目标是源模块 ``nodes.storyline.join_storyline`` 的属性。
    """
    from nodes.storyline import join_storyline as _join_mod

    real_join = _join_mod.storyline_join_node

    def _join_with_plan(state):  # noqa: ANN001 — LangGraph 节点签名
        out = real_join(state)
        if isinstance(out, dict) and "storyline_plan" in out:
            out = {**out, "storyline_plan": json.loads(json.dumps(plan))}
        return out

    monkeypatch.setattr(_join_mod, "storyline_join_node", _join_with_plan)


def _sample_plan_on_video(fake_video: Path) -> dict:
    """共享样例计划,media_0001 的 file_uri 重写到假视频(plan 主导发现可解析)。"""
    plan = load_canonical_sample()
    for m in plan["source_media"]:
        if m["media_id"] == "media_0001":
            m["file_uri"] = f"file:///{str(fake_video).replace(chr(92), '/')}"
    return plan


def _stub_manifest(
    monkeypatch: pytest.MonkeyPatch,
    fake_video: Path,
    *,
    candidate_segments: list[dict[str, Any]],
    black_ranges: list[list[float]] | None = None,
    duration: float = 10.0,
) -> None:
    """discover 双 patch:probe/analyze 返回带候选与黑帧区间的完整 report。"""
    from assembly_capabilities import media_probe
    from assembly_capabilities.result import ToolResult
    from nodes.assembly import node_discover_and_probe as node_dp

    probe_data = {
        "input_path": str(fake_video),
        "duration_seconds": duration,
        "summary": {"duration_seconds": duration},
    }
    analysis_report = {
        "input_path": str(fake_video),
        "duration_seconds": duration,
        "scene_change_times": [],
        "candidate_segments": candidate_segments,
        "black_ranges": black_ranges or [],
        "silence_ranges": [],
        "notes": [],
        "logs": {},
        "elapsed_seconds": 0.01,
    }

    def _inspect(args, ctx):  # noqa: ANN001 — 工具签名
        return ToolResult(text="probe ok", data=probe_data, artifacts=[])

    def _analyze(args, ctx):  # noqa: ANN001 — 工具签名
        output_json = args.get("output_json")
        if output_json:
            Path(output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(output_json).write_text(
                json.dumps(analysis_report), encoding="utf-8"
            )
        return ToolResult(
            text="analyze ok",
            data={
                "input_path": str(fake_video),
                "duration_seconds": duration,
                "candidate_segment_count": len(candidate_segments),
            },
            artifacts=[output_json] if output_json else [],
        )

    monkeypatch.setattr(media_probe, "inspect_media", _inspect)
    monkeypatch.setattr(media_probe, "analyze_media", _analyze)
    monkeypatch.setattr(node_dp, "inspect_media", _inspect)
    monkeypatch.setattr(node_dp, "analyze_media", _analyze)


def _stub_observation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str = "ok",
    sheet_name: str = "sheet_A.jpg",
) -> None:
    """观察双 patch:按 manifest 实际 asset_id 动态生成匹配 candidate_id 的条目。"""
    from assembly_capabilities import visual_observe as vo_mod
    from nodes.assembly import node_visual_observe as node_vo

    def _fake(manifest, ctx, out_dir, **_kw):  # noqa: ANN001 — 签名同 build_candidate_observation
        entries = []
        for src in (manifest or {}).get("sources") or []:
            if not isinstance(src, dict):
                continue
            asset_id = str(src.get("asset_id") or "")
            for i in range(len(src.get("candidate_segments") or [])):
                entries.append(
                    {
                        "candidate_id": f"{asset_id}_c{i:03d}",
                        "asset_id": asset_id,
                        "source_path": src.get("source_path"),
                        "index": i,
                        "status": status,
                        "detail": None if status == "ok" else "fixture 注入降级",
                        "observed_flags": [],
                        "frame_paths": [],
                        "sheet_paths": [f"X:/evidence/{sheet_name}"] if status == "ok" else [],
                        "cache_reused": False,
                        "cache_signature": None,
                    }
                )
        data = {
            "schema_version": 1,
            "tool": "assembly_visual_observe.candidate_observation",
            "candidates": entries,
            "candidate_count": len(entries),
            "ok_count": sum(1 for e in entries if e.get("status") == "ok"),
            "degraded": any(e.get("status") != "ok" for e in entries),
            "notes": [],
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "visual_observation.json").write_text(
            json.dumps(data), encoding="utf-8"
        )
        return data

    monkeypatch.setattr(vo_mod, "build_candidate_observation", _fake)
    monkeypatch.setattr(node_vo, "build_candidate_observation", _fake)


@pytest.fixture
def scripted_llm(monkeypatch: pytest.MonkeyPatch):
    """脚本化 LLM:decider(user_prompt) → 选段 JSON(经 set_default_client 注入)。"""
    from storyline_capabilities import vlm_client

    holder: dict[str, Callable[[str], dict[str, Any]]] = {"decider": lambda _p: {"selected_segments": []}}

    class _Scripted:
        def chat(self, **kwargs):  # noqa: ANN001 — LLMClient 协议
            import json as _json

            return _json.dumps(holder["decider"](str(kwargs.get("user_prompt") or "")))

    vlm_client.set_default_client(_Scripted())
    try:
        yield holder
    finally:
        vlm_client.set_default_client(None)


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


def _invoke_to_before_node_06(state: dict, thread_id: str) -> dict:
    g = _build_state_graph().compile(
        checkpointer=InMemorySaver(),
        interrupt_before=["node_06_human_reorder"],
    )
    return g.invoke(
        state,
        config={"configurable": {"thread_id": thread_id}, "recursion_limit": 60},
    )


def _prepare_live_run(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    live_draft_state,
    fake_video: Path,
    *,
    plan: dict | None = None,
) -> tuple[dict, Path]:
    """公共准备:QC 开态 + plan 注入 + 落盘目录。"""
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    monkeypatch.setattr(config, "ASSEMBLY_QC_MAX_RETRY", 2)
    _inject_plan(monkeypatch, plan or _sample_plan_on_video(fake_video))
    state, draft_dir = live_draft_state
    return state, draft_dir


# ---------------------------------------------------------------------------
# 场景 1:discover(黑帧区间)→ 最终草稿源区间变化
# ---------------------------------------------------------------------------
def test_discover_black_ranges_change_final_draft(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    live_draft_state,
    fake_video: Path,
) -> None:
    state, draft_dir = _prepare_live_run(
        monkeypatch, patched_assembly_tools, live_draft_state, fake_video
    )
    _stub_manifest(
        monkeypatch,
        fake_video,
        candidate_segments=[{"index": 0, "start": 0.0, "end": 10.0}],
        black_ranges=[{"start": 1.0, "end": 2.5}],
    )
    _stub_observation(monkeypatch)

    out = _invoke_to_before_node_06(state, "assembly-p18-s1")

    # 提交 + 消费全链成功
    assert out.get("assembly_final_plan_path"), f"status_log={out.get('status_log')[-8:]}"
    draft_file = draft_dir / "draft_content.json"
    assert draft_file.is_file()
    summary = draft_video_summary(json.loads(draft_file.read_text(encoding="utf-8")))

    # clip_0001 被黑帧证据 trim:0-4000 → 2500-4000ms(最大干净子区间)
    assert summary[0]["source_start_us"] == 2_500_000
    assert summary[0]["source_end_us"] == 4_000_000
    # 其余 clip 不受影响(黑帧只落在 media_0001)
    assert summary[1]["source_start_us"] == 1_000_000
    assert summary[2]["source_start_us"] == 8_000_000

    # 证据可追溯:patch op 带黑帧区间证据
    patch = out.get("assembly_draft_patch") or {}
    trim_ops = [
        o for o in patch.get("operations") or []
        if o.get("operation") == "trim" and o.get("target_clip_id") == "clip_0001"
    ]
    assert trim_ops, "应有 clip_0001 的 trim op"
    assert any(str(e).startswith("black:") for e in trim_ops[0].get("evidence_refs") or [])
    # 影响链:build contribution 记录了该 op
    trace = json.loads(Path(out["assembly_influence_trace_path"]).read_text(encoding="utf-8"))
    build_contrib = next(
        c for c in trace["node_contributions"] if c["node_id"] == "assembly_build_timeline"
    )
    assert trim_ops[0]["operation_id"] in (build_contrib.get("affected_operations") or [])


def test_discover_clean_manifest_keeps_baseline_draft(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    live_draft_state,
    fake_video: Path,
) -> None:
    """对照:discover 无黑帧 → 无确定性 op → 最终草稿与基线逐段一致。"""
    state, draft_dir = _prepare_live_run(
        monkeypatch, patched_assembly_tools, live_draft_state, fake_video
    )
    _stub_manifest(
        monkeypatch,
        fake_video,
        candidate_segments=[{"index": 0, "start": 0.0, "end": 10.0}],
        black_ranges=[],
    )
    _stub_observation(monkeypatch)

    out = _invoke_to_before_node_06(state, "assembly-p18-s1-clean")
    assert out.get("assembly_final_plan_path")
    draft_file = draft_dir / "draft_content.json"
    summary = draft_video_summary(json.loads(draft_file.read_text(encoding="utf-8")))
    baseline = load_canonical_sample()
    expected = {c["clip_id"]: c for c in baseline["clips"]}
    assert summary[0]["source_start_us"] == expected["clip_0001"]["source_in_ms"] * 1000
    assert summary[0]["source_end_us"] == expected["clip_0001"]["source_out_ms"] * 1000


# ---------------------------------------------------------------------------
# 场景 2:visual evidence(观察 fixture)→ 选段决策变化
# ---------------------------------------------------------------------------
def test_visual_evidence_changes_selection_decision(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    live_draft_state,
    fake_video: Path,
    scripted_llm,
) -> None:
    candidates = [
        {"index": 0, "start": 0.0, "end": 4.0},
        {"index": 1, "start": 5.0, "end": 9.0},
    ]
    scripted_llm["decider"] = lambda prompt: (
        {"selected_segments": []}
        if "evidence=degraded" in prompt
        else {
            "selected_segments": [
                {"index": 1, "order": 0, "reason": "视觉证据健康,选中未入编候选"}
            ]
        }
    )

    # 变体 A:证据 ok + sheet → 选中候选 #1 → 草稿 4 段
    state, draft_dir = _prepare_live_run(
        monkeypatch, patched_assembly_tools, live_draft_state, fake_video
    )
    _stub_manifest(monkeypatch, fake_video, candidate_segments=candidates)
    _stub_observation(monkeypatch, status="ok", sheet_name="sheet_A.jpg")
    out_a = _invoke_to_before_node_06(state, "assembly-p18-s2-ok")
    draft_a = draft_video_summary(
        json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
    )
    assert len(draft_a) == 4, f"证据 ok 应触发选段追加,实际 {len(draft_a)} 段"
    assert draft_a[3]["source_start_us"] == 5_000_000
    assert draft_a[3]["source_end_us"] == 9_000_000

    # 变体 B:同一 manifest,证据 degraded → 不选段 → 草稿 3 段 == 基线
    state_b, draft_dir_b = _prepare_live_run(
        monkeypatch, patched_assembly_tools, live_draft_state, fake_video
    )
    _stub_manifest(monkeypatch, fake_video, candidate_segments=candidates)
    _stub_observation(monkeypatch, status="degraded")
    out_b = _invoke_to_before_node_06(state_b, "assembly-p18-s2-degraded")
    draft_b = draft_video_summary(
        json.loads((draft_dir_b / "draft_content.json").read_text(encoding="utf-8"))
    )
    assert len(draft_b) == 3, "证据 degraded 不应触发选段"
    baseline = load_canonical_sample()
    assert draft_b[0]["source_start_us"] == baseline["clips"][0]["source_in_ms"] * 1000
    # 唯一变量是观察 fixture → 决策差异必须由视觉证据驱动
    assert len(draft_a) != len(draft_b), "同一 manifest 不同视觉证据必须产生不同草稿"


# ---------------------------------------------------------------------------
# 场景 3:build patch(select op)→ 最终草稿 video track 对应变化
# ---------------------------------------------------------------------------
def test_llm_selection_patch_changes_video_track(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    live_draft_state,
    fake_video: Path,
    scripted_llm,
) -> None:
    scripted_llm["decider"] = lambda _prompt: {
        "selected_segments": [{"index": 1, "order": 0, "reason": "追加未入编候选"}]
    }
    state, draft_dir = _prepare_live_run(
        monkeypatch, patched_assembly_tools, live_draft_state, fake_video
    )
    _stub_manifest(
        monkeypatch,
        fake_video,
        candidate_segments=[
            {"index": 0, "start": 0.0, "end": 4.0},
            {"index": 1, "start": 5.0, "end": 9.0},
        ],
    )
    _stub_observation(monkeypatch, status="ok", sheet_name="sheet_B.jpg")

    out = _invoke_to_before_node_06(state, "assembly-p18-s3")

    candidate = json.loads(
        Path(out["assembly_candidate_plan_path"]).read_text(encoding="utf-8")
    )
    draft = json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
    summary = draft_video_summary(draft)
    clips = candidate.get("clips") or []
    assert len(summary) == len(clips) == 4
    # 逐段一致:最终草稿 == 候选计划映射(含 select 追加段)
    for seg, clip in zip(summary, clips):
        assert seg["source_start_us"] == int(clip["source_in_ms"]) * 1000
        assert seg["source_end_us"] == int(clip["source_out_ms"]) * 1000
    # select op 证据引用候选 id
    patch = out.get("assembly_draft_patch") or {}
    select_ops = [o for o in patch.get("operations") or [] if o.get("operation") == "select"]
    assert select_ops, "应有 select op"
    assert any(
        str(e).endswith("_c001") for e in select_ops[0].get("evidence_refs") or []
    ), "select op 证据应引用候选 #1 的 candidate_id"


# ---------------------------------------------------------------------------
# 场景 5:repair 真实回滚不安全候选 → 最终草稿反映修复结果
# ---------------------------------------------------------------------------
def test_repair_reverts_unsafe_candidate_to_baseline(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    live_draft_state,
    fake_video: Path,
) -> None:
    """第一轮 QC 注入 broken_reference(clip_ids 定位)→ repair restore 基线
    clip → 第二轮 QC 通过 → 最终草稿 = 基线区间(而非被 trim 的候选)。"""
    from assembly_capabilities import draft_preview_adapter
    from nodes.assembly import node_validate_render_qc

    real_validate = draft_preview_adapter.validate_candidate_draft
    calls = {"n": 0}

    def _validate_first_round_only(candidate_plan, draft_content, **kw):
        result = real_validate(candidate_plan, draft_content, **kw)
        calls["n"] += 1
        if calls["n"] == 1:
            result = dict(result)
            result["issues"] = list(result.get("issues") or []) + [
                {
                    "severity": "error",
                    "code": "broken_reference",
                    "check": "track_references",
                    "message": "注入:clip_0001 素材引用损坏(第一轮)",
                    "clip_id": "clip_0001",
                    "material_id": "video-1",
                }
            ]
        return result

    monkeypatch.setattr(
        draft_preview_adapter, "validate_candidate_draft", _validate_first_round_only
    )
    monkeypatch.setattr(
        node_validate_render_qc,
        "validate_candidate_draft",
        _validate_first_round_only,
        raising=False,
    )

    state, draft_dir = _prepare_live_run(
        monkeypatch, patched_assembly_tools, live_draft_state, fake_video
    )
    _stub_manifest(
        monkeypatch,
        fake_video,
        candidate_segments=[{"index": 0, "start": 0.0, "end": 10.0}],
        black_ranges=[{"start": 1.0, "end": 2.5}],
    )
    _stub_observation(monkeypatch)

    out = _invoke_to_before_node_06(state, "assembly-p18-s5")

    # repair 真实回滚:历史有 restore 应用 + 前后 sha 不同
    history = json.loads(
        Path(out["assembly_repair_history_path"]).read_text(encoding="utf-8")
    )
    rounds = history if isinstance(history, list) else history.get("rounds") or []
    assert rounds, "应有一轮修复"
    r1 = rounds[0]
    # applied_operations 是 op id 字符串列表;restore 策略记录在 strategy_notes
    assert r1.get("applied_operations"), f"第一轮应有真实应用的 op:{r1}"
    notes = " ".join(str(n) for n in (r1.get("strategy_notes") or []))
    assert "restore" in notes, f"broken_reference 应走 restore 策略:{notes}"
    assert r1.get("before_plan_sha256") != r1.get("after_plan_sha256")

    # 最终草稿回到基线(修复结果可见,而非被 trim 的候选)
    assert out.get("assembly_final_plan_path")
    draft = json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
    summary = draft_video_summary(draft)
    baseline = load_canonical_sample()
    assert summary[0]["source_start_us"] == baseline["clips"][0]["source_in_ms"] * 1000
    assert summary[0]["source_end_us"] == baseline["clips"][0]["source_out_ms"] * 1000
    assert calls["n"] >= 2, "repair 后必须复验"
    assert "assembly_final_plan_submitted" in (out.get("status_log") or [])


# ---------------------------------------------------------------------------
# 场景 4:QC 结构性错误阻断(P1-7 首批已落,保留)
# ---------------------------------------------------------------------------
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
    monkeypatch.setattr(
        node_validate_render_qc,
        "validate_candidate_draft",
        _validate_with_structural,
        raising=False,
    )
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    monkeypatch.setattr(config, "ASSEMBLY_QC_MAX_RETRY", 1)
    _inject_plan(monkeypatch, load_canonical_sample())

    g = _build_state_graph().compile(
        checkpointer=InMemorySaver(),
        interrupt_before=["node_06_human_reorder"],
    )
    out = g.invoke(
        base_state,
        config={"configurable": {"thread_id": "assembly-influence-blocked"}},
    )

    submitted = "assembly_final_plan_submitted" in (out.get("status_log") or [])
    assert not submitted, "结构性错误候选绝不能被提交为最终计划"
    if out.get("assembly_report_path"):
        report = Path(out["assembly_report_path"]).read_text(encoding="utf-8")
        assert "⛔" in report or "无候选" in report


# ---------------------------------------------------------------------------
# 回退场景(P1-7 首批已落):不提交 → 回退 storyline_plan
# ---------------------------------------------------------------------------
def test_no_candidate_falls_back_to_storyline_plan(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    live_draft_state,
) -> None:
    """build 不产候选 → 不提交 final plan → generate_draft 回退 storyline_plan。"""
    import graph as graph_mod

    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    monkeypatch.setattr(config, "ASSEMBLY_QC_MAX_RETRY", 2)
    _inject_plan(monkeypatch, load_canonical_sample())

    def _stub_build_timeline(state):  # noqa: ANN001 — LangGraph 节点签名
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

    assert "assembly_final_plan_path" not in out
    assert "assembly_final_plan_submitted" not in (out.get("status_log") or [])
    report = Path(out["assembly_report_path"]).read_text(encoding="utf-8")
    assert "无候选" in report

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
