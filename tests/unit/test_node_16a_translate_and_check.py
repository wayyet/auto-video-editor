"""node_16a_translate_and_check 单测 — Week 5 新节点。

覆盖:
- marker 已存在 → 跳过翻译,只写 state
- marker 不存在 → 调翻译 + 写 marker + 写 state
- asr_segments_zh 缺失 → 跳过(error_log)
- draft_dir_en_branch 缺失 → 跳过(error_log)
- layout 校验写 state(不触发 interrupt)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jy_common.translate_client import MockTranslateClient, set_default_client
from nodes.node_16a_translate_and_check import node_16a_translate_and_check


def _seed_en_branch(tmp_path: Path) -> Path:
    draft_dir = tmp_path / "en_branch"
    draft_dir.mkdir()
    draft = {
        "canvas_config": {},
        "materials": {
            "texts": [
                {"content": "你好", "target_timerange": {"start": 0, "duration": 2_000_000}},
                {"content": "世界", "target_timerange": {"start": 2_000_000, "duration": 2_000_000}},
            ]
        },
    }
    (draft_dir / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return draft_dir


def _state(asr_segs: list[dict], draft_dir: Path) -> dict:
    return {
        "asr_segments_zh": asr_segs,
        "draft_dir_en_branch": str(draft_dir),
        "status_log": [],
        "error_log": [],
    }


def _normal_asr() -> list[dict]:
    return [
        {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_zh": "你好"},
        {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_zh": "世界"},
    ]


def test_node_16a_happy_path_writes_state_and_srt(tmp_path: Path) -> None:
    """正常路径:翻译 + 写 marker + 写 state + 写 SRT(Week 5 调整:16a 也写 SRT)。"""
    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())
    state = _state(_normal_asr(), draft_dir)
    out = node_16a_translate_and_check(state)

    # state 字段应有值
    assert out.get("subtitle_segments_en")
    assert out.get("layout_issues") == [] or out.get("layout_issues") is None
    assert out.get("layout_issues_detected") is False
    assert "node_16a_translate_done" in out["status_log"]
    # 不应触发 interrupt(payload=None 不应存在)
    assert "interrupt" not in out
    # Week 5:正常路径下也写 SRT(原 node_16 行为)
    srt_path = out.get("subtitle_srt_path")
    assert srt_path
    assert Path(srt_path).exists()


def test_node_16a_marker_exists_skips_translate(tmp_path: Path) -> None:
    """marker 已存在 → 不调翻译客户端,直接读草稿。"""
    draft_dir = _seed_en_branch(tmp_path)
    marker_data = [
        {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_en": "PreExisting"},
        {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_en": "PreExisting2"},
    ]
    (draft_dir / "subtitle_en.json").write_text(
        json.dumps(marker_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    class _ShouldNotCall:
        def translate(self, segments):
            raise AssertionError("marker 存在时不应调翻译")

    set_default_client(_ShouldNotCall())
    state = _state(_normal_asr(), draft_dir)
    out = node_16a_translate_and_check(state)
    assert out.get("subtitle_segments_en")


def test_node_16a_no_asr_segments_skips(tmp_path: Path) -> None:
    """asr_segments_zh 缺失 → 跳过,error_log 有说明。"""
    state = {
        "asr_segments_zh": [],
        "draft_dir_en_branch": str(tmp_path),
        "status_log": [],
        "error_log": [],
    }
    out = node_16a_translate_and_check(state)
    assert "node_16a_translate_skipped" in out["status_log"]
    assert any("asr_segments_zh" in e for e in out["error_log"])


def test_node_16a_no_draft_dir_skips() -> None:
    """draft_dir_en_branch 缺失 → 跳过。"""
    state = {
        "asr_segments_zh": _normal_asr(),
        "draft_dir_en_branch": None,
        "status_log": [],
        "error_log": [],
    }
    out = node_16a_translate_and_check(state)
    assert "node_16a_translate_skipped" in out["status_log"]
    assert any("draft_dir_en_branch" in e for e in out["error_log"])


def test_node_16a_layout_issues_writes_state_without_interrupt(tmp_path: Path) -> None:
    """layout 异常时:写 layout_issues + layout_issues_detected=True,但不调 interrupt。"""
    draft_dir = _seed_en_branch(tmp_path)
    issues_to_inject = [{"index": 0, "reason": "text_too_wide", "width_px": 2000}]

    with patch(
        "nodes.node_16a_translate_and_check.validate_layout",
        return_value=issues_to_inject,
    ):
        set_default_client(MockTranslateClient())
        state = _state(_normal_asr(), draft_dir)
        out = node_16a_translate_and_check(state)

    assert out.get("layout_issues") == issues_to_inject
    assert out.get("layout_issues_detected") is True
    # 关键:不应有任何 interrupt 调用(纯函数侧不抛,LangGraph 层也不调)
    assert "node_16a_translate_done" in out["status_log"]


# ---------------------------------------------------------------------------
# 9 工具迁移 §6.4 step 2:qc 链集成测试
# ---------------------------------------------------------------------------
def test_node_16a_qc_chain_success_writes_preview_and_evidence(tmp_path: Path) -> None:
    """qc 链全部成功 → 写 ``preview_video_path`` + ``subtitle_qc_evidence_frames``
    + ``layout_issues_source='qc_chain'``。
    """
    from unittest.mock import patch

    from assembly_capabilities.result import ToolResult

    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())

    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"\x00")  # 文件存在即可,内容无关

    state = _state(_normal_asr(), draft_dir)
    state["video_input_path"] = str(fake_video)

    qc_issues = [{"index": 0, "severity": "warning", "message": "minor"}]
    qc_evidence_meta = [{"path": str(tmp_path / "frame_001.jpg"), "timestamp": 0.5}]

    def fake_build(args, ctx):
        sub = tmp_path / "subtitles.json"
        sub.write_text(json.dumps({"cues": [], "style": {}}), encoding="utf-8")
        return ToolResult(
            text="ok",
            data={"cue_count": 0},
            artifacts=[str(sub)],
        )

    def fake_render(args, ctx):
        return ToolResult(
            text="ok",
            data={"outputs": {"burned": str(tmp_path / "preview.mp4")}},
        )

    def fake_qc(args, ctx):
        return ToolResult(
            text="ok",
            data={"issues": qc_issues, "evidence_frames": qc_evidence_meta},
            image_paths=[str(tmp_path / "frame_001.jpg")],
        )

    with (
        patch("nodes.node_16a_translate_and_check.subtitle_build", side_effect=fake_build),
        patch("nodes.node_16a_translate_and_check.subtitle_render", side_effect=fake_render),
        patch("nodes.node_16a_translate_and_check.subtitle_qc", side_effect=fake_qc),
    ):
        out = node_16a_translate_and_check(state)

    # qc 链路径
    assert out["layout_issues_source"] == "qc_chain"
    assert out["layout_issues"] == qc_issues
    assert out["layout_issues_detected"] is True
    # 预览路径
    assert out["preview_video_path"] == str(tmp_path / "preview.mp4")
    # 证据帧(同时来自 data.evidence_frames 与 image_paths,合并去重)
    assert str(tmp_path / "frame_001.jpg") in out["subtitle_qc_evidence_frames"]


def test_node_16a_qc_chain_build_failure_falls_back_to_validate_layout(
    tmp_path: Path,
) -> None:
    """``subtitle_build`` 返回 ``[ERROR]`` → 降级为 ``validate_layout``,
    ``layout_issues_source='validate_layout'``,不写 preview 字段。
    """
    from unittest.mock import patch

    from assembly_capabilities.result import ToolResult

    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())

    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"\x00")

    state = _state(_normal_asr(), draft_dir)
    state["video_input_path"] = str(fake_video)

    def fake_build_fail(args, ctx):
        return ToolResult(text="[ERROR] video not found")

    issues_to_inject = [{"index": 0, "reason": "text_too_wide"}]
    with (
        patch("nodes.node_16a_translate_and_check.subtitle_build", side_effect=fake_build_fail),
        patch("nodes.node_16a_translate_and_check.validate_layout", return_value=issues_to_inject),
    ):
        out = node_16a_translate_and_check(state)

    # 降级路径
    assert out["layout_issues_source"] == "validate_layout"
    assert out["layout_issues"] == issues_to_inject
    # 不写 preview / evidence
    assert "preview_video_path" not in out
    assert "subtitle_qc_evidence_frames" not in out


def test_node_16a_qc_chain_render_failure_falls_back_to_validate_layout(
    tmp_path: Path,
) -> None:
    """``subtitle_render`` 失败 → 同样降级为 ``validate_layout``。"""
    from unittest.mock import patch

    from assembly_capabilities.result import ToolResult

    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())

    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"\x00")

    state = _state(_normal_asr(), draft_dir)
    state["video_input_path"] = str(fake_video)

    def fake_build_ok(args, ctx):
        sub = tmp_path / "subtitles.json"
        sub.write_text(json.dumps({"cues": [], "style": {}}), encoding="utf-8")
        return ToolResult(text="ok", data={}, artifacts=[str(sub)])

    def fake_render_fail(args, ctx):
        return ToolResult(text="[ERROR] subtitle burn-in failed")

    with (
        patch("nodes.node_16a_translate_and_check.subtitle_build", side_effect=fake_build_ok),
        patch("nodes.node_16a_translate_and_check.subtitle_render", side_effect=fake_render_fail),
        patch("nodes.node_16a_translate_and_check.validate_layout", return_value=[]),
    ):
        out = node_16a_translate_and_check(state)

    assert out["layout_issues_source"] == "validate_layout"
    assert out["layout_issues"] == []
    assert out["layout_issues_detected"] is False
    assert "preview_video_path" not in out


def test_node_16a_qc_chain_skipped_when_no_video_path(tmp_path: Path) -> None:
    """``video_input_path`` 缺失 → qc 链跳过,直接走 ``validate_layout``。"""
    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())
    state = _state(_normal_asr(), draft_dir)
    # state 中没有 video_input_path

    with patch(
        "nodes.node_16a_translate_and_check.validate_layout",
        return_value=[],
    ) as mock_validate:
        out = node_16a_translate_and_check(state)

    assert out["layout_issues_source"] == "validate_layout"
    mock_validate.assert_called_once()


def test_node_16a_qc_chain_skipped_when_video_path_does_not_exist(tmp_path: Path) -> None:
    """``video_input_path`` 不存在 → qc 链跳过,直接走 ``validate_layout``。"""
    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())
    state = _state(_normal_asr(), draft_dir)
    state["video_input_path"] = str(tmp_path / "nonexistent.mp4")

    with patch(
        "nodes.node_16a_translate_and_check.validate_layout",
        return_value=[],
    ):
        out = node_16a_translate_and_check(state)

    assert out["layout_issues_source"] == "validate_layout"


def test_node_16a_qc_chain_empty_transcript_falls_back(tmp_path: Path) -> None:
    """segments_en 全空 / 时间无效 → transcript 记录为空 → 走 validate_layout。"""
    from unittest.mock import patch

    from assembly_capabilities.result import ToolResult

    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())
    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"\x00")

    # 用只有 text_en 没有 start_ms 的 segments,触发 _segments_en_to_transcript_records 返回 []
    state = _state(_normal_asr(), draft_dir)
    state["video_input_path"] = str(fake_video)

    # 在 _check_layout_via_subtitle_qc 入口拦截:让 _segments_en_to_transcript_records 返回 []
    # 通过 mock segments_en 让 record 转换产出空列表
    def fake_build(*args, **kwargs):
        # 走到这里说明 _segments_en_to_transcript_records 没过滤空,应该会出错
        return ToolResult(text="[ERROR] should not reach here")

    with patch("nodes.node_16a_translate_and_check.subtitle_build", side_effect=fake_build):
        # marker 不存在会调翻译,翻译后 segments_en 是正常字段
        # 这里通过 mock 翻译客户端返回空 segments
        class _EmptyTranslator:
            def translate(self, segments):
                return []  # 空 list

        set_default_client(_EmptyTranslator())
        with patch("nodes.node_16a_translate_and_check.validate_layout", return_value=[]):
            out = node_16a_translate_and_check(state)

    # 空 segments → qc 链不跑(records 为空) → validate_layout 兜底
    assert out["layout_issues_source"] == "validate_layout"
    assert out["subtitle_segments_en"] == []
    assert "preview_video_path" not in out


def test_node_16a_qc_chain_qc_failure_returns_only_preview(tmp_path: Path) -> None:
    """``subtitle_qc`` 失败(但 build/render 都成功)→ 返回 ``(issues=None,
    preview=有, evidence=None)``;节点行为是 qc_issues is None → 走
    validate_layout 兜底(对照 _check_layout_via_subtitle_qc 注释)。

    注:当前实现把 qc 失败归到"qc 链失败"分支,与 build/render 失败一致。
    """
    from unittest.mock import patch

    from assembly_capabilities.result import ToolResult

    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())

    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"\x00")

    state = _state(_normal_asr(), draft_dir)
    state["video_input_path"] = str(fake_video)

    def fake_build(args, ctx):
        sub = tmp_path / "subtitles.json"
        sub.write_text(json.dumps({"cues": [], "style": {}}), encoding="utf-8")
        return ToolResult(text="ok", data={}, artifacts=[str(sub)])

    def fake_render(args, ctx):
        return ToolResult(
            text="ok",
            data={"outputs": {"burned": str(tmp_path / "preview.mp4")}},
        )

    def fake_qc_fail(args, ctx):
        return ToolResult(text="[ERROR] qc failed")

    with (
        patch("nodes.node_16a_translate_and_check.subtitle_build", side_effect=fake_build),
        patch("nodes.node_16a_translate_and_check.subtitle_render", side_effect=fake_render),
        patch("nodes.node_16a_translate_and_check.subtitle_qc", side_effect=fake_qc_fail),
        patch("nodes.node_16a_translate_and_check.validate_layout", return_value=[]),
    ):
        out = node_16a_translate_and_check(state)

    # qc 失败 → 降级 validate_layout
    assert out["layout_issues_source"] == "validate_layout"
    assert "preview_video_path" not in out
    assert "subtitle_qc_evidence_frames" not in out


def test_node_16a_qc_chain_layout_issues_field_contract_unchanged(tmp_path: Path) -> None:
    """关键契约(对照 9 工具迁移 §6.4 step 2):``layout_issues`` / ``layout_issues_detected``
    字段名与结构**完全不变**,qc 链产出与旧 validate_layout 同构(都是 list[dict])。
    关卡③ 现有逻辑零改动。
    """
    from unittest.mock import patch

    from assembly_capabilities.result import ToolResult

    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())

    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"\x00")
    state = _state(_normal_asr(), draft_dir)
    state["video_input_path"] = str(fake_video)

    qc_issues = [
        {"index": 0, "severity": "warning", "message": "w1"},
        {"index": 1, "severity": "error", "message": "e1"},
    ]

    def fake_build(args, ctx):
        sub = tmp_path / "subtitles.json"
        sub.write_text(json.dumps({"cues": [], "style": {}}), encoding="utf-8")
        return ToolResult(text="ok", data={}, artifacts=[str(sub)])

    def fake_render(args, ctx):
        return ToolResult(
            text="ok",
            data={"outputs": {"burned": str(tmp_path / "preview.mp4")}},
        )

    def fake_qc(args, ctx):
        return ToolResult(text="ok", data={"issues": qc_issues, "evidence_frames": []})

    with (
        patch("nodes.node_16a_translate_and_check.subtitle_build", side_effect=fake_build),
        patch("nodes.node_16a_translate_and_check.subtitle_render", side_effect=fake_render),
        patch("nodes.node_16a_translate_and_check.subtitle_qc", side_effect=fake_qc),
    ):
        out = node_16a_translate_and_check(state)

    # 字段名 / 类型与 Week 5 旧实现一致
    assert isinstance(out["layout_issues"], list)
    assert all(isinstance(i, dict) for i in out["layout_issues"])
    assert out["layout_issues_detected"] is True
    # 关卡③ 触发条件:layout_issues_detected == True(只要 issues 非空)
    assert out["layout_issues"] == qc_issues
