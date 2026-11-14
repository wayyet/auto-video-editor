"""nodes/_auto_bgm.apply_auto_bgm_track 单测(20261114 阶段4)。

覆盖:happy/幂等重放/换曲替换/人工轨保留/非原生拒绝/缓存缺失跳过/
循环铺轨/时长不延长/事务式回滚(dangling refs 时原 draft 不变)。
"""
from __future__ import annotations

import pytest

from draft_ops.draft_integrity import check_track_references
from draft_ops.jianying_registry import compute_draft_duration_us
from nodes import _auto_bgm
from nodes._auto_bgm import AUTO_BGM_TRACK_NAME, apply_auto_bgm_track

_MUSIC_ID = "7553871402870491155"
_MUSIC_DUR_US = 190_766_666  # 旅途的风景 190.77s


def _library_entry(
    tmp_path,
    music_id: str = _MUSIC_ID,
    duration_us: int = _MUSIC_DUR_US,
    name: str = "旅途的风景",
    cache_exists: bool = True,
) -> dict:
    cache = tmp_path / "cache" / f"{music_id}.mp3"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache_exists:
        cache.write_bytes(b"x" * 2048)
    raw = {
        "id": f"MAT-{music_id}",
        "type": "music",
        "music_id": music_id,
        "name": name,
        "duration": duration_us,
        "category_id": "7405872200315882277",
        "category_name": "会员热榜",
        "request_id": f"REQ_{music_id}",
        "path": str(cache),
    }
    return {
        "bgm_id": music_id,
        "music_id": music_id,
        "name": name,
        "duration_us": duration_us,
        "cache_path": str(cache) if cache_exists else str(cache),
        "raw_material": raw,
    }


def _selection(music_id: str = _MUSIC_ID) -> dict:
    return {
        "bgm_ref": music_id,
        "music_id": music_id,
        "beats": [1000, 2000],
        "duration_ms": _MUSIC_DUR_US // 1000,
    }


def _native_draft(video_end_us: int = 5_000_000, extra_audio_track: dict | None = None) -> dict:
    half = video_end_us // 2
    tracks: list[dict] = [
        {
            "type": "video",
            "id": "track-video-1",
            "segments": [
                {"id": "seg-v1", "material_id": "m1",
                 "target_timerange": {"start": 0, "duration": half}},
                {"id": "seg-v2", "material_id": "m1",
                 "target_timerange": {"start": half, "duration": video_end_us - half}},
            ],
        }
    ]
    if extra_audio_track is not None:
        tracks.append(extra_audio_track)
    return {
        "new_version": "110.0.0",
        "duration": video_end_us,
        "materials": {
            "videos": [{"id": "m1", "type": "video", "path": "x.mp4"}],
            "audios": [],
            "speeds": [],
            "sound_channel_mappings": [],
            "vocal_separations": [],
        },
        "tracks": tracks,
    }


def _bgm_tracks(draft: dict) -> list[dict]:
    return [
        t for t in draft.get("tracks") or []
        if t.get("type") == "audio" and t.get("name") == AUTO_BGM_TRACK_NAME
    ]


def test_apply_happy_path_single_segment(tmp_path) -> None:
    """音乐(190s) > 视频(5s):单段铺满,素材字段保留,引用零悬空。"""
    draft = _native_draft()
    snapshot = str(draft)
    res = apply_auto_bgm_track(
        draft=draft, selection=_selection(), library_entry=_library_entry(tmp_path), job_id="job1"
    )
    assert res["applied"] is True and res["draft"] is not None
    # 原 draft 零污染
    assert str(draft) == snapshot
    assert len(draft["tracks"]) == 1 and draft["materials"]["audios"] == []

    cand = res["draft"]
    tracks = _bgm_tracks(cand)
    assert len(tracks) == 1
    track = tracks[0]
    assert len(track) == 7 and track["type"] == "audio" and track["name"] == "BGM"
    assert len(track["segments"]) == 1
    seg = track["segments"][0]
    assert seg["target_timerange"] == {"start": 0, "duration": 5_000_000}
    assert seg["volume"] == pytest.approx(0.35)
    mats = cand["materials"]
    assert len(mats["audios"]) == 1
    mat = mats["audios"][0]
    assert mat["type"] == "music" and mat["music_id"] == _MUSIC_ID
    assert mat["duration"] == _MUSIC_DUR_US  # 原始时长保留
    assert mat["request_id"] == f"REQ_{_MUSIC_ID}"
    assert mat["path"].endswith(f"{_MUSIC_ID}.mp3")  # 当前设备缓存路径
    assert check_track_references(cand, "zh") == []
    assert res["status_notes"] and "旅途的风景" in res["status_notes"][0]


def test_apply_idempotent_replay(tmp_path) -> None:
    """同 selection+job 重放:不重复追加轨/素材,ID 稳定。"""
    res1 = apply_auto_bgm_track(
        draft=_native_draft(), selection=_selection(), library_entry=_library_entry(tmp_path), job_id="job1"
    )
    res2 = apply_auto_bgm_track(
        draft=res1["draft"], selection=_selection(), library_entry=_library_entry(tmp_path), job_id="job1"
    )
    assert res2["applied"] is True
    assert res2["track_id"] == res1["track_id"]
    cand = res2["draft"]
    assert len(_bgm_tracks(cand)) == 1
    assert len(cand["materials"]["audios"]) == 1
    assert len(cand["materials"]["speeds"]) == 1
    assert len(cand["materials"]["sound_channel_mappings"]) == 1
    assert len(cand["materials"]["vocal_separations"]) == 1


def test_apply_music_change_replaces_old(tmp_path) -> None:
    """换曲重放:旧自动轨与孤立素材被清理,不残留。"""
    entry_a = _library_entry(tmp_path, music_id=_MUSIC_ID)
    entry_b = _library_entry(tmp_path, music_id="7546563445158332454", name="Island Sway 3")
    res1 = apply_auto_bgm_track(
        draft=_native_draft(), selection=_selection(), library_entry=entry_a, job_id="job1"
    )
    res2 = apply_auto_bgm_track(
        draft=res1["draft"], selection=_selection("7546563445158332454"), library_entry=entry_b, job_id="job1"
    )
    assert res2["applied"] is True
    cand = res2["draft"]
    tracks = _bgm_tracks(cand)
    assert len(tracks) == 1
    mat = cand["materials"]["audios"][0]
    assert mat["music_id"] == "7546563445158332454"
    assert check_track_references(cand, "zh") == []


def test_apply_preserves_human_audio_track(tmp_path) -> None:
    """人工添加的音轨(name="")不被清除,自动轨并存。"""
    human = {
        "type": "audio", "id": "human-track", "name": "",
        "segments": [{"id": "human-seg", "material_id": "m1",
                      "target_timerange": {"start": 0, "duration": 5_000_000}}],
    }
    draft = _native_draft(extra_audio_track=human)
    res = apply_auto_bgm_track(
        draft=draft, selection=_selection(), library_entry=_library_entry(tmp_path), job_id="job1"
    )
    cand = res["draft"]
    assert any(t.get("id") == "human-track" for t in cand["tracks"])
    assert len(_bgm_tracks(cand)) == 1


def test_apply_rejects_non_native_draft(tmp_path) -> None:
    draft = _native_draft()
    draft.pop("new_version")
    res = apply_auto_bgm_track(
        draft=draft, selection=_selection(), library_entry=_library_entry(tmp_path)
    )
    assert res["applied"] is False and res["draft"] is None
    assert any("原生" in e for e in res["errors"])


def test_apply_skips_when_cache_missing(tmp_path) -> None:
    entry = _library_entry(tmp_path, cache_exists=False)
    res = apply_auto_bgm_track(draft=_native_draft(), selection=_selection(), library_entry=entry)
    assert res["applied"] is False
    assert any("缓存" in e for e in res["errors"])


def test_apply_skips_without_music_id(tmp_path) -> None:
    res = apply_auto_bgm_track(draft=_native_draft(), selection={}, library_entry=_library_entry(tmp_path))
    assert res["applied"] is False
    assert any("music_id" in e for e in res["errors"])


def test_apply_loop_fill_when_music_shorter(tmp_path) -> None:
    """短音乐循环铺轨(用户拍板):2s 音乐铺 5s 视频 → 2s+2s+1s 三段。"""
    entry = _library_entry(tmp_path, duration_us=2_000_000)
    res = apply_auto_bgm_track(draft=_native_draft(), selection=_selection(), library_entry=entry)
    assert res["applied"] is True
    segs = _bgm_tracks(res["draft"])[0]["segments"]
    assert [s["target_timerange"]["duration"] for s in segs] == [2_000_000, 2_000_000, 1_000_000]
    assert [s["target_timerange"]["start"] for s in segs] == [0, 2_000_000, 4_000_000]
    assert res["draft"]["materials"]["speeds"] and len(res["draft"]["materials"]["speeds"]) == 3
    assert check_track_references(res["draft"], "zh") == []


def test_apply_does_not_extend_draft_duration(tmp_path) -> None:
    res = apply_auto_bgm_track(
        draft=_native_draft(), selection=_selection(), library_entry=_library_entry(tmp_path), job_id="j"
    )
    assert compute_draft_duration_us(res["draft"]) == 5_000_000


def test_apply_transactional_rollback_on_dangling_refs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """伴随素材引用悬空 → 放弃注入,原 draft 保持完全不变。"""
    draft = _native_draft()
    snapshot = str(draft)

    def _ghost_scm(*, material_id: str, channel_type: str = "none") -> dict:
        return {"audio_channel_mapping": 0, "id": f"ghost-{material_id}",
                "is_config_open": False, "type": channel_type}

    monkeypatch.setattr(_auto_bgm, "build_native_sound_channel_mapping_material", _ghost_scm)
    res = apply_auto_bgm_track(
        draft=draft, selection=_selection(), library_entry=_library_entry(tmp_path), job_id="job1"
    )
    assert res["applied"] is False and res["draft"] is None
    assert any("悬空" in e for e in res["errors"])
    assert str(draft) == snapshot  # 事务式:零污染
