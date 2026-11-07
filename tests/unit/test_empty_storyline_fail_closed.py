"""空 storyline fail-closed + 相关诊断(2026-11-07 实机回归)。

背景:切镜超时 0 镜头时,旧链路一路级联出空 storyline,node_05 照常发布
00:00 空草稿并挂关卡① —— 人工面对"调整空时间轴镜头顺序"的死局。回归点:
1. node_05:canonical clips=0 → ContractInvalid 终止运行,不发布不挂关卡;
2. node_05:1 个 clip 的正常 canonical 不受守卫影响(照常产草稿);
3. node_split_shots:0 镜头时 error_log 带 SPLIT_SHOTS_EMPTY 诊断标记;
4. probe_video_size:容忍部分 ffprobe 构建的尾部 'x' 分隔符("1920x1080x")。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from draft_ops.encryption_detector import DraftStatus
from nodes.node_05_generate_draft import generate_initial_jianying_draft
from nodes.storyline.node_split_shots import storyline_split_shots_node
from storyline.contract import ContractInvalid
from video_edit_capabilities import render as render_mod


def _fake_encrypt(_draft_dir):
    return DraftStatus.PLAINTEXT


def _canonical_plan(clips: list[dict]) -> dict:
    """构造合法 CanonicalTimeline dict(与 plan_timeline_pro 产物同形)。"""
    return {
        "schema_version": "1.0",
        "job_id": "unit-test",
        "created_at_ms": 0,
        "source_media": [
            {
                "media_id": "media_0001",
                "file_uri": "file:///C:/nonexistent-unit-test.mp4",
                "duration_ms": 4000,
                "media_type": "video",
            }
        ],
        "clips": clips,
    }


def _one_clip() -> dict:
    return {
        "clip_id": "clip_0001",
        "source_media_id": "media_0001",
        "source_in_ms": 0,
        "source_out_ms": 4000,
        "timeline_in_ms": 0,
        "timeline_out_ms": 4000,
    }


def test_node_05_empty_canonical_raises(tmp_path: Path) -> None:
    """canonical clips=0 → 直接终止运行(ContractInvalid),不写草稿不挂关卡。"""
    state = {"storyline_plan": _canonical_plan([]), "error_log": []}
    with pytest.raises(ContractInvalid) as exc_info:
        generate_initial_jianying_draft(state, tmp_path, encrypt_detector=_fake_encrypt)
    assert "clips=0" in str(exc_info.value)
    assert not (tmp_path / "draft_content.json").exists()


def test_node_05_single_clip_canonical_still_publishes(tmp_path: Path) -> None:
    """正例对照:1 个 clip 的 canonical 不触发守卫,照常写出草稿。

    writer 用桩(直接落盘),不走 safe_write_draft —— 本机剪映开着时
    写入互斥严格模式会拒绝写入,那是另一个机制的职责,不归本用例管。
    """

    def fake_writer(draft_dir, content):
        draft_dir = Path(draft_dir)
        draft_dir.mkdir(parents=True, exist_ok=True)
        (draft_dir / "draft_content.json").write_text(
            json.dumps(content, ensure_ascii=False), encoding="utf-8"
        )
        return {"jianying_running": False}

    state = {"storyline_plan": _canonical_plan([_one_clip()]), "error_log": []}
    out = generate_initial_jianying_draft(
        state, tmp_path, encrypt_detector=_fake_encrypt, writer=fake_writer
    )
    assert out.get("draft_path"), "1 clip 的正常路径不得被空守卫误伤"
    assert (tmp_path / "draft_content.json").exists()


def test_node_split_shots_empty_marks_error_log(tmp_path: Path) -> None:
    """切镜 0 镜头:节点产物照写,但 status/error log 带 SPLIT_SHOTS_EMPTY。"""
    artifact = tmp_path / "load_media.json"
    artifact.write_text(
        json.dumps(
            {
                "media": [
                    {
                        "media_id": "media_0001",
                        "path": str(tmp_path / "missing.mp4"),  # 不存在 → 0 clips
                        "media_type": "video",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    state = {
        "storyline_media_artifact": str(artifact),
        "storyline_outputs_root": str(tmp_path / "out"),
        "status_log": [],
        "error_log": [],
    }
    out = storyline_split_shots_node(state)
    assert any("SPLIT_SHOTS_EMPTY" in e for e in out.get("error_log", []))
    assert any("split_shots_empty" in t for t in out.get("status_log", []))


def test_probe_video_size_tolerates_trailing_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实机 ffprobe 输出 "1920x1080x\\r\\n" → 解析 (1920, 1080),不再 int('1080x')。"""

    def fake_run_proc(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="1920x1080x\r\n", stderr="")

    monkeypatch.setattr(render_mod._ffproc, "run_proc", fake_run_proc)
    assert render_mod.probe_video_size(Path("any.mov")) == (1920, 1080)
