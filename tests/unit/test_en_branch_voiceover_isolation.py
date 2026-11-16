"""英文分支配音隔离(2026-11-17 P0)单测。

覆盖修订计划 §8.1(fork 清理)/§8.3(QA)核心场景:
- ``remove_chinese_voiceover_from_draft`` 纯函数:轨名精确匹配、共享素材不删、
  companion 回收、链产孤儿素材 GC、幂等;
- fork 三路径:新建清理(v2)、重放幂等、历史目录 legacy 不改写、写回失败 fail-fast;
- ``check_audio_mirror`` 双侧排除配音轨;
- ``check_no_zh_voiceover_in_en_draft`` + ``voice_language_isolation`` 类别 gate;
- 物理 WAV 文件永不删除(只改草稿 JSON)。

node_12 v2 镜像过滤用例见 ``test_node_12_human_add_bgm.py``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from draft_ops.draft_integrity import (
    CAT_FAILED,
    CAT_NOT_APPLICABLE,
    CAT_PASSED,
    build_join_qa_report,
    build_qa_categories,
    check_audio_mirror,
    check_no_zh_voiceover_in_en_draft,
)
from nodes._dual_draft import remove_chinese_voiceover_from_draft
from nodes.node_fork_english_branch import _copy_snapshot, _fork, fork_draft_for_english_branch


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
STORYLINE_WAV = "X:/runs/t1/storyline/voiceover_0001.wav"


def _vo_draft() -> dict:
    """中文 VO 轨 + BGM 轨 + en 配音轨;含 VO 专属 speed 与共享 speed。"""
    return {
        "duration": 10_000_000,
        "materials": {
            "audios": [
                {"id": "vo-mat-1", "name": "voiceover_0001.wav", "path": STORYLINE_WAV},
                {"id": "bgm-mat-1", "name": "bgm.mp3", "path": "C:/music/bgm.mp3"},
                {"id": "en-mat-1", "name": "en_voiceover", "path": "C:/tts/en.wav"},
            ],
            "speeds": [
                {"id": "speed-vo", "speed": 1.0},
                {"id": "speed-shared", "speed": 1.2},
            ],
        },
        "tracks": [
            {"type": "video", "segments": [{"id": "v1"}]},
            {
                "type": "audio",
                "id": "track-vo",
                "name": "Voiceover_zh",
                "segments": [
                    {
                        "id": "vo-seg-1",
                        "material_id": "vo-mat-1",
                        "extra_material_refs": ["speed-vo", "speed-shared"],
                    }
                ],
            },
            {
                "type": "audio",
                "id": "track-bgm",
                "name": "BGM",
                "segments": [
                    {
                        "id": "bgm-seg-1",
                        "material_id": "bgm-mat-1",
                        "extra_material_refs": ["speed-shared"],
                    }
                ],
            },
            {
                "type": "audio",
                "id": "track-en",
                "name": "en_voiceover",
                "segments": [{"id": "en-seg-1", "material_id": "en-mat-1"}],
            },
        ],
    }


def _track_names(draft: dict) -> list[str]:
    return [
        str(t.get("name"))
        for t in draft.get("tracks") or []
        if isinstance(t, dict) and t.get("type") == "audio"
    ]


# ---------------------------------------------------------------------------
# remove_chinese_voiceover_from_draft 纯函数
# ---------------------------------------------------------------------------
def test_remove_keeps_bgm_and_en_voiceover() -> None:
    """只删 Voiceover_zh 轨;BGM / en_voiceover / video 轨原样保留。"""
    draft = _vo_draft()
    summary = remove_chinese_voiceover_from_draft(draft)

    assert summary["removed_tracks"] == 1
    assert summary["removed_segments"] == 1
    assert _track_names(draft) == ["BGM", "en_voiceover"]
    assert [t["type"] for t in draft["tracks"] if t["type"] == "video"] == ["video"]


def test_remove_orphans_material_and_speed_but_keeps_shared() -> None:
    """VO 专属素材/speed 回收;被其它轨引用的共享 speed 保留。"""
    draft = _vo_draft()
    remove_chinese_voiceover_from_draft(draft)

    audio_ids = [m["id"] for m in draft["materials"]["audios"]]
    assert "vo-mat-1" not in audio_ids
    assert "bgm-mat-1" in audio_ids and "en-mat-1" in audio_ids
    speed_ids = [m["id"] for m in draft["materials"]["speeds"]]
    assert "speed-vo" not in speed_ids
    assert "speed-shared" in speed_ids


def test_remove_keeps_material_shared_with_other_track() -> None:
    """VO 轨与 BGM 轨共用同一素材 → 素材保留,不产生悬空引用。"""
    draft = _vo_draft()
    draft["tracks"][2]["segments"][0]["material_id"] = "vo-mat-1"  # BGM 也引用它
    summary = remove_chinese_voiceover_from_draft(draft)

    assert summary["removed_tracks"] == 1
    audio_ids = [m["id"] for m in draft["materials"]["audios"]]
    assert "vo-mat-1" in audio_ids
    assert "vo-mat-1" not in summary["removed_material_ids"]
    # 引用完整性:BGM 段的 material_id 仍可解析
    bgm_seg = next(t for t in draft["tracks"] if t.get("name") == "BGM")["segments"][0]
    assert bgm_seg["material_id"] in {m["id"] for m in draft["materials"]["audios"]}


def test_gc_orphan_chain_audio_material_only_within_storyline_root() -> None:
    """无轨引用的链产配音素材(snapshot② 实证形态)按路径边界回收;
    路径在边界外时不回收。"""
    draft = _vo_draft()
    draft["tracks"] = [t for t in draft["tracks"] if t.get("name") != "Voiceover_zh"]
    # vo-mat-1 指向边界外的路径(仅剩的非配音孤儿素材):不回收
    draft["materials"]["audios"][0]["path"] = "C:/elsewhere/voiceover_0001.wav"
    draft["materials"]["audios"].append(
        {"id": "orphan-vo", "name": "voiceover_0002.wav", "path": STORYLINE_WAV}
    )

    root = "X:/runs/t1/storyline"
    summary = remove_chinese_voiceover_from_draft(draft, storyline_root=root)
    audio_ids = [m["id"] for m in draft["materials"]["audios"]]
    assert "orphan-vo" not in audio_ids
    assert "vo-mat-1" in audio_ids
    assert summary["removed_materials"] == 1
    assert summary["removed_material_ids"] == ["orphan-vo"]


def test_remove_is_idempotent() -> None:
    """重放:第二次运行 changed=False、零删除。"""
    draft = _vo_draft()
    first = remove_chinese_voiceover_from_draft(draft, storyline_root="X:/runs/t1/storyline")
    assert first["changed"] is True
    second = remove_chinese_voiceover_from_draft(draft, storyline_root="X:/runs/t1/storyline")
    assert second["changed"] is False
    assert second["removed_tracks"] == 0 and second["removed_materials"] == 0


def test_remove_never_touches_disk(tmp_path: Path) -> None:
    """清理只改草稿 dict:被删素材指向的 WAV 文件必须原样存在。"""
    wav = tmp_path / "storyline" / "voiceover_0001.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"RIFF")
    draft = _vo_draft()
    draft["materials"]["audios"][0]["path"] = str(wav)

    remove_chinese_voiceover_from_draft(draft, storyline_root=str(tmp_path / "storyline"))
    assert wav.is_file()


# ---------------------------------------------------------------------------
# fork 三路径 + fail-fast
# ---------------------------------------------------------------------------
def _seed_snapshot_with_vo(snap_dir: Path, storyline_root: Path) -> None:
    """snapshot② 预置:Voiceover_zh 轨 + BGM 轨 + 链产孤儿配音素材 + 真实 WAV。"""
    snap_dir.mkdir(parents=True, exist_ok=True)
    wav = storyline_root / "voiceover_0001.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF")
    draft = {
        "duration": 10_000_000,
        "materials": {
            "audios": [
                {"id": "vo-mat-1", "name": "voiceover_0001.wav", "path": str(wav)},
                {"id": "orphan-vo", "name": "voiceover_0002.wav", "path": str(wav)},
                {"id": "bgm-mat-1", "name": "bgm.mp3", "path": "C:/music/bgm.mp3"},
            ]
        },
        "tracks": [
            {"type": "video", "segments": [{"id": "v1"}]},
            {"type": "audio", "id": "track-vo", "name": "Voiceover_zh", "segments": [
                {"id": "vo-seg-1", "material_id": "vo-mat-1"}]},
            {"type": "audio", "id": "track-bgm", "name": "BGM", "segments": [
                {"id": "bgm-seg-1", "material_id": "bgm-mat-1"}]},
        ],
    }
    (snap_dir / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False), encoding="utf-8"
    )


def _fork_state(snap: Path, storyline_root: Path, **extra) -> dict:
    return {
        "session_id": "t1",
        "snapshot2_path": str(snap / "draft_content.json"),
        "storyline_voiceover_artifact": str(storyline_root / "voiceover.json"),
        **extra,
    }


def test_fork_new_cleans_voiceover_and_sets_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新建英文分支:中文 VO 轨 + 孤儿素材被清,policy=v2,WAV 文件仍在磁盘。"""
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts" / "default"))
    snap = tmp_path / "snapshots" / "snapshot2"
    storyline_root = tmp_path / "storyline"
    _seed_snapshot_with_vo(snap, storyline_root)

    out = _fork(_fork_state(snap, storyline_root))
    assert out["policy"] == "v2"

    en_draft = json.loads(
        (Path(out["draft_dir_en_branch"]) / "draft_content.json").read_text(encoding="utf-8")
    )
    assert "Voiceover_zh" not in _track_names(en_draft)
    assert "BGM" in _track_names(en_draft)
    audio_ids = [m["id"] for m in en_draft["materials"]["audios"]]
    assert "vo-mat-1" not in audio_ids and "orphan-vo" not in audio_ids
    assert "bgm-mat-1" in audio_ids
    # 物理文件不删除
    assert (storyline_root / "voiceover_0001.wav").is_file()


def test_fork_wrapper_reports_cleanup_and_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wrapper:status_log 带清理打点,state 回填策略版本字段。"""
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts" / "default"))
    snap = tmp_path / "snapshots" / "snapshot2"
    storyline_root = tmp_path / "storyline"
    _seed_snapshot_with_vo(snap, storyline_root)

    out = fork_draft_for_english_branch(_fork_state(snap, storyline_root))
    assert out["en_branch_audio_policy_version"] == "v2"
    assert any(s.startswith("node_fork_zh_voiceover_removed:") for s in out["status_log"])


def test_fork_replay_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同任务 checkpoint 重放(state 已带 v2)→ 幂等重清,结果不变。"""
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts" / "default"))
    snap = tmp_path / "snapshots" / "snapshot2"
    storyline_root = tmp_path / "storyline"
    _seed_snapshot_with_vo(snap, storyline_root)

    first = _fork(_fork_state(snap, storyline_root))
    replay_state = _fork_state(
        snap,
        storyline_root,
        draft_dir_en_branch=first["draft_dir_en_branch"],
        en_branch_audio_policy_version="v2",
    )
    second = _fork(replay_state)
    assert second["draft_dir_en_branch"] == first["draft_dir_en_branch"]
    assert second["policy"] == "v2"

    en_draft = json.loads(
        (Path(first["draft_dir_en_branch"]) / "draft_content.json").read_text(encoding="utf-8")
    )
    assert "Voiceover_zh" not in _track_names(en_draft)
    assert len([t for t in en_draft["tracks"] if t.get("type") == "audio"]) == 1


def test_fork_legacy_dir_not_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复用无策略标记的历史目录:不改写(VO 轨保留),policy=legacy。"""
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts" / "default"))
    snap = tmp_path / "snapshots" / "snapshot2"
    storyline_root = tmp_path / "storyline"
    _seed_snapshot_with_vo(snap, storyline_root)

    # 模拟历史目录:直接 copytree,不经过新策略
    legacy_dir = tmp_path / "drafts" / "en_branch_t1_historical"
    _copy_snapshot(snap, legacy_dir)

    out = _fork(
        _fork_state(
            snap,
            storyline_root,
            draft_dir_en_branch=str(legacy_dir),  # 无 en_branch_audio_policy_version
        )
    )
    assert out["policy"] == "legacy"
    assert out["cleanup"] is None

    en_draft = json.loads((legacy_dir / "draft_content.json").read_text(encoding="utf-8"))
    assert "Voiceover_zh" in _track_names(en_draft)  # 历史目录原样

    out2 = fork_draft_for_english_branch(
        _fork_state(snap, storyline_root, draft_dir_en_branch=str(legacy_dir))
    )
    assert out2["en_branch_audio_policy_version"] == "legacy"
    assert "legacy_en_branch_cleanup_skipped" in out2["status_log"]


def test_fork_cleanup_write_failure_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写回失败 → fail-fast 抛异常,不标记成功。"""
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts" / "default"))
    snap = tmp_path / "snapshots" / "snapshot2"
    storyline_root = tmp_path / "storyline"
    _seed_snapshot_with_vo(snap, storyline_root)

    import nodes.node_fork_english_branch as fork_mod

    def _bad_write(*args, **kwargs):
        return {"ok": False, "reason": "boom"}

    monkeypatch.setattr(fork_mod, "write_draft", _bad_write)
    with pytest.raises(RuntimeError, match="写回失败"):
        _fork(_fork_state(snap, storyline_root))


# ---------------------------------------------------------------------------
# QA:镜像排除 + 语言隔离 + 类别 gate
# ---------------------------------------------------------------------------
def test_audio_mirror_excludes_voiceover_tracks_both_sides() -> None:
    """双侧配音轨都不参与比对:中文 VO 不镜像不算缺;英文 VO 特有没有算多。"""
    zh = _vo_draft()
    # zh 主草稿不会含英文配音轨:去掉 track-en/en-mat-1,只留 VO + BGM
    zh["tracks"] = [t for t in zh["tracks"] if t.get("name") != "en_voiceover"]
    zh["materials"]["audios"] = [
        m for m in zh["materials"]["audios"] if m["id"] != "en-mat-1"
    ]
    en = {
        "duration": 10_000_000,
        "materials": {"audios": [{"id": "bgm-mat-1", "path": "C:/music/bgm.mp3"}]},
        "tracks": [
            {"type": "video", "segments": [{"id": "v1"}]},
            {"type": "audio", "id": "track-bgm", "name": "BGM", "segments": [
                {"id": "bgm-seg-1", "material_id": "bgm-mat-1"}]},
            {"type": "audio", "id": "track-en", "name": "en_voiceover", "segments": [
                {"id": "en-seg-1", "material_id": "en-mat-1"}]},
        ],
    }
    result = check_audio_mirror(zh, en)
    assert result["summary"]["matched"] is True
    assert result["issues"] == []

    # BGM 段缺失仍要报(镜像一致性不放宽)
    en_missing = {"tracks": [t for t in en["tracks"] if t.get("name") != "BGM"],
                  "materials": en["materials"]}
    result2 = check_audio_mirror(zh, en_missing)
    assert any(i["severity"] == "error" for i in result2["issues"])


def test_check_no_zh_voiceover_v2_reports_error() -> None:
    en = _vo_draft()  # 含 Voiceover_zh 轨
    issues = check_no_zh_voiceover_in_en_draft(en, "v2")
    assert len(issues) == 1
    assert issues[0]["severity"] == "error"
    assert issues[0]["draft"] == "en"
    assert "track-vo" in issues[0]["detail"]
    assert "vo-mat-1" in issues[0]["detail"]


def test_check_no_zh_voiceover_legacy_and_clean() -> None:
    en = _vo_draft()
    assert check_no_zh_voiceover_in_en_draft(en, None) == []
    assert check_no_zh_voiceover_in_en_draft(en, "legacy") == []

    clean = _vo_draft()
    clean["tracks"] = [t for t in clean["tracks"] if t.get("name") != "Voiceover_zh"]
    assert check_no_zh_voiceover_in_en_draft(clean, "v2") == []


def _qa_state(policy: str | None, en_branch: bool = True) -> dict:
    state: dict = {"required_features": {}}
    if en_branch:
        state["draft_dir_en_branch"] = "en_branch_x"
    if policy:
        state["en_branch_audio_policy_version"] = policy
    return state


def test_category_isolation_v2_gate_blocks() -> None:
    """v2 + 深度 QA 配音隔离 error → 类别 failed 且 gate_failed(不受 required 关闭)。"""
    deep = [
        {"check": "配音隔离", "severity": "error", "draft": "en",
         "detail": "英文草稿残留中文配音轨 track-vo", "category": "voice_language_isolation"},
    ]
    categories, _, gate_failed = build_qa_categories(
        _qa_state("v2"), zh_draft=None, en_draft={"tracks": []}, deep_issues=deep
    )
    assert categories["voice_language_isolation"]["status"] == "failed"
    assert categories["voice_language_isolation"]["required"] is True
    assert gate_failed is True


def test_category_isolation_legacy_not_applicable() -> None:
    """legacy / 英文分支未生成 → 类别 n/a,永不阻断。"""
    categories, _, gate_failed = build_qa_categories(
        _qa_state(None), zh_draft=None, en_draft={"tracks": []}, deep_issues=[]
    )
    assert categories["voice_language_isolation"]["status"] == CAT_NOT_APPLICABLE
    assert gate_failed is False

    categories2, _, gate_failed2 = build_qa_categories(
        _qa_state("v2", en_branch=False), zh_draft=None, en_draft=None, deep_issues=[]
    )
    assert categories2["voice_language_isolation"]["status"] == CAT_NOT_APPLICABLE
    assert gate_failed2 is False


def _write_both(dir_path: Path, draft: dict) -> Path:
    """双文件契约(content+info 同内容);空 path 不触发查盘检查。"""
    dir_path.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(draft, ensure_ascii=False)
    (dir_path / "draft_content.json").write_text(serialized, encoding="utf-8")
    (dir_path / "draft_info.json").write_text(serialized, encoding="utf-8")
    return dir_path


def _clean_video_draft() -> dict:
    """引用自洽的最小草稿(无悬空、无查盘路径)。"""
    return {
        "duration": 1_000_000,
        "materials": {"videos": [{"id": "video-1", "path": ""}], "audios": []},
        "tracks": [
            {"type": "video", "segments": [{"id": "segment-1", "material_id": "video-1"}]},
        ],
    }


def test_join_qa_report_carries_isolation(tmp_path: Path) -> None:
    """build_join_qa_report:v2 + en 残留 VO 轨 → passed=False + issue 计数。"""
    zh_dir = tmp_path / "zh"
    en_dir = tmp_path / "en_branch"
    en_draft = _clean_video_draft()
    en_draft["materials"]["audios"].append({"id": "vo-mat-1", "path": ""})
    en_draft["tracks"].append({
        "type": "audio", "id": "track-vo", "name": "Voiceover_zh",
        "segments": [{"id": "vo-seg-1", "material_id": "vo-mat-1"}],
    })
    _write_both(zh_dir, _clean_video_draft())
    _write_both(en_dir, en_draft)

    report = build_join_qa_report({
        "draft_path": str(zh_dir / "draft_content.json"),
        "draft_dir_en_branch": str(en_dir),
        "en_branch_audio_policy_version": "v2",
    })
    assert report["skipped"] is False
    assert report["language_isolation"]["policy"] == "v2"
    assert report["language_isolation"]["issue_count"] == 1
    assert report["passed"] is False


def test_join_qa_report_legacy_no_isolation(tmp_path: Path) -> None:
    """legacy:同形状草稿不触发隔离 issue(不追溯阻断旧任务)。"""
    zh_dir = tmp_path / "zh"
    en_dir = tmp_path / "en_branch"
    draft = _clean_video_draft()
    draft["materials"]["audios"].append({"id": "vo-mat-1", "path": ""})
    draft["tracks"].append({
        "type": "audio", "id": "track-vo", "name": "Voiceover_zh",
        "segments": [{"id": "vo-seg-1", "material_id": "vo-mat-1"}],
    })
    _write_both(zh_dir, draft)
    _write_both(en_dir, draft)

    report = build_join_qa_report({
        "draft_path": str(zh_dir / "draft_content.json"),
        "draft_dir_en_branch": str(en_dir),
    })
    assert report["language_isolation"]["policy"] == "legacy"
    assert report["language_isolation"]["issue_count"] == 0
    assert report["passed"] is True


def test_qa_categories_v2_clean_passes() -> None:
    """v2 + 无残留:类别 passed(恒检查项不误伤正常任务)。"""
    categories, _, gate_failed = build_qa_categories(
        _qa_state("v2"), zh_draft=None, en_draft={"tracks": []}, deep_issues=[]
    )
    assert categories["voice_language_isolation"]["status"] == CAT_PASSED
    assert gate_failed is False


# ---------------------------------------------------------------------------
# 2026-11-17 P1:QA 消费 subtitle_translation_status(stub 不冒充成功)
# ---------------------------------------------------------------------------
def test_subtitles_en_stub_status_blocks() -> None:
    """translation_status=stub → subtitles_en 类别 failed(required 默认阻断)。"""
    state = _qa_state("v2")
    state["subtitle_translation_status"] = "stub"
    state["subtitle_translator_id"] = "mock"
    en_draft = {"tracks": [], "materials": {"texts": [{"id": "text-en-1"}]}}
    categories, new_issues, gate_failed = build_qa_categories(
        state, zh_draft=None, en_draft=en_draft, deep_issues=[]
    )
    assert categories["subtitles_en"]["status"] == CAT_FAILED
    assert any("翻译状态非真实" in i["detail"] for i in new_issues)
    assert gate_failed is True


def test_subtitles_en_real_status_passes() -> None:
    """translation_status=real + 有英文字幕 → 类别 passed(不误伤)。"""
    state = _qa_state("v2")
    state["subtitle_translation_status"] = "real"
    state["subtitle_translator_id"] = "mcode-cli"
    en_draft = {"tracks": [], "materials": {"texts": [{"id": "text-en-1"}]}}
    categories, _, gate_failed = build_qa_categories(
        state, zh_draft=None, en_draft=en_draft, deep_issues=[]
    )
    assert categories["subtitles_en"]["status"] == CAT_PASSED
