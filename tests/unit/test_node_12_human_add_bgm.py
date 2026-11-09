"""节点 12 关卡②单测 — 结构对齐节点 6。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nodes.node_12_human_add_bgm import (
    _build_interrupt_payload,
    _post_resume,
    _zh_bgm_missing,
    mirror_bgm_to_en_draft,
)


def _state(**overrides: Any) -> dict:
    base = {
        "session_id": "t1",
        "draft_path": "C:/tmp/draft.json",
        "status_log": [],
        "error_log": [],
    }
    base.update(overrides)
    return base


def test_interrupt_payload_structure() -> None:
    payload = _build_interrupt_payload(_state())
    # Week 5:统一为 "②",旧值保留在 legacy_id
    assert payload["checkpoint"] == "②"
    assert payload["legacy_id"] == "checkpoint2_add_bgm"
    assert payload["step"] == 12
    assert payload["draft_path"] == "C:/tmp/draft.json"
    assert "BGM" in payload["instructions"]


# ---------------------------------------------------------------------------
# 2026-11-10:关卡② BGM 落轨校验 —— required_features.bgm 必选时,resume 后
# 校验中文草稿确有非配音音轨,缺失 → 带告警再次挂起(console-5aa2606e546b
# 复盘:此前确认即放行,缺口拖到交付 QA 才兜住)
# ---------------------------------------------------------------------------
def test_zh_bgm_missing_with_bgm_track_false() -> None:
    """有人工 BGM 音轨 → 不缺。"""
    assert _zh_bgm_missing(_zh_draft_with_bgm()) is False


def test_zh_bgm_missing_voiceover_only_true() -> None:
    """只有配音轨(按轨名豁免,与交付 QA 同口径)→ 视为 BGM 缺失。"""
    draft = _en_draft_without_audio()
    draft["tracks"].append({
        "type": "audio",
        "id": "vo-zh",
        "name": "Voiceover_zh",
        "segments": [{"id": "vo-seg", "material_id": "m1"}],
    })
    assert _zh_bgm_missing(draft) is True


def test_zh_bgm_missing_no_tracks_or_none_true() -> None:
    """无音轨 / 草稿不可得(None)→ 缺失。"""
    assert _zh_bgm_missing(_en_draft_without_audio()) is True
    assert _zh_bgm_missing(None) is True


def test_interrupt_payload_bgm_missing_variant() -> None:
    """bgm_missing 变体:extra_payload 带标记,指令文案点名问题。"""
    payload = _build_interrupt_payload(_state(), bgm_missing=True)
    assert payload["checkpoint"] == "②"  # resume 匹配键不变
    assert payload["bgm_missing"] is True
    assert "未检测到 BGM 音轨" in payload["instructions"]
    # 常规变体不带标记
    plain = _build_interrupt_payload(_state())
    assert "bgm_missing" not in plain


def test_post_resume_sends_notification_and_logs() -> None:
    """resume 后:发出通知 + 写 status_log + 设置 bgm_notified。"""
    calls: list[tuple] = []

    def fake_notifier(state: dict, checkpoint: str) -> None:
        calls.append((state.get("session_id"), checkpoint))

    state = _state()
    result = _post_resume(state, notifier=fake_notifier)

    assert len(calls) == 1
    assert calls[0] == ("t1", "checkpoint2")
    assert result["bgm_notified"] is True
    assert "checkpoint2_resumed" in result["status_log"]


def test_post_resume_preserves_existing_status_log() -> None:
    state = _state(status_log=["node_11_done"])
    result = _post_resume(state, notifier=lambda s, c: None)
    assert result["status_log"][0] == "node_11_done"
    assert "checkpoint2_resumed" in result["status_log"]


# ---------------------------------------------------------------------------
# K2=B:一次选曲、两份草稿 — resume 后把中文草稿的 BGM 镜像进英文草稿
# ---------------------------------------------------------------------------
def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _zh_draft_with_bgm() -> dict:
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": {
            "audios": [{"id": "audio-bgm-1", "name": "user_picked_bgm"}],
            "audio_fades": [],
        },
        "tracks": [
            {"type": "video", "segments": [
                {"id": "v1", "target_timerange": {"start": 0, "duration": 35_000_000}}]},
            {"type": "audio", "segments": [
                {"id": "audio-bgm-1", "name": "user_picked_bgm", "type": "audio_bgm",
                 "target_timerange": {"start": 0, "duration": 35_000_000}}]},
        ],
    }


def _en_draft_without_audio() -> dict:
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": {},
        "tracks": [{"type": "video", "segments": [
            {"id": "v1", "target_timerange": {"start": 0, "duration": 35_000_000}}]}],
    }


def test_mirror_bgm_copies_audio_track_to_en_draft(tmp_path: Path) -> None:
    """K2=B:中文草稿的 audio 轨 + audio material 整体复制到英文草稿。

    轨道名故意用 ``user_picked_bgm``(人工在剪映里拖的,不受控)—— 验证整轨复制
    不依赖 ``audio_bgm`` 这类命名(计划 §6 R3)。
    """
    zh = _write_json(tmp_path / "zh" / "draft_content.json", _zh_draft_with_bgm())
    en = _write_json(tmp_path / "en" / "draft_content.json", _en_draft_without_audio())

    notes = mirror_bgm_to_en_draft({"draft_path": str(zh), "draft_dir_en_branch": str(en.parent)})

    assert any(n.startswith("node_12_bgm_mirrored_en_done") for n in notes), notes
    en_draft = json.loads(en.read_text(encoding="utf-8"))
    audio_tracks = [t for t in en_draft["tracks"] if t["type"] == "audio"]
    assert len(audio_tracks) == 1
    assert audio_tracks[0]["segments"][0]["id"] == "audio-bgm-1"
    assert [m["id"] for m in en_draft["materials"]["audios"]] == ["audio-bgm-1"]


def test_mirror_bgm_is_idempotent(tmp_path: Path) -> None:
    """K2=B 幂等:重放不会叠出重复音轨(按角色合并:替换镜像轨;P3b 起英文配音轨另行保留)。"""
    zh = _write_json(tmp_path / "zh" / "draft_content.json", _zh_draft_with_bgm())
    en = _write_json(tmp_path / "en" / "draft_content.json", _en_draft_without_audio())
    state = {"draft_path": str(zh), "draft_dir_en_branch": str(en.parent)}

    mirror_bgm_to_en_draft(state)
    mirror_bgm_to_en_draft(state)

    en_draft = json.loads(en.read_text(encoding="utf-8"))
    assert len([t for t in en_draft["tracks"] if t["type"] == "audio"]) == 1
    assert len(en_draft["materials"]["audios"]) == 1


def test_mirror_bgm_degrades_when_zh_has_no_audio(tmp_path: Path) -> None:
    """人工没加 BGM → 记 error_log 降级通过,英文草稿不动。"""
    zh = _write_json(tmp_path / "zh" / "draft_content.json", _en_draft_without_audio())
    en = _write_json(tmp_path / "en" / "draft_content.json", _en_draft_without_audio())

    notes = mirror_bgm_to_en_draft({"draft_path": str(zh), "draft_dir_en_branch": str(en.parent)})

    assert any("无 audio track" in n for n in notes), notes
    en_draft = json.loads(en.read_text(encoding="utf-8"))
    assert not [t for t in en_draft["tracks"] if t["type"] == "audio"]


def test_mirror_bgm_degrades_when_en_draft_missing(tmp_path: Path) -> None:
    """英文草稿不存在 → 记 error_log 降级,不抛异常。"""
    zh = _write_json(tmp_path / "zh" / "draft_content.json", _zh_draft_with_bgm())

    notes = mirror_bgm_to_en_draft({
        "draft_path": str(zh), "draft_dir_en_branch": str(tmp_path / "nope")})

    assert any("英文草稿不存在" in n for n in notes), notes


def test_post_resume_mirrors_bgm_and_keeps_legacy_contract(tmp_path: Path) -> None:
    """节点包装:resume 后既走老收尾(通知 + bgm_notified + checkpoint2_resumed),
    也把 BGM 镜像到英文草稿 —— 旧契约字段一个都不少。"""
    zh = _write_json(tmp_path / "zh" / "draft_content.json", _zh_draft_with_bgm())
    en = _write_json(tmp_path / "en" / "draft_content.json", _en_draft_without_audio())

    result = _post_resume(
        {"session_id": "t1", "draft_path": str(zh), "draft_dir_en_branch": str(en.parent),
         "status_log": [], "error_log": []},
        notifier=lambda s, c: None,
    )

    assert result["bgm_notified"] is True
    assert "checkpoint2_resumed" in result["status_log"]
    assert any(t.startswith("node_12_bgm_mirrored_en_done") for t in result["status_log"])
    en_draft = json.loads(en.read_text(encoding="utf-8"))
    assert [t for t in en_draft["tracks"] if t["type"] == "audio"]


# ---------------------------------------------------------------------------
# P3b:按角色合并 —— 保留 node_17 写入的英文配音轨
# ---------------------------------------------------------------------------
def _en_draft_with_voiceover() -> dict:
    draft = _en_draft_without_audio()
    draft["materials"] = {
        "audios": [{"id": "en-dub-1", "name": "en_voiceover", "path": "C:/tts/en.wav"}],
        "audio_fades": [],
    }
    draft["tracks"].append({
        "type": "audio",
        "id": "track-audio-en-dub",
        "name": "en_voiceover",
        "segments": [{
            "id": "segment-en-dub-1", "material_id": "en-dub-1", "name": "en_voiceover",
            "target_timerange": {"start": 0, "duration": 30_000_000},
        }],
    })
    return draft


def test_mirror_keeps_en_voiceover_track(tmp_path: Path) -> None:
    """P3b 验收:英文草稿里 node_17 写的配音轨,经过 node_12 镜像后仍在。"""
    zh = _write_json(tmp_path / "zh" / "draft_content.json", _zh_draft_with_bgm())
    en = _write_json(tmp_path / "en" / "draft_content.json", _en_draft_with_voiceover())

    notes = mirror_bgm_to_en_draft(
        {"draft_path": str(zh), "draft_dir_en_branch": str(en.parent)}
    )

    assert any(n.startswith("node_12_bgm_mirrored_en_done") for n in notes), notes
    en_draft = json.loads(en.read_text(encoding="utf-8"))
    audio_tracks = [t for t in en_draft["tracks"] if t["type"] == "audio"]
    assert len(audio_tracks) == 2
    # 配音轨原样保留
    voiceover = next(t for t in audio_tracks if t.get("name") == "en_voiceover")
    assert voiceover["segments"][0]["id"] == "segment-en-dub-1"
    assert voiceover["segments"][0]["target_timerange"]["duration"] == 30_000_000
    # BGM 镜像轨也在
    bgm = next(t for t in audio_tracks if t.get("name") != "en_voiceover")
    assert bgm["segments"][0]["id"] == "audio-bgm-1"
    # materials 两边素材都在
    material_ids = {m["id"] for m in en_draft["materials"]["audios"]}
    assert material_ids == {"en-dub-1", "audio-bgm-1"}


def test_mirror_idempotent_with_voiceover(tmp_path: Path) -> None:
    """带配音轨时重放:配音轨不丢,镜像轨不翻倍。"""
    zh = _write_json(tmp_path / "zh" / "draft_content.json", _zh_draft_with_bgm())
    en = _write_json(tmp_path / "en" / "draft_content.json", _en_draft_with_voiceover())
    state = {"draft_path": str(zh), "draft_dir_en_branch": str(en.parent)}

    mirror_bgm_to_en_draft(state)
    mirror_bgm_to_en_draft(state)

    en_draft = json.loads(en.read_text(encoding="utf-8"))
    assert len([t for t in en_draft["tracks"] if t["type"] == "audio"]) == 2
    assert len(en_draft["materials"]["audios"]) == 2


def test_mirror_zh_no_audio_keeps_voiceover(tmp_path: Path) -> None:
    """P3b 后的新降级语义:zh 没加 BGM 但 en 有配音轨 → en 不动,配音轨保留。"""
    zh = _write_json(tmp_path / "zh" / "draft_content.json", _en_draft_without_audio())
    en = _write_json(tmp_path / "en" / "draft_content.json", _en_draft_with_voiceover())

    notes = mirror_bgm_to_en_draft(
        {"draft_path": str(zh), "draft_dir_en_branch": str(en.parent)}
    )

    assert any("无 audio track" in n for n in notes), notes
    en_draft = json.loads(en.read_text(encoding="utf-8"))
    audio_tracks = [t for t in en_draft["tracks"] if t["type"] == "audio"]
    assert len(audio_tracks) == 1
    assert audio_tracks[0].get("name") == "en_voiceover"


# ---------------------------------------------------------------------------
# 阶段 5:伴随素材镜像(阶段 0 基线实证 EN 侧悬空 companion ×4 的修复)
# ---------------------------------------------------------------------------
def _zh_draft_with_bgm_and_companions() -> dict:
    draft = _zh_draft_with_bgm()
    # 剪映客户端生成的 BGM 段带 4 伴随 + fade 引用;对应素材在 zh materials 里
    draft["materials"]["audios"] = [{"id": "audio-bgm-1", "name": "user_picked_bgm"}]
    draft["materials"]["speeds"] = [{"id": "speed-1", "mode": 0, "speed": 1.0, "type": "speed"}]
    draft["materials"]["canvases"] = [{"id": "canvas-1", "type": "canvas"}]
    draft["materials"]["sound_channel_mappings"] = [{"id": "scm-1", "type": "sound_channel_mapping"}]
    draft["materials"]["vocal_separations"] = [{"id": "vocal-1", "type": "vocal_separation"}]
    draft["materials"]["audio_fades"] = [{
        "id": "audio-fade-audio-bgm-1", "type": "audio_fade",
        "fade_in_duration": 2_000_000, "fade_out_duration": 3_000_000,
    }]
    draft["tracks"][1]["segments"][0]["extra_material_refs"] = [
        "speed-1", "canvas-1", "scm-1", "vocal-1", "audio-fade-audio-bgm-1",
    ]
    return draft


def test_mirror_copies_companion_materials(tmp_path: Path) -> None:
    """镜像段引用的伴随素材(speeds/canvases/scm/vocal/audio_fades)一并复制到英文草稿。"""
    from draft_ops.draft_integrity import check_track_references

    zh = _write_json(tmp_path / "zh" / "draft_content.json", _zh_draft_with_bgm_and_companions())
    en = _write_json(tmp_path / "en" / "draft_content.json", _en_draft_without_audio())

    notes = mirror_bgm_to_en_draft(
        {"draft_path": str(zh), "draft_dir_en_branch": str(en.parent)}
    )

    assert any(n.startswith("node_12_bgm_mirrored_en_done") for n in notes), notes
    en_draft = json.loads(en.read_text(encoding="utf-8"))
    materials = en_draft["materials"]
    for bucket, item_id in (
        ("speeds", "speed-1"),
        ("canvases", "canvas-1"),
        ("sound_channel_mappings", "scm-1"),
        ("vocal_separations", "vocal-1"),
        ("audio_fades", "audio-fade-audio-bgm-1"),
    ):
        assert any(m["id"] == item_id for m in materials.get(bucket, [])), (
            f"{bucket} 应镜像 {item_id}"
        )
    # 镜像后引用完整性:零悬空(阶段 0 基线的 ×4 悬空不再出现)
    assert check_track_references(en_draft, "en") == []


def test_mirror_strips_unmirrorable_refs(tmp_path: Path) -> None:
    """镜像段引用了未复制桶的素材 → 剥离该 ref + 告警(防悬空)。"""
    zh = _write_json(tmp_path / "zh" / "draft_content.json", _zh_draft_with_bgm_and_companions())
    zh_draft = json.loads(zh.read_text(encoding="utf-8"))
    zh_draft["tracks"][1]["segments"][0]["extra_material_refs"].append("ghost-companion")
    zh.write_text(json.dumps(zh_draft, ensure_ascii=False), encoding="utf-8")
    en = _write_json(tmp_path / "en" / "draft_content.json", _en_draft_without_audio())

    notes = mirror_bgm_to_en_draft(
        {"draft_path": str(zh), "draft_dir_en_branch": str(en.parent)}
    )

    assert any("无法镜像的 companion ref" in n for n in notes), notes
    en_draft = json.loads(en.read_text(encoding="utf-8"))
    mirrored_seg = next(
        t for t in en_draft["tracks"] if t["type"] == "audio"
    )["segments"][0]
    assert "ghost-companion" not in mirrored_seg.get("extra_material_refs", [])


def test_post_resume_records_manual_draft_sha(tmp_path: Path) -> None:
    """阶段 5 任务 1:resume 时记录人工修改后草稿 SHA-256(bgm_manual_draft_sha)。"""
    zh = _write_json(tmp_path / "zh" / "draft_content.json", _zh_draft_with_bgm())
    en = _write_json(tmp_path / "en" / "draft_content.json", _en_draft_without_audio())

    result = _post_resume(
        {"session_id": "t1", "draft_path": str(zh), "draft_dir_en_branch": str(en.parent),
         "status_log": [], "error_log": []},
        notifier=lambda s, c: None,
    )

    sha = result.get("bgm_manual_draft_sha")
    assert sha, "应记录人工修改后草稿 SHA"
    # fixture 只写了 draft_content.json:缺哪个文件就少一个键(hash_draft_files 契约)
    assert "draft_content.json" in sha["zh"]
    assert all(len(v) == 64 for v in sha["zh"].values())  # sha256 hex
    assert "en_pre_mirror" in sha
    assert "node_12_manual_draft_sha_recorded" in result["status_log"]