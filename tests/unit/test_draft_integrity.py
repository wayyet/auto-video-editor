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


# ---------------------------------------------------------------------------
# 阶段 6(2026-11-09 效果缺失修复):required-feature gate
# ---------------------------------------------------------------------------
from draft_ops.draft_integrity import (  # noqa: E402
    CAT_DEGRADED,
    CAT_FAILED,
    CAT_NOT_APPLICABLE,
    CAT_PASSED,
    REQUIRED_FEATURE_KEYS,
    build_join_qa_report,
    build_qa_categories,
    check_resource_ids,
    default_required_features,
    resolve_required_features,
)


def test_check_resource_ids_placeholder_reported() -> None:
    draft = {
        "materials": {
            "transitions": [{"id": "transition-1", "resource_id": ""}],
            "video_effects": [{"id": "vfx-1", "resource_id": "PLACEHOLDER_X"}],
            "stickers": [{"id": "sticker-t1", "resource_id": "123"}],
            "material_animations": [{"id": "text-anim-m1", "resource_id": "1234567890123456789"}],
        },
    }
    issues = check_resource_ids(draft, "zh")
    details = [i["detail"] for i in issues]
    assert any("transition-1" in d for d in details)
    assert any("vfx-1" in d for d in details)
    assert any("sticker-t1" in d for d in details)
    # 19 位合法 + 人工 uuid 不报
    assert not any("text-anim-m1" in d for d in details)


def test_check_resource_ids_missing_key_skipped() -> None:
    """resource_id 键缺失(测试夹具/旧形态)不报;真实构造器总写该键。"""
    draft = {"materials": {"transitions": [{"id": "transition-1"}]}}
    assert check_resource_ids(draft, "zh") == []


def test_check_resource_ids_ignores_human_uuid_entries() -> None:
    draft = {
        "materials": {
            "transitions": [{"id": "d1a2b3c4-0000-0000-0000-000000000000", "resource_id": ""}],
        },
    }
    assert check_resource_ids(draft, "zh") == []


def test_default_required_features_follow_tts_backend(monkeypatch) -> None:
    monkeypatch.setenv("TTS_BACKEND", "firered")
    assert default_required_features()["voiceover_zh"] is True
    assert default_required_features()["voiceover_en"] is True
    monkeypatch.setenv("TTS_BACKEND", "stub")
    assert default_required_features()["voiceover_zh"] is False
    # 恒定项:BGM/贴纸默认可选;字幕/转场/动画/淡入淡出默认必选
    defaults = default_required_features()
    assert defaults["bgm"] is False and defaults["stickers"] is False
    assert defaults["subtitles_zh"] and defaults["transitions"]
    assert defaults["text_animation"] and defaults["volume_fades"]


def test_resolve_required_features_explicit_overrides_default(monkeypatch) -> None:
    monkeypatch.setenv("TTS_BACKEND", "firered")
    resolved = resolve_required_features({"required_features": {"voiceover_zh": False, "bgm": True}})
    assert resolved["voiceover_zh"] is False  # 显式覆盖推导
    assert resolved["bgm"] is True
    assert resolved["subtitles_zh"] is True  # 未声明的保持推导
    # 非布尔/未知键忽略
    resolved2 = resolve_required_features({"required_features": {"bgm": "yes", "unknown": True}})
    assert resolved2["bgm"] is False  # "yes" 非布尔,忽略
    # 无显式声明 → 纯推导
    assert resolve_required_features({}) == default_required_features()


def _cat_state(**overrides) -> dict:
    state = {
        "draft_path": "/tmp/zh/draft_content.json",
        "draft_dir_en_branch": "/tmp/en_branch",
    }
    state.update(overrides)
    return state


def test_build_qa_categories_narration_without_subtitles_failed() -> None:
    """有旁白产物但草稿无我方中文字幕 → subtitles_zh failed。"""
    categories, new_issues, gate_failed = build_qa_categories(
        _cat_state(voiceover_summary={"provider": "firered", "written": 3}),
        zh_draft={"materials": {"texts": []}, "tracks": []},
        en_draft=None,
        deep_issues=[],
    )
    assert categories["subtitles_zh"]["status"] == CAT_FAILED
    assert any("字幕" in i["detail"] for i in new_issues)
    assert gate_failed is True  # subtitles_zh 默认 required


def test_build_qa_categories_no_narration_na() -> None:
    """无旁白产物 → subtitles_zh not_applicable(不阻断)。"""
    categories, _, gate_failed = build_qa_categories(
        _cat_state(),
        zh_draft={"materials": {"texts": []}, "tracks": []},
        en_draft=None,
        deep_issues=[],
    )
    assert categories["subtitles_zh"]["status"] == CAT_NOT_APPLICABLE
    assert gate_failed is False


def test_build_qa_categories_single_shot_transitions_na() -> None:
    """单镜头(≤1 视频段)→ transitions not_applicable。"""
    draft = {"tracks": [{"type": "video", "segments": [{"id": "s1"}]}]}
    categories, _, _ = build_qa_categories(_cat_state(), zh_draft=draft, en_draft=None, deep_issues=[])
    assert categories["transitions"]["status"] == CAT_NOT_APPLICABLE


def test_build_qa_categories_stub_voiceover_degraded_blocks_when_required(monkeypatch) -> None:
    monkeypatch.setenv("TTS_BACKEND", "firered")
    categories, new_issues, gate_failed = build_qa_categories(
        _cat_state(voiceover_summary={"provider": "stub", "written": 2, "stub_count": 2}),
        zh_draft={"tracks": []},
        en_draft=None,
        deep_issues=[],
    )
    assert categories["voiceover_zh"]["status"] == CAT_DEGRADED
    assert any("Stub" in i["detail"] for i in new_issues)
    assert gate_failed is True


def test_build_qa_categories_required_bgm_missing_blocks(monkeypatch) -> None:
    monkeypatch.setenv("TTS_BACKEND", "stub")
    categories, _, gate_failed = build_qa_categories(
        _cat_state(required_features={"bgm": True}),
        zh_draft={"tracks": []},
        en_draft=None,
        deep_issues=[],
    )
    assert categories["bgm"]["status"] == CAT_FAILED
    assert gate_failed is True


def test_build_qa_categories_optional_bgm_missing_na() -> None:
    categories, _, gate_failed = build_qa_categories(
        _cat_state(),
        zh_draft={"tracks": []},
        en_draft=None,
        deep_issues=[],
    )
    assert categories["bgm"]["status"] == CAT_NOT_APPLICABLE
    assert gate_failed is False


def test_build_qa_categories_deep_issue_routed_to_category() -> None:
    """深度/尾链 issue 带 category → failed 按桶归位。"""
    deep = [
        {"check": "转场", "severity": "error", "draft": "zh", "detail": "中文主草稿转场缺失", "category": "transitions"},
    ]
    draft = {"tracks": [{"type": "video", "segments": [{"id": "s1"}, {"id": "s2"}]}]}
    categories, _, gate_failed = build_qa_categories(_cat_state(), zh_draft=draft, en_draft=None, deep_issues=deep)
    assert categories["transitions"]["status"] == CAT_FAILED
    assert gate_failed is True


def test_build_qa_categories_all_category_keys_present() -> None:
    categories, _, _ = build_qa_categories(_cat_state(), zh_draft=None, en_draft=None, deep_issues=[])
    assert set(categories.keys()) == set(
        list(REQUIRED_FEATURE_KEYS) + ["video_effects", "covers", "srt", "draft_integrity"]
    )


def test_join_qa_report_carries_sha256(tmp_path: Path) -> None:
    """报告顶层带双草稿回读 SHA-256(创建台可指出"哪份草稿哪个版本")。"""
    from draft_ops.draft_lock import hash_draft_files

    zh_dir = tmp_path / "zh"
    en_dir = tmp_path / "en_branch"
    _write_draft(zh_dir, _minimal_draft())
    _write_draft(en_dir, _minimal_draft())
    report = build_join_qa_report({
        "draft_path": str(zh_dir / "draft_content.json"),
        "draft_dir_en_branch": str(en_dir),
    })
    assert report["sha256"]["zh"]["draft_content.json"] == hash_draft_files(
        zh_dir / "draft_content.json"
    )["draft_content.json"]
    assert len(report["sha256"]["zh"]["draft_content.json"]) == 64
