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


# ---------------------------------------------------------------------------
# P3b:英文配音轨不参与镜像一致性比对
# ---------------------------------------------------------------------------
def test_mirror_en_voiceover_track_is_allowed() -> None:
    """P3b:英文草稿多出 node_17 的配音轨 → 不算镜像不一致。"""
    zh = {"tracks": [{"type": "audio", "segments": [
        {"id": "audio-bgm-1", "material_id": "m-bgm"}]}]}
    en = {"tracks": [
        {"type": "audio", "name": "en_voiceover", "segments": [
            {"id": "segment-en-dub-1", "material_id": "en-dub-1"}]},
        {"type": "audio", "segments": [
            {"id": "audio-bgm-1", "material_id": "m-bgm"}]},
    ]}
    result = check_audio_mirror(zh, en)
    assert result["issues"] == []
    assert result["summary"]["matched"] is True


def test_mirror_en_voiceover_only_with_zh_empty_passes() -> None:
    """zh 没加 BGM + en 只有配音轨 → 不再误报「英文有音频轨但中文没有」。"""
    zh = {"tracks": []}
    en = {"tracks": [{"type": "audio", "name": "en_voiceover", "segments": [
        {"id": "segment-en-dub-1", "material_id": "en-dub-1"}]}]}
    result = check_audio_mirror(zh, en)
    assert result["issues"] == []
    assert result["summary"]["matched"] is True


# ---------------------------------------------------------------------------
# 阶段 1(2026-11-09 效果缺失修复):text 轨 ↔ materials.texts 一致性
# ---------------------------------------------------------------------------
from draft_ops.draft_integrity import check_texts_track_alignment


def test_texts_alignment_no_texts_no_track_passes() -> None:
    """无字幕是合法态(空输入跳过):不报。"""
    draft = {"materials": {"texts": []}, "tracks": [{"type": "video", "segments": []}]}
    assert check_texts_track_alignment(draft, "zh") == []


def test_texts_alignment_dangling_segment_reference_errors() -> None:
    """text 轨段引用不存在的字幕素材 → error(时间轴可见但数据断裂)。"""
    draft = {
        "materials": {"texts": []},
        "tracks": [
            {"type": "text", "id": "track-text-1", "segments": [
                {"id": "s1", "material_id": "text-zh-1",
                 "target_timerange": {"start": 0, "duration": 1_000_000}}]},
        ],
    }
    issues = check_texts_track_alignment(draft, "zh")
    assert any(i["severity"] == "error" and "text-zh-1" in i["detail"] for i in issues)


def test_texts_alignment_unreferenced_material_warns() -> None:
    """texts 桶里有素材但无任何 text 轨段引用 → warning(时间轴不可见)。"""
    draft = {
        "materials": {
            "texts": [{"id": "text-zh-1", "type": "text", "content": "孤儿字幕"}],
        },
        "tracks": [{"type": "video", "segments": []}],
    }
    issues = check_texts_track_alignment(draft, "zh")
    assert any(i["severity"] == "warning" and "未被任何 text 轨道段引用" in i["detail"] for i in issues)


def test_texts_alignment_native_content_ok(tmp_path: Path) -> None:
    """原生 content JSON + 轨段引用齐全 → 无 issue;check_draft_dir 纳入新检查。"""
    content = json.dumps(
        {"styles": [{"fill": {"content": {"render_type": "solid"}}}], "text": "你好"},
        ensure_ascii=False,
    )
    draft = {
        "materials": {
            "videos": [{"id": "video-1", "path": ""}],
            "texts": [{"id": "text-zh-1", "type": "text", "content": content,
                       "target_timerange": {"start": 0, "duration": 1_000_000}}],
        },
        "tracks": [
            {"type": "video", "segments": [
                {"id": "v1", "material_id": "video-1",
                 "target_timerange": {"start": 0, "duration": 1_000_000}}]},
            {"type": "text", "id": "track-text-1", "segments": [
                {"id": "s1", "material_id": "text-zh-1",
                 "target_timerange": {"start": 0, "duration": 1_000_000}}]},
        ],
    }
    assert check_texts_track_alignment(draft, "zh") == []
    result = check_draft_dir(_write_draft(tmp_path, draft), tag="zh", registered_expected=False)
    assert "texts_track_alignment" in result["checks"]
