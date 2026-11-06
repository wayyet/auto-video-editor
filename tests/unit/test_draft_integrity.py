"""draft_ops.draft_integrity 模块级单测(阶段 4 深度 QA 的边界用例)。"""

from __future__ import annotations

import json
from pathlib import Path

from draft_ops.draft_integrity import (
    check_audio_mirror,
    check_draft_dir,
    check_material_paths,
    check_track_references,
    issue_text,
    registered_expected_for,
)


def _write_draft(dir_path: Path, draft: dict) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(draft, ensure_ascii=False)
    (dir_path / "draft_content.json").write_text(serialized, encoding="utf-8")
    (dir_path / "draft_info.json").write_text(serialized, encoding="utf-8")
    return dir_path


def _minimal_draft() -> dict:
    return {
        "materials": {
            "videos": [{"id": "video-1", "path": ""}],  # 空 path 不查盘
            "audios": [],  # 无音频轨:镜像无事可查
        },
        "tracks": [
            {
                "type": "video",
                "segments": [
                    {
                        "id": "segment-1",
                        "material_id": "video-1",
                        "extra_material_refs": ["sticker-1", "ghost-ref"],
                    }
                ],
            }
        ],
    }


def test_issue_text_draft_tag() -> None:
    assert issue_text({"check": "素材路径", "severity": "error", "draft": "zh", "detail": "x"}).startswith("[中文素材路径]")
    assert issue_text({"check": "BGM镜像", "severity": "error", "draft": "-", "detail": "x"}).startswith("[BGM镜像]")


def test_checkable_path_rules() -> None:
    issues = check_material_paths(
        {
            "materials": {
                "videos": [
                    {"id": "v1", "path": "https://cdn.example.com/a.mp4"},  # URI 跳过
                    {"id": "v2", "path": ""},  # 空串跳过
                    {"id": "v3", "path": "<placeholder>"},  # 测试占位跳过
                    {"id": "v4", "path": "assets://music/x.mp3"},  # 剪映 URI 跳过
                ],
                "audios": [
                    {"id": "a1", "ref": "https://cdn.example.com/b.mp3"},  # URI 跳过
                ],
            }
        },
        tag="zh",
    )
    assert issues == []


def test_dangling_reference_reported(tmp_path: Path) -> None:
    draft = _minimal_draft()
    issues = check_track_references(draft, tag="zh")
    assert any("ghost-ref" in i["detail"] for i in issues)
    assert all("video-1" not in i["detail"] for i in issues)


def test_mirror_both_sides_empty_passes() -> None:
    result = check_audio_mirror({"tracks": []}, {"tracks": []})
    assert result["issues"] == []
    assert result["summary"]["matched"] is True


def test_mirror_en_extra_track_warns() -> None:
    zh = {"tracks": []}
    en = {"tracks": [{"type": "audio", "segments": [{"id": "s1", "material_id": "m1"}]}]}
    result = check_audio_mirror(zh, en)
    assert any(i["severity"] == "warning" for i in result["issues"])


def test_check_draft_dir_missing_dir(tmp_path: Path) -> None:
    result = check_draft_dir(tmp_path / "nope", tag="zh", registered_expected=False)
    assert result["present"] is False
    assert any(i["check"] == "目录" for i in result["issues"])


def test_registered_expected_requires_publish_enabled(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "jy_root"
    draft_dir = root / "d"
    draft_dir.mkdir(parents=True)
    monkeypatch.setattr("config.JY_DRAFT_PUBLISH_ENABLED", False)
    monkeypatch.setattr("config.JIANYING_DRAFT_ROOT", str(root))
    assert registered_expected_for(draft_dir) is False
    monkeypatch.setattr("config.JY_DRAFT_PUBLISH_ENABLED", True)
    assert registered_expected_for(draft_dir) is True
    # 不在草稿库根下 → 不要求注册
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert registered_expected_for(outside) is False
