"""阶段四集成测试 — Phase 5 assembly QC 通道(plan §十 阶段四 / ADR-3)。

测试矩阵:
1. 默认 ``ASSEMBLY_QC_GATE_ENABLED=True`` 时,图能成功 compile
2. ``ASSEMBLY_QC_GATE_ENABLED=False`` 时,图仍能 compile,且
   ``generate_draft → node_06_human_reorder`` 直接相连(无 assembly 节点)
3. ``ASSEMBLY_QC_GATE_ENABLED=False`` 时,图拓扑上**没有**从
   ``assembly_validate_render_qc`` 到 ``assembly_write_report`` 的边
4. ``ASSEMBLY_QC_GATE_ENABLED=True`` 时,图拓扑上有完整的 6 节点链路
5. 节点 ``route_after_assembly_qc`` 在 ``assembly_qc_status == "pass"`` 时
   走 ``assembly_write_report``(已在单元测试覆盖,这里仅 spot-check)
6. **核心集成**:从 ``generate_draft`` 端到端 invoke 到 ``node_06_human_reorder``
   中断之前,验证 ``outputs/<job>/assembly/`` 下 8 个产物文件全部生成
   (mock 掉 ``assembly_capabilities.*`` 工具,避免依赖 ffmpeg)

参考:plan §八 graph.py 接线改动 + §十一 验收标准。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import config
from graph import _build_state_graph


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _patch_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """屏蔽心跳线程,避免单测期间写文件。"""
    monkeypatch.setattr("graph.start_heartbeat", lambda: None)


@pytest.fixture
def fake_video(tmp_path: Path) -> Path:
    p = tmp_path / "input.mp4"
    p.write_bytes(b"\x00" * 1024)
    return p


@pytest.fixture
def base_state(fake_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """最小可用的 WorkflowState,直接喂进 ``generate_draft`` 之后的下一个节点。"""
    # mock resolve_draft_dir 让 generate_draft 走预置 fixture 路径
    draft_dir = tmp_path / "drafts" / "default"
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_file = draft_dir / "draft_content.json"
    draft_file.write_text(
        json.dumps(
            {
                "canvas_config": {"width": 1080, "height": 1920},
                "duration": 30_000_000,
                "materials": {"videos": [{"id": "v1"}]},
                "tracks": [
                    {
                        "type": "video",
                        "fps": 30,
                        "segments": [
                            {"id": "s1", "target_timerange": {"start": 0, "duration": 30_000_000}}
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "resolve_draft_dir", lambda _s: draft_dir)
    return {
        "session_id": "assembly-qc-int",
        "video_input_path": str(fake_video),
        "storyline_outputs_root": str(tmp_path / "outputs" / "assembly-qc-int"),
        "draft_path": str(draft_file),
        "draft_encryption_status": "plaintext",
        "draft_version_strategy": "strategy_a_version_lock",
        "error_log": [],
        "status_log": [],
    }


@pytest.fixture
def patched_assembly_tools(monkeypatch: pytest.MonkeyPatch, base_state: dict) -> None:
    """把 ``assembly_capabilities.*`` 工具全 mock 掉,产出合法空产物。

    每个工具返回一个 ``ToolResult``,并把产物 JSON 写到 ``args.output_json`` 指定的
    路径(模拟真实工具的副作用)。这样:
    - 节点调用全部不依赖 ffmpeg / 真实视频
    - 6 个 assembly 节点能在 test 环境完整跑完
    - validate / qc / render 都返回合法 JSON 产物
    """
    from assembly_capabilities.result import ToolResult

    def _make_stub_writer(return_data: dict):
        """返回一个 stub 工具,会把 return_data 写到 args.output_json。"""
        def _stub(args, ctx):
            output_json = args.get("output_json")
            if output_json:
                Path(output_json).parent.mkdir(parents=True, exist_ok=True)
                Path(output_json).write_text(json.dumps(return_data), encoding="utf-8")
            return ToolResult(
                text=f"stub {id(_stub)}",
                data=return_data,
                artifacts=[output_json] if output_json else [],
            )
        return _stub

    # 准备 fake media_reports(给 discover 节点返回 1 段整段占位)
    media_data = [
        {
            "source": base_state["video_input_path"],
            "probe": {"duration_seconds": 10.0, "size_bytes": 1024, "summary": {"duration_seconds": 10.0}},
            "analysis": {
                "input_path": base_state["video_input_path"],
                "duration_seconds": 10.0,
                "scene_change_count": 0,
                "candidate_segment_count": 1,
                "candidate_segments": [
                    {"index": 0, "start": 0.0, "end": 10.0, "duration": 10.0, "source": "placeholder_full"}
                ],
            },
            "probe_text": "Inspected media",
            "analysis_text": "ok",
        }
    ]

    transcript_data = {
        "language": "zh",
        "segments": [{"start": 0, "end": 5, "text": "你好"}],
        "words": [],
        "provider": "stub",
        "source_media": base_state["video_input_path"],
    }
    ingest_data = {
        "tool": "video_ingest",
        "video_path": base_state["video_input_path"],
        "media": {"sampled_frames": 4, "duration": 10.0},
        "sheet_paths": [],
        "frame_paths": [],
        "transcript_text": "",
    }
    timeline_data = {
        "version": "stub-1",
        "project": {"name": "stub"},
        "sequence": {
            "fps": 30,
            "duration": 10.0,
            "canvas": {"width": 1080, "height": 1920, "fps": 30},
            "output_canvas": {"width": 1080, "height": 1920, "fps": 30},
        },
        "assets": [
            {
                "id": "a1",
                "path": base_state["video_input_path"],
                "source": base_state["video_input_path"],
                "type": "video",
                "duration": 10.0,
                "width": 1080,
                "height": 1920,
                "fps": 30,
            }
        ],
        "tracks": [
            {
                "id": "t_video_main",
                "name": "video_main",
                "type": "video",
                "track_type": "video",
                "enabled": True,
                "visible": True,
                "order": 0,
                "clips": [
                    {
                        "id": "clip_0000",
                        "track_type": "video",
                        "asset_id": "a1",
                        "source": base_state["video_input_path"],
                        "start": 0.0,
                        "end": 10.0,
                        "duration": 10.0,
                        "timeline_start": 0.0,
                        "timeline_end": 10.0,
                        "speed": 1.0,
                        "volume": 1.0,
                        "opacity": 1.0,
                        "enabled": True,
                        "reason": "stub segment",
                        "candidate_index": 0,
                    }
                ],
            }
        ],
        "markers": [],
        "metadata": {"stub": True},
    }
    validation_data = {
        "status": "pass",
        "clip_count": 1,
        "project_contract": {"ok": True, "missing": []},
        "issues": [],
    }
    qc_data = {
        "status": "pass",
        "video_path": "/tmp/preview.mp4",
        "video_sha256": "stub-sha",
        "has_video_stream": True,
        "has_audio_stream": False,
        "media": {"duration_seconds": 10.0},
        "issues": [],
        "elapsed_seconds": 0.1,
    }
    timeline_diff_data = {
        "timeline_path": "/tmp/timeline.json",
        "applied": True,
        "after_validation_status": "pass",
        "after_validation_issues": [],
        "before_clip_count": 1,
        "after_clip_count": 1,
    }

    # patch 源模块
    from assembly_capabilities import (
        media_probe,
        qc_preview as qc_preview_mod,
        render_preview as render_preview_mod,
        speech_asr,
        timeline_ops,
        visual_observe,
    )

    monkeypatch.setattr(
        media_probe, "inspect_media",
        _make_stub_writer({
            "input_path": base_state["video_input_path"],
            "duration_seconds": 10.0,
            "summary": {"duration_seconds": 10.0},
        }),
    )
    monkeypatch.setattr(media_probe, "analyze_media", _make_stub_writer(media_data[0]["analysis"]))
    monkeypatch.setattr(speech_asr, "speech_transcribe", _make_stub_writer(transcript_data))
    monkeypatch.setattr(visual_observe, "video_ingest", _make_stub_writer(ingest_data))
    monkeypatch.setattr(
        timeline_ops, "validate_timeline", _make_stub_writer(validation_data)
    )
    monkeypatch.setattr(
        timeline_ops, "timeline_diff", _make_stub_writer(timeline_diff_data)
    )

    def _fake_render(args, ctx):
        output_path = args.get("output_path")
        report_path = Path(output_path).with_suffix(".render_report.json")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"\x00" * 1024)
        report = {
            "status": "pass",
            "timeline_path": args.get("timeline_path"),
            "timeline_sha256": "stub-sha",
            "output_path": output_path,
            "output_sha256": "stub-sha",
            "segment_count": 1,
        }
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return ToolResult(
            text=f"stub rendered: {output_path}",
            data=report,
            artifacts=[output_path, str(report_path)],
            video_paths=[output_path],
        )

    monkeypatch.setattr(render_preview_mod, "render_preview", _fake_render)
    monkeypatch.setattr(qc_preview_mod, "qc_preview", _make_stub_writer(qc_data))

    # patch 消费端(节点模块命名空间已 import 过的绑定)
    from nodes.assembly import (
        node_asr_and_visual_observe,
        node_discover_and_probe,
        node_repair_loop,
        node_validate_render_qc,
    )
    monkeypatch.setattr(node_discover_and_probe, "inspect_media", media_probe.inspect_media)
    monkeypatch.setattr(node_discover_and_probe, "analyze_media", media_probe.analyze_media)
    monkeypatch.setattr(node_asr_and_visual_observe, "speech_transcribe", speech_asr.speech_transcribe)
    monkeypatch.setattr(node_asr_and_visual_observe, "video_ingest", visual_observe.video_ingest)
    monkeypatch.setattr(node_validate_render_qc, "validate_timeline", timeline_ops.validate_timeline)
    monkeypatch.setattr(node_validate_render_qc, "render_preview", _fake_render)
    monkeypatch.setattr(node_validate_render_qc, "qc_preview", qc_preview_mod.qc_preview)
    monkeypatch.setattr(node_repair_loop, "timeline_diff", timeline_ops.timeline_diff)


def _edges_from(graph, node: str) -> set[str]:
    """返回 ``node`` 出边的目标节点集合。

    LangGraph 1.x 把边存为 ``set[tuple[src, tgt]]``(StateGraph.edges);
    条件边在 compiled graph 上才展开;单元测试只看基础边集合。
    """
    out: set[str] = set()
    for edge in graph.edges:
        if isinstance(edge, tuple) and len(edge) >= 2 and edge[0] == node:
            tgt = edge[1]
            if isinstance(tgt, str):
                out.add(tgt)
    return out


# ---------------------------------------------------------------------------
# 测试 1:默认 True 时图能 compile
# ---------------------------------------------------------------------------
def test_graph_compiles_with_assembly_qc_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    g = _build_state_graph().compile(checkpointer=InMemorySaver())
    assert g is not None


# ---------------------------------------------------------------------------
# 测试 2:ASSEMBLY_QC_GATE_ENABLED=False 时图仍 compile
# ---------------------------------------------------------------------------
def test_graph_compiles_with_assembly_qc_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", False)
    g = _build_state_graph().compile(checkpointer=InMemorySaver())
    assert g is not None


# ---------------------------------------------------------------------------
# 测试 3:False 时 generate_draft → node_06_human_reorder 直连(无 assembly 节点)
# ---------------------------------------------------------------------------
def test_qc_gate_false_skips_assembly_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", False)
    g = _build_state_graph()
    out = _edges_from(g, "generate_draft")
    # 应急关闭:generate_draft 直接到 node_06_human_reorder
    assert "node_06_human_reorder" in out
    # 不应该有 assembly 节点的边
    for edge_target in out:
        assert not edge_target.startswith("assembly_"), (
            f"应急关闭时不应有 assembly 出边,实际:{edge_target}"
        )


# ---------------------------------------------------------------------------
# 测试 4:True 时 6 节点链路完整
# ---------------------------------------------------------------------------
def test_qc_gate_true_wires_full_assembly_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    g = _build_state_graph()
    # generate_draft → assembly_discover_and_probe
    assert "assembly_discover_and_probe" in _edges_from(g, "generate_draft")
    # 6 节点串联 + 条件路由 + repair_loop 回到 validate
    assert "assembly_asr_and_visual_observe" in _edges_from(g, "assembly_discover_and_probe")
    assert "assembly_build_timeline" in _edges_from(g, "assembly_asr_and_visual_observe")
    assert "assembly_validate_render_qc" in _edges_from(g, "assembly_build_timeline")
    # assembly_write_report → node_06_human_reorder(回到主流程)
    assert "node_06_human_reorder" in _edges_from(g, "assembly_write_report")
    # repair_loop → assembly_validate_render_qc(回到 7.4 重新走一遍)
    assert "assembly_validate_render_qc" in _edges_from(g, "assembly_repair_loop")
    # 验证条件路由存在:assembly_validate_render_qc 有两条 outgoing(targets)
    # (assembly_repair_loop / assembly_write_report)。
    from graph import route_after_assembly_qc
    state_pass = {"assembly_qc_status": "pass", "assembly_qc_retry_count": 0}
    state_escalated_low = {
        "assembly_qc_status": "escalated",
        "assembly_qc_retry_count": 0,
    }
    assert route_after_assembly_qc(state_pass) == "assembly_write_report"
    assert route_after_assembly_qc(state_escalated_low) == "assembly_repair_loop"


# ---------------------------------------------------------------------------
# 测试 5:端到端跑一遍,验证 8 个产物文件全部生成
# ---------------------------------------------------------------------------
def test_assembly_chain_end_to_end_produces_all_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    base_state: dict,
) -> None:
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    monkeypatch.setattr(config, "ASSEMBLY_QC_MAX_RETRY", 1)
    g = _build_state_graph().compile(
        checkpointer=InMemorySaver(),
        interrupt_before=["node_06_human_reorder"],
    )

    config_dict = {"configurable": {"thread_id": "assembly-e2e"}}
    # 从 generate_draft 之前跑会被前面 4 个节点阻塞(interrupt + 心跳等),
    # 这里直接 invoke,让 LangGraph 自然推进;interrupt_before 在到达 node_06 之前停下。
    out = g.invoke(base_state, config=config_dict)

    assembly_dir = Path(base_state["storyline_outputs_root"]) / "assembly"
    expected_files = [
        "media.json",
        "transcript.json",
        "video_ingest.json",
        "timeline.json",
        "timeline_validation.json",
        "preview.mp4",
        "preview_qc_report.json",
        "report.md",
    ]
    missing: list[str] = []
    for name in expected_files:
        p = assembly_dir / name
        if not p.is_file():
            missing.append(name)
    assert not missing, f"缺失产物文件:{missing},assembly_dir={assembly_dir},exists={assembly_dir.is_dir()}"

    # 关键 state 字段
    assert out.get("assembly_qc_status") == "pass"
    assert out.get("assembly_timeline_path")
    assert out.get("assembly_preview_path")
    assert out.get("assembly_qc_report_path")
    assert out.get("assembly_report_path")
    # report.md 含"建议对照剪映草稿一并查看"
    report_md = Path(out["assembly_report_path"]).read_text(encoding="utf-8")
    assert "建议对照剪映草稿一并查看" in report_md


# ---------------------------------------------------------------------------
# 测试 6:QC 阻断 + repair_loop 软降级 → 仍到 node_06_human_reorder
# ---------------------------------------------------------------------------
def test_assembly_chain_qc_blocking_then_repair_soft_degrade(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    base_state: dict,
    tmp_path: Path,
) -> None:
    """QC 阻断项触发 → repair_loop 重试 → 仍 soft-fail → node_06 继续。

    验证 plan §七 7.5 + ADR-3 软降级行为:
    - 重试到 ASSEMBLY_QC_MAX_RETRY 上限后,不再阻断流水线
    - assembly_qc_status 保持 "escalated",所有问题如实写进 report.md
    - 流程最终仍到达 node_06_human_reorder
    """
    from assembly_capabilities import qc_preview as qc_preview_mod
    from assembly_capabilities.result import ToolResult
    from nodes.assembly import node_validate_render_qc

    def _qc_blocking(args, ctx):
        output_json = args.get("output_json")
        report = {
            "status": "fail",
            "video_path": args.get("video_path"),
            "has_video_stream": True,
            "media": {"duration_seconds": 10.0},
            "issues": [
                {"severity": "error", "message": "fake blocking issue for repair_loop"},
            ],
            "elapsed_seconds": 0.1,
        }
        if output_json:
            Path(output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(output_json).write_text(json.dumps(report), encoding="utf-8")
        return ToolResult(text="QC fail", data=report, artifacts=[output_json] if output_json else [])

    monkeypatch.setattr(qc_preview_mod, "qc_preview", _qc_blocking)
    monkeypatch.setattr(node_validate_render_qc, "qc_preview", _qc_blocking)
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    monkeypatch.setattr(config, "ASSEMBLY_QC_MAX_RETRY", 1)

    g = _build_state_graph().compile(
        checkpointer=InMemorySaver(),
        interrupt_before=["node_06_human_reorder"],
    )
    config_dict = {"configurable": {"thread_id": "assembly-qc-block"}}
    out = g.invoke(base_state, config=config_dict)

    # 软降级:流程仍到了 node_06_human_reorder(状态被 interrupt_before 截停)
    assert out.get("assembly_qc_status") == "escalated"
    assert int(out.get("assembly_qc_retry_count") or 0) >= 1
    # report.md 仍生成且含阻断项说明
    assert out.get("assembly_report_path")
    report_md = Path(out["assembly_report_path"]).read_text(encoding="utf-8")
    assert "fake blocking issue" in report_md
    # 状态 log 含 repair_loop_done(证明确实跑了修复循环)
    assert any("assembly_repair_loop_done" in tag for tag in out.get("status_log", []))