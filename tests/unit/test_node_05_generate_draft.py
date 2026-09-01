"""节点 5 单测(附件 3.2 节要点):加密检测 → 策略 → 构造 → 原子写入。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from draft_ops.encryption_detector import DraftStatus
from nodes.node_05_generate_draft import (
    _build_draft_content,
    _init_empty_tracks,
    _shot_plan_to_materials,
    generate_initial_jianying_draft,
)


def test_encryption_detected_early_exits(tmp_path: Path) -> None:
    """加密状态 → 早退,draft_path=None,error_log 有记录。"""

    def fake_encrypt(_draft_dir):
        return DraftStatus.ENCRYPTED

    state = {
        "shot_plan": {"shots": [{"id": "s1", "video_ref": "v1"}]},
        "video_input_path": "/tmp/30s.mp4",
        "error_log": [],
    }
    out = generate_initial_jianying_draft(
        state, tmp_path, encrypt_detector=fake_encrypt
    )
    assert out["draft_path"] is None
    assert out["draft_encryption_status"] == "encrypted"
    assert any("检测到已加密草稿" in e for e in out["error_log"])


def test_happy_path_produces_draft_and_uses_atomic_writer(tmp_path: Path) -> None:
    """正常路径:产出 draft_path,顶层含 canvas_config/materials/tracks,写走原子写入。"""

    def fake_encrypt(_draft_dir):
        return DraftStatus.PLAINTEXT

    state = {
        "shot_plan": {
            "shots": [
                {"id": "s1", "video_ref": "v1", "start_s": 0.0, "end_s": 5.0},
                {"id": "s2", "video_ref": "v1", "start_s": 5.0, "end_s": 10.0},
            ]
        },
        "video_input_path": "/tmp/30s.mp4",
        "error_log": [],
    }

    writer_called = []

    def fake_writer(draft_file, content):
        writer_called.append((draft_file, content))
        # 真写一份以验证最终文件可读
        from draft_ops.atomic_writer import atomic_write_draft

        atomic_write_draft(draft_file, content)

    out = generate_initial_jianying_draft(
        state, tmp_path, encrypt_detector=fake_encrypt, writer=fake_writer
    )

    assert len(writer_called) == 1, "应当调用一次原子写入"
    draft_path = Path(out["draft_path"])
    assert draft_path.exists()
    assert draft_path.name == "draft_content.json"
    loaded = json.loads(draft_path.read_text(encoding="utf-8"))
    assert set(loaded.keys()) >= {"canvas_config", "materials", "tracks"}
    assert loaded["canvas_config"]["width"] == 1080
    assert loaded["canvas_config"]["height"] == 1920
    assert len(loaded["materials"]["videos"]) == 2
    assert out["draft_encryption_status"] == "plaintext"
    assert out["draft_version_strategy"] == "strategy_a_version_lock"


def test_writer_called_not_bare_open(tmp_path: Path) -> None:
    """禁止节点 5 直接 open().write() —— 必须走注入的 writer(单测层断言)。"""
    from draft_ops.atomic_writer import atomic_write_draft

    state = {
        "shot_plan": {"shots": [{"id": "s1", "video_ref": "v1"}]},
        "video_input_path": "/tmp/30s.mp4",
        "error_log": [],
    }

    captured = {}

    def fake_writer(draft_file, content):
        captured["called"] = True
        # 真原子写入
        atomic_write_draft(draft_file, content)

    generate_initial_jianying_draft(
        state,
        tmp_path,
        encrypt_detector=lambda d: DraftStatus.PLAINTEXT,
        writer=fake_writer,
    )
    assert captured.get("called") is True


# ---------------------------------------------------------------------------
# 辅助函数小测
# ---------------------------------------------------------------------------
def test_shot_plan_to_materials_basic() -> None:
    mats = _shot_plan_to_materials(
        {"shots": [{"id": "s1", "video_ref": "v1", "start_s": 0.0, "end_s": 1.0}]},
        "/tmp/v.mp4",
    )
    assert len(mats) == 1
    assert mats[0]["id"] == "video-1"
    assert mats[0]["path"] == "/tmp/v.mp4"
    assert mats[0]["shot_id"] == "s1"


def test_shot_plan_to_materials_empty() -> None:
    assert _shot_plan_to_materials({}, "/tmp/v.mp4") == []
    assert _shot_plan_to_materials({"shots": []}, "/tmp/v.mp4") == []


def test_init_empty_tracks() -> None:
    assert _init_empty_tracks() == []


def test_build_draft_content_top_level_shape() -> None:
    content = _build_draft_content(
        {"shots": [{"id": "s1", "video_ref": "v1"}]},
        "/tmp/v.mp4",
    )
    assert set(content.keys()) == {"canvas_config", "materials", "tracks"}
    assert "videos" in content["materials"]
    assert isinstance(content["tracks"], list)
