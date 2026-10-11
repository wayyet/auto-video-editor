"""节点 12 关卡②单测 — 结构对齐节点 6。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nodes.node_12_human_add_bgm import (
    _build_interrupt_payload,
    _post_resume,
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
    """K2=B 幂等:重放不会叠出重复音轨(整体替换,不是 append)。"""
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