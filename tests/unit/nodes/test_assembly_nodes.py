"""``nodes/assembly/`` 6 个节点的单元测试(mock 掉 ``assembly_capabilities`` 层)。

覆盖:
- 入参缺失 → CONTRACT_INVALID 错误
- 正常路径 → 写产物、写 state 字段、append_status_tag
- repair_loop + validate_render_qc 的路由函数 ``route_after_assembly_qc``
- report.md 内容基本形态
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.assembly import (
    assembly_asr_and_visual_observe_node,
    assembly_build_timeline_node,
    assembly_discover_and_probe_node,
    assembly_repair_loop_node,
    assembly_validate_render_qc_node,
    assembly_write_report_node,
    route_after_assembly_qc,
)

# pytest fixtures (base_state / mock_assembly_capabilities / mock_qc_blocking) are
# auto-injected by name; helper functions need explicit import.
from .conftest import make_media_reports, make_timeline  # noqa: F401


# =============================================================================
# assembly_discover_and_probe
# =============================================================================
def test_discover_requires_video_input_path(base_state):
    base_state.pop("video_input_path")
    out = assembly_discover_and_probe_node(base_state)
    assert any("CONTRACT_INVALID" in e and "video_input_path" in e for e in out["error_log"])
    assert "assembly_discover_failed" in out["status_log"]
    assert "assembly_media_artifact" not in out


def test_discover_handles_single_file(mock_assembly_capabilities, base_state, tmp_path):
    out = assembly_discover_and_probe_node(base_state)
    assert "assembly_media_artifact" in out
    assert "assembly_discover_done" in out["status_log"]
    p = Path(out["assembly_media_artifact"])
    assert p.is_file()
    payload = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["source"] == base_state["video_input_path"]


def test_discover_handles_directory(mock_assembly_capabilities, base_state, tmp_path):
    # 准备子目录,内放 2 个视频文件(避免 tmp_path 本身下的 input.mp4 被扫到)
    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    (video_dir / "a.mp4").write_bytes(b"\x00" * 1024)
    (video_dir / "b.mp4").write_bytes(b"\x00" * 1024)
    base_state["video_input_path"] = str(video_dir)
    out = assembly_discover_and_probe_node(base_state)
    assert "assembly_media_artifact" in out
    payload = json.loads(Path(out["assembly_media_artifact"]).read_text(encoding="utf-8"))
    assert len(payload) == 2


def test_discover_handles_directory_no_videos(mock_assembly_capabilities, base_state, tmp_path):
    base_state["video_input_path"] = str(tmp_path / "empty_dir")
    (tmp_path / "empty_dir").mkdir()
    (tmp_path / "empty_dir" / "readme.txt").write_text("not a video")
    out = assembly_discover_and_probe_node(base_state)
    assert any("no source video found" in e for e in out["error_log"])
    assert "assembly_discover_failed" in out["status_log"]


# =============================================================================
# assembly_asr_and_visual_observe
# =============================================================================
def test_asr_observe_requires_video_input_path(base_state):
    base_state.pop("video_input_path")
    out = assembly_asr_and_visual_observe_node(base_state)
    assert any("CONTRACT_INVALID" in e and "video_input_path" in e for e in out["error_log"])


def test_asr_observe_requires_media_artifact(mock_assembly_capabilities, base_state):
    # media_artifact 缺失
    out = assembly_asr_and_visual_observe_node(base_state)
    assert any("missing assembly_media_artifact" in e for e in out["error_log"])


def test_asr_observe_happy_path(mock_assembly_capabilities, base_state, tmp_path):
    # 直接构造 media_artifact 路径(假装 discover 已跑过)
    artifact = tmp_path / "media.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(make_media_reports(str(base_state["video_input_path"]), [])), encoding="utf-8")
    base_state["assembly_media_artifact"] = str(artifact)

    out = assembly_asr_and_visual_observe_node(base_state)
    assert "assembly_transcript_artifact" in out
    assert "assembly_ingest_artifact" in out
    assert "assembly_asr_observe_done" in out["status_log"]
    # transcript 文件应被 fake_speech_transcribe_success 写出
    assert Path(out["assembly_transcript_artifact"]).is_file()
    assert Path(out["assembly_ingest_artifact"]).is_file()


# =============================================================================
# assembly_build_timeline
# =============================================================================
def test_build_timeline_requires_media_artifact(base_state):
    out = assembly_build_timeline_node(base_state)
    assert any("missing assembly_media_artifact" in e for e in out["error_log"])


def test_build_timeline_writes_timeline_via_fallback(mock_assembly_capabilities, base_state, tmp_path):
    """StubLLMClient 默认空 → 走确定性 fallback,仍产出合法 timeline。

    阶段三默认 LLM client 是 StubLLMClient(默认无 default_json),
    它按 schema 生成空 stub ``{"selected_segments": []}``,被解析为"LLM 返回了
    选段数组但全是空" → ``_normalize_selection`` 返回空 chosen → 走
    ``_fallback_chosen`` 兜底,产出 2 段占位 timeline。
    """
    artifact = tmp_path / "media.json"
    artifact.write_text(json.dumps(make_media_reports(
        base_state["video_input_path"],
        [
            {"index": 0, "start": 0.0, "end": 2.0, "duration": 2.0, "source": "scene_boundary"},
            {"index": 1, "start": 3.0, "end": 5.0, "duration": 2.0, "source": "scene_boundary"},
        ],
    )), encoding="utf-8")
    base_state["assembly_media_artifact"] = str(artifact)

    out = assembly_build_timeline_node(base_state)
    assert "assembly_timeline_path" in out
    p = Path(out["assembly_timeline_path"])
    assert p.is_file()
    timeline = json.loads(p.read_text(encoding="utf-8"))
    # 阶段三新契约:project.name == "assembly"(LLM 主导)
    assert timeline["project"]["name"] == "assembly"
    # StubLLMClient 按 schema 生成空 stub → LLM 调通但选段为空 →
    # 走 fallback 算法兜底
    assert timeline["project"]["llm_used"] is True
    assert timeline["project"]["fallback_used"] is True
    assert "tracks" in timeline
    assert len(timeline["tracks"][0]["clips"]) == 2
    # 关键契约:每个 clip 必须有 reason,否则 validate_timeline 会报错
    for clip in timeline["tracks"][0]["clips"]:
        assert "reason" in clip
        assert "source" in clip
        assert "start" in clip
        assert "end" in clip
    # 关键字段:每个 clip 必须有 timeline_start/timeline_end(plan §7.3)
    for clip in timeline["tracks"][0]["clips"]:
        assert "timeline_start" in clip
        assert "timeline_end" in clip


def test_build_timeline_uses_llm_selection_when_provided(mock_assembly_capabilities, base_state, tmp_path):
    """阶段三:注入 LLM client 给"selected_segments",看是否优先使用 LLM 输出。"""
    from storyline_capabilities.vlm_client import StubLLMClient

    # StubLLMClient(default_json=...) 会按 schema 生成 stub;
    # 这里显式注入一个"只输出指定 selected_segments"的 client。
    llm_payload = {
        "selected_segments": [
            {"index": 0, "reason": "hook 镜头", "beat": "hook", "order": 0},
            {"index": 1, "reason": "core 镜头", "beat": "core", "order": 1},
        ],
        "editorial_structure": "hook -> core",
        "task_assumption": "默认竖屏",
        "selected_strategy": "只保留前 2 段",
        "canvas": {"width": 1080, "height": 1920, "fps": 30, "platform": "vertical", "aspect_ratio": "9:16"},
    }
    client = StubLLMClient(default_json=llm_payload)

    artifact = tmp_path / "media.json"
    artifact.write_text(json.dumps(make_media_reports(
        base_state["video_input_path"],
        [
            {"index": 0, "start": 0.0, "end": 2.0, "duration": 2.0, "source": "scene_boundary"},
            {"index": 1, "start": 3.0, "end": 5.0, "duration": 2.0, "source": "scene_boundary"},
            {"index": 2, "start": 6.0, "end": 8.0, "duration": 2.0, "source": "scene_boundary"},
        ],
    )), encoding="utf-8")
    base_state["assembly_media_artifact"] = str(artifact)

    # 直接调能力函数(节点用 None 默认 client)
    from assembly_capabilities.build_timeline import build_timeline_from_paths

    timeline = build_timeline_from_paths(
        media_artifact=str(artifact),
        transcript_artifact=None,
        ingest_artifact=None,
        lang="zh",
        client=client,
    )
    # LLM 输出 2 段 → 实际选 2 段
    assert timeline["project"]["llm_used"] is True
    assert timeline["project"]["fallback_used"] is False
    assert timeline["project"]["selected_segments_count"] == 2
    assert len(timeline["tracks"][0]["clips"]) == 2
    # canvas 应该是 LLM 指定的 9:16 竖屏
    assert timeline["sequence"]["canvas"]["width"] == 1080
    assert timeline["sequence"]["canvas"]["height"] == 1920
    # 第 0 段 reason 应是 LLM 注入的
    first_clip = timeline["tracks"][0]["clips"][0]
    assert first_clip["reason"] == "hook 镜头"
    assert first_clip.get("beat") == "hook"


def test_build_timeline_handles_empty_segments(mock_assembly_capabilities, base_state, tmp_path):
    """无 candidate_segments 时回退为整段,仍然产出合法 timeline。"""
    artifact = tmp_path / "media.json"
    artifact.write_text(json.dumps(make_media_reports(
        base_state["video_input_path"],
        [],  # 空
    )), encoding="utf-8")
    base_state["assembly_media_artifact"] = str(artifact)

    out = assembly_build_timeline_node(base_state)
    # 应有产物
    assert "assembly_timeline_path" in out
    timeline = json.loads(Path(out["assembly_timeline_path"]).read_text(encoding="utf-8"))
    assert "tracks" in timeline
    # probe.duration_seconds = 10.0 → 兜底产出 1 段整段
    assert len(timeline["tracks"][0]["clips"]) >= 1


def test_build_timeline_records_metadata(mock_assembly_capabilities, base_state, tmp_path):
    """阶段三 metadata 字段(plan §7.6 + report.md 渲染需要)。"""
    artifact = tmp_path / "media.json"
    artifact.write_text(json.dumps(make_media_reports(
        base_state["video_input_path"],
        [{"index": 0, "start": 0.0, "end": 2.0, "duration": 2.0, "source": "scene_boundary"}],
    )), encoding="utf-8")
    base_state["assembly_media_artifact"] = str(artifact)

    out = assembly_build_timeline_node(base_state)
    timeline = json.loads(Path(out["assembly_timeline_path"]).read_text(encoding="utf-8"))
    md = timeline["metadata"]
    assert md["assembly_build_timeline"] is True
    assert "candidate_segments_count" in md
    assert "selected_segments_count" in md
    assert "transcript_chars" in md
    assert "sheet_count" in md
    # project 也有 metadata 的镜像字段,方便 read_report 渲染
    proj = timeline["project"]
    assert "selected_segments_count" in proj
    assert "candidate_segments_count" in proj
    assert "elapsed_seconds" in proj


def test_build_timeline_rejects_corrupt_media_json(base_state, tmp_path):
    artifact = tmp_path / "media.json"
    artifact.write_text("not json", encoding="utf-8")
    base_state["assembly_media_artifact"] = str(artifact)

    out = assembly_build_timeline_node(base_state)
    assert any("CONTRACT_INVALID" in e and "media.json" in e for e in out["error_log"])


# =============================================================================
# assembly_validate_render_qc
# =============================================================================
def test_validate_render_qc_requires_timeline(base_state):
    out = assembly_validate_render_qc_node(base_state)
    assert any("missing assembly_timeline_path" in e for e in out["error_log"])


def test_validate_render_qc_happy_path(mock_assembly_capabilities, base_state, tmp_path):
    # 准备一个合法 timeline(直接构造,跳过 build_timeline)
    timeline = make_timeline(
        [
            {"track_type": "video", "source": str(tmp_path / "input.mp4"),
             "start": 0.0, "end": 2.0, "duration": 2.0, "reason": "test1"},
        ],
        str(tmp_path / "input.mp4"),
    )
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps(timeline), encoding="utf-8")
    base_state["assembly_timeline_path"] = str(timeline_path)

    out = assembly_validate_render_qc_node(base_state)
    assert out["assembly_qc_status"] == "pass"
    assert Path(out["assembly_timeline_validation_path"]).is_file()
    assert Path(out["assembly_preview_path"]).is_file()
    assert Path(out["assembly_qc_report_path"]).is_file()


def test_validate_render_qc_escalated_when_blocking(mock_assembly_capabilities, mock_qc_blocking, base_state, tmp_path):
    timeline = make_timeline(
        [
            {"track_type": "video", "source": str(tmp_path / "input.mp4"),
             "start": 0.0, "end": 2.0, "duration": 2.0, "reason": "test1"},
        ],
        str(tmp_path / "input.mp4"),
    )
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps(timeline), encoding="utf-8")
    base_state["assembly_timeline_path"] = str(timeline_path)

    out = assembly_validate_render_qc_node(base_state)
    assert out["assembly_qc_status"] == "escalated"


def test_validate_render_qc_pass_with_warnings(mock_assembly_capabilities, monkeypatch, base_state, tmp_path):
    from assembly_capabilities import qc_preview as qc_module
    from nodes.assembly import node_validate_render_qc

    def fake_qc_warnings(args, ctx):
        out = tmp_path / "qc.json"
        report = {
            "status": "pass",
            "video_path": args.get("video_path"),
            "issues": [{"severity": "warning", "message": "minor noise"}],
            "media": {"duration_seconds": 5.0},
            "elapsed_seconds": 0.1,
        }
        out.write_text(json.dumps(report), encoding="utf-8")
        from assembly_capabilities.result import ToolResult
        return ToolResult(text="QC pass", data=report, artifacts=[str(out)])

    monkeypatch.setattr(qc_module, "qc_preview", fake_qc_warnings)
    # 节点模块顶部已 import 的绑定也要 patch(节点调的是它自己命名空间里的 qc_preview)
    monkeypatch.setattr(node_validate_render_qc, "qc_preview", fake_qc_warnings)

    timeline = make_timeline(
        [{"track_type": "video", "source": str(tmp_path / "input.mp4"),
          "start": 0.0, "end": 2.0, "duration": 2.0, "reason": "t"}],
        str(tmp_path / "input.mp4"),
    )
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps(timeline), encoding="utf-8")
    base_state["assembly_timeline_path"] = str(timeline_path)

    out = assembly_validate_render_qc_node(base_state)
    assert out["assembly_qc_status"] == "pass_with_warnings"


# =============================================================================
# route_after_assembly_qc
# =============================================================================
def test_route_pass_goes_to_write_report(base_state):
    base_state["assembly_qc_status"] = "pass"
    base_state["assembly_qc_retry_count"] = 0
    assert route_after_assembly_qc(base_state) == "assembly_write_report"


def test_route_pass_with_warnings_goes_to_write_report(base_state):
    base_state["assembly_qc_status"] = "pass_with_warnings"
    base_state["assembly_qc_retry_count"] = 0
    assert route_after_assembly_qc(base_state) == "assembly_write_report"


def test_route_escalated_with_low_retry_goes_to_repair(base_state, monkeypatch):
    import config
    monkeypatch.setattr(config, "ASSEMBLY_QC_MAX_RETRY", 2)
    base_state["assembly_qc_status"] = "escalated"
    base_state["assembly_qc_retry_count"] = 0
    assert route_after_assembly_qc(base_state) == "assembly_repair_loop"


def test_route_escalated_at_max_retry_goes_to_write_report(base_state, monkeypatch):
    import config
    monkeypatch.setattr(config, "ASSEMBLY_QC_MAX_RETRY", 2)
    base_state["assembly_qc_status"] = "escalated"
    base_state["assembly_qc_retry_count"] = 2
    assert route_after_assembly_qc(base_state) == "assembly_write_report"


def test_route_none_status_treated_as_repair(base_state, monkeypatch):
    import config
    monkeypatch.setattr(config, "ASSEMBLY_QC_MAX_RETRY", 2)
    base_state["assembly_qc_status"] = None
    base_state["assembly_qc_retry_count"] = 0
    # None 不是 pass/warnings,retry 0 < MAX → repair
    assert route_after_assembly_qc(base_state) == "assembly_repair_loop"


# =============================================================================
# assembly_repair_loop
# =============================================================================
def test_repair_loop_requires_timeline(base_state):
    out = assembly_repair_loop_node(base_state)
    assert any("missing assembly_timeline_path" in e for e in out["error_log"])


def test_repair_loop_writes_marker_and_increments_retry(mock_assembly_capabilities, base_state, tmp_path):
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps({"clips": []}), encoding="utf-8")
    base_state["assembly_timeline_path"] = str(timeline_path)

    out = assembly_repair_loop_node(base_state)
    assert out["assembly_qc_retry_count"] == 1
    assert "assembly_repair_loop_done" in out["status_log"]
    # mock 的 timeline_diff 会把 timeline 写成 {"metadata": {"repair_marker": True}}
    written = json.loads(timeline_path.read_text(encoding="utf-8"))
    assert written["metadata"]["repair_marker"] is True


def test_repair_loop_increments_retry_count(mock_assembly_capabilities, base_state, tmp_path):
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps({"clips": []}), encoding="utf-8")
    base_state["assembly_timeline_path"] = str(timeline_path)
    base_state["assembly_qc_retry_count"] = 2

    out = assembly_repair_loop_node(base_state)
    assert out["assembly_qc_retry_count"] == 3


# =============================================================================
# assembly_write_report
# =============================================================================
def test_write_report_writes_markdown_with_sections(mock_assembly_capabilities, base_state, tmp_path):
    # 准备所有上游产物
    media_path = tmp_path / "media.json"
    media_path.write_text(json.dumps(make_media_reports(
        base_state["video_input_path"],
        [{"index": 0, "start": 0.0, "end": 2.0, "duration": 2.0, "source": "scene_boundary"}],
    )), encoding="utf-8")

    validation_path = tmp_path / "validation.json"
    validation_path.write_text(json.dumps({
        "status": "pass", "clip_count": 1, "issues": []
    }), encoding="utf-8")

    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps(make_timeline(
        [{"track_type": "video", "source": base_state["video_input_path"], "start": 0.0, "end": 2.0, "reason": "r"}],
        base_state["video_input_path"],
    )), encoding="utf-8")

    qc_path = tmp_path / "qc.json"
    qc_path.write_text(json.dumps({
        "status": "pass",
        "issues": [
            {"severity": "warning", "message": "minor warning"},
        ],
    }), encoding="utf-8")

    base_state["assembly_media_artifact"] = str(media_path)
    base_state["assembly_timeline_validation_path"] = str(validation_path)
    base_state["assembly_timeline_path"] = str(timeline_path)
    base_state["assembly_qc_report_path"] = str(qc_path)
    base_state["assembly_preview_path"] = str(tmp_path / "preview.mp4")
    base_state["assembly_qc_status"] = "pass_with_warnings"
    base_state["assembly_qc_retry_count"] = 0

    out = assembly_write_report_node(base_state)
    assert "assembly_report_path" in out
    p = Path(out["assembly_report_path"])
    assert p.is_file()
    content = p.read_text(encoding="utf-8")
    # 必须有这些 section
    assert "# Assembly QC 报告" in content
    assert "## 1. 总览" in content
    assert "## 2. 素材清单" in content
    assert "## 3. timeline 校验" in content
    assert "## 4. timeline 内容" in content
    assert "## 5. 质检报告" in content
    assert "pass_with_warnings" in content
    assert "minor warning" in content
    assert "建议对照剪映草稿一并查看" in content


def test_write_report_handles_missing_artifacts_gracefully(base_state, tmp_path):
    """所有上游缺失也不应崩 —— 降级写空 section。"""
    out = assembly_write_report_node(base_state)
    p = Path(out["assembly_report_path"])
    assert p.is_file()
    content = p.read_text(encoding="utf-8")
    assert "Assembly QC 报告" in content
    assert "(无素材清单)" in content
    assert "(无 validation 报告)" in content
    assert "(无 QC 报告)" in content
    assert "(timeline.json 未生成)" in content


def test_write_report_handles_corrupt_artifacts_gracefully(base_state, tmp_path):
    """破损 JSON 不应阻断 report 节点。"""
    media_path = tmp_path / "media.json"
    media_path.write_text("not json", encoding="utf-8")
    base_state["assembly_media_artifact"] = str(media_path)

    out = assembly_write_report_node(base_state)
    assert "assembly_report_path" in out
    assert Path(out["assembly_report_path"]).is_file()
