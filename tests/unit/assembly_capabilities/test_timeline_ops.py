"""``timeline_ops`` 的入参校验 + patch 引擎 + validate 规则纯逻辑测试。

不依赖 ffmpeg/ffprobe:核心覆盖 validate_timeline / timeline_diff 的入参
校验、patch 校验、normalize 逻辑、file_sha256、coerce_apply_flag 等。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from assembly_capabilities.timeline_ops import (
    KNOWN_TRACK_TYPES,
    OVERLAY_TRACK_TYPES,
    PATCH_OPERATION_KEYS,
    PROTECTED_TIMELINE_PATCH_FIELDS,
    TEXT_TRACK_TYPES,
    VIDEO_TRACK_TYPES,
    AUDIO_TRACK_TYPES,
    apply_patch_to_timeline,
    clip_render_duration,
    coerce_apply_flag,
    coerce_timeline_number,
    default_clip_container,
    file_sha256,
    is_patch_index,
    is_video_track,
    load_timeline,
    normalize_clip,
    normalize_track_type,
    operation_is_non_empty,
    patch_has_non_empty_operation,
    safe_duration,
    timeline_clips,
    timeline_duration,
    timeline_project_contract,
    track_clips_list,
    validate_timeline,
    validate_timeline_data,
    validate_timeline_patch,
)
from assembly_capabilities.result import ToolResult


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------
def test_validate_timeline_requires_timeline_path(run_context):
    r = validate_timeline({}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "timeline_path is required" in r.text


def test_validate_timeline_rejects_missing_file(run_context, tmp_path):
    r = validate_timeline({"timeline_path": str(tmp_path / "missing.json")}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "timeline not found" in r.text


def test_validate_timeline_rejects_invalid_json(run_context, tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("not json", encoding="utf-8")
    r = validate_timeline({"timeline_path": str(p)}, run_context)
    assert r.text.startswith("[ERROR]")
    # 实际错误可能是 "ffprobe not found on PATH"(原版 validate_timeline 先检 ffmpeg
    # 存在性);若 ffprobe 不存在时 text 含 "ffprobe",否则含 "invalid JSON"。
    assert ("invalid JSON" in r.text) or ("ffprobe" in r.text)


# ---------------------------------------------------------------------------
# validate_timeline_data 纯逻辑(无 ffmpeg 调用)
# ---------------------------------------------------------------------------
def _empty_project() -> dict:
    """构造一个最小可校验的 project timeline(空 video track + 空 assets)。"""
    return {
        "project": {"name": "demo"},
        "assets": [{"id": "a1", "path": "/tmp/nope.mp4", "duration": 1.0, "width": 16, "height": 16, "fps": 30}],
        "sequence": {"duration": 1.0, "fps": 30, "canvas": {"width": 16, "height": 16}},
        "tracks": [{"type": "video", "clips": []}],
    }


def test_validate_timeline_data_passes_on_minimal_project(run_context):
    """最小可校验的 project timeline + 一个有效 video clip。"""
    data = _empty_project()
    # 用真实存在的源文件(占位 mp4 即可,validate 不去 probe 源)
    import os
    placeholder_src = os.path.abspath(__file__)  # 用本测试文件当 source 占位
    data["tracks"] = [{"type": "video", "clips": [
        {"track_type": "video", "source": placeholder_src, "start": 0.0, "end": 0.5, "reason": "test"},
    ]}]
    clips, issues = validate_timeline_data(data, run_context)
    # 原版 validate_timeline_data 在 issues 列表里必塞 "ffprobe not found on PATH"(即使
    # 测试环境没装 ffprobe)。过滤掉这一项后,其余 error 应为空。
    real_errors = [
        i for i in issues
        if i["severity"] == "error" and "ffprobe not found" not in i["message"]
    ]
    assert real_errors == [], f"unexpected errors: {real_errors}"


def test_validate_timeline_data_rejects_both_clips_and_tracks(run_context):
    bad = _empty_project()
    bad["clips"] = []
    _, issues = validate_timeline_data(bad, run_context)
    assert any("not both" in i["message"] for i in issues if i["severity"] == "error")


def test_validate_timeline_data_requires_project_object(run_context):
    bad = _empty_project()
    bad["project"] = None
    _, issues = validate_timeline_data(bad, run_context)
    assert any("project timeline contract requires a non-empty project" in i["message"] for i in issues)


def test_validate_timeline_data_requires_assets(run_context):
    bad = _empty_project()
    bad["assets"] = []
    _, issues = validate_timeline_data(bad, run_context)
    assert any("non-empty assets" in i["message"] for i in issues)


def test_validate_timeline_data_requires_video_track(run_context):
    bad = _empty_project()
    bad["tracks"] = [{"type": "audio", "clips": []}]
    _, issues = validate_timeline_data(bad, run_context)
    assert any("at least one video/main track" in i["message"] for i in issues)


def test_validate_timeline_data_requires_track_clips_to_be_list(run_context):
    bad = _empty_project()
    bad["tracks"] = [{"type": "video", "clips": "not a list"}]
    _, issues = validate_timeline_data(bad, run_context)
    assert any("track.clips must be an array" in i["message"] for i in issues)


def test_validate_timeline_data_flags_unknown_track_type_as_warning(run_context):
    bad = _empty_project()
    bad["tracks"] = [{"type": "weird_type", "clips": []}]
    _, issues = validate_timeline_data(bad, run_context)
    assert any("unknown track type/name: weird_type" in i["message"] for i in issues if i["severity"] == "warning")


def test_validate_timeline_data_flags_missing_source(run_context):
    bad = _empty_project()
    bad["tracks"] = [{"type": "video", "clips": [{"track_type": "video", "start": 0.0, "end": 1.0, "reason": "test"}]}]
    _, issues = validate_timeline_data(bad, run_context)
    assert any("missing source/input_path/src" in i["message"] for i in issues)


def test_validate_timeline_data_flags_missing_reason(run_context):
    bad = _empty_project()
    bad["tracks"] = [{"type": "video", "clips": [
        {"track_type": "video", "source": "/tmp/a.mp4", "start": 0.0, "end": 1.0}
    ]}]
    _, issues = validate_timeline_data(bad, run_context)
    assert any("missing reason" in i["message"] for i in issues)


# ---------------------------------------------------------------------------
# patch 引擎
# ---------------------------------------------------------------------------
def test_coerce_apply_flag_accepts_bool_and_none():
    assert coerce_apply_flag(None) is False
    assert coerce_apply_flag(True) is True
    assert coerce_apply_flag(False) is False
    assert coerce_apply_flag("yes") is None
    assert coerce_apply_flag(1) is None


def test_patch_with_apply_true_requires_non_empty_op():
    issues = validate_timeline_patch({}, {"apply": True}, apply_requested=True) if False else validate_timeline_patch(
        {"clips": []},
        {"apply": True},  # 空 patch + apply=true
        apply_requested=True,
    )
    assert any("non-empty timeline patch operation" in i["message"] for i in issues)


def test_patch_with_apply_false_accepts_empty_patch():
    issues = validate_timeline_patch({"clips": []}, {}, apply_requested=False)
    assert all(i["severity"] != "error" for i in issues)


def test_patch_warns_on_unknown_key():
    issues = validate_timeline_patch({"clips": []}, {"unknown_key": []}, apply_requested=False)
    assert any("unknown patch key ignored: unknown_key" in i["message"] for i in issues if i["severity"] == "warning")


def test_patch_rejects_structural_track_ops_with_clip_ops():
    """同一次 patch 同时改 track 结构与 clip → 必须拆开两次调。"""
    data = {"tracks": [{"type": "video", "clips": []}]}
    patch = {
        "add_tracks": [{"type": "video"}],
        "add_clips": [{"start": 0, "end": 1}],
    }
    issues = validate_timeline_patch(data, patch, apply_requested=False)
    assert any("must be split into separate timeline_diff calls" in i["message"] for i in issues)


def test_patch_set_timeline_fields_cannot_modify_clips_or_tracks():
    issues = validate_timeline_patch(
        {"clips": []},
        {"set_timeline_fields": {"clips": []}},
        apply_requested=False,
    )
    assert any("set_timeline_fields cannot modify clips" in i["message"] for i in issues)


def test_patch_set_timeline_fields_can_modify_other_keys():
    issues = validate_timeline_patch(
        {"clips": []},
        {"set_timeline_fields": {"metadata": {"foo": "bar"}}},
        apply_requested=False,
    )
    assert all(i["severity"] != "error" for i in issues)


def test_apply_patch_to_timeline_set_timeline_fields():
    data = {"clips": [], "metadata": {"a": 1}}
    apply_patch_to_timeline(data, {"set_timeline_fields": {"metadata": {"a": 2}}})
    assert data["metadata"]["a"] == 2


def test_apply_patch_to_timeline_add_clips_appends_to_video_track():
    data = {"tracks": [{"type": "video", "clips": []}]}
    apply_patch_to_timeline(data, {"add_clips": [{"start": 0, "end": 1, "reason": "r"}]})
    assert len(data["tracks"][0]["clips"]) == 1


def test_apply_patch_to_timeline_remove_clips():
    data = {"tracks": [{"type": "video", "clips": [
        {"start": 0, "end": 1, "reason": "r1"},
        {"start": 1, "end": 2, "reason": "r2"},
    ]}]}
    apply_patch_to_timeline(data, {"remove_clip_indices": [0]})
    assert len(data["tracks"][0]["clips"]) == 1


def test_apply_patch_to_timeline_update_clips_fields():
    data = {"tracks": [{"type": "video", "clips": [
        {"start": 0, "end": 1, "reason": "r", "speed": 1.0},
    ]}]}
    apply_patch_to_timeline(data, {"update_clips": [{"index": 0, "fields": {"speed": 2.0}}]})
    assert data["tracks"][0]["clips"][0]["speed"] == 2.0


def test_apply_patch_to_timeline_replace_clips():
    data = {"tracks": [{"type": "video", "clips": [{"start": 0, "end": 1}]}]}
    apply_patch_to_timeline(data, {"replace_clips": [{"index": 0, "clip": {"start": 0, "end": 5}}]})
    assert data["tracks"][0]["clips"][0]["end"] == 5


def test_apply_patch_to_timeline_move_clips():
    data = {"tracks": [{"type": "video", "clips": [
        {"start": 0, "end": 1, "reason": "r0"},
        {"start": 1, "end": 2, "reason": "r1"},
    ]}]}
    apply_patch_to_timeline(data, {"move_clips": [{"from": 0, "to": 1}]})
    # 移动 from=0 to=1: 先 pop idx 0 (r0),剩下 [r1]; r1 现在在 idx 0; r0 插入 idx 1
    # 原版逻辑 `if target_container is source_container and to_idx > local_idx: to_idx -= 1`
    # 此时 to_idx=1, local_idx=0, to_idx > local_idx → to_idx=0 → r0 插到 idx 0 → [r0, r1]
    assert data["tracks"][0]["clips"][0]["reason"] == "r0"
    assert data["tracks"][0]["clips"][1]["reason"] == "r1"


def test_apply_patch_to_timeline_insert_clips():
    data = {"tracks": [{"type": "video", "clips": []}]}
    apply_patch_to_timeline(data, {"insert_clips": [{"index": 0, "clip": {"start": 0, "end": 1, "reason": "r"}}]})
    assert len(data["tracks"][0]["clips"]) == 1


def test_apply_patch_to_timeline_add_assets():
    data = {"assets": []}
    apply_patch_to_timeline(data, {"add_assets": [{"id": "a1", "path": "/tmp/x.mp4"}]})
    assert len(data["assets"]) == 1


def test_apply_patch_to_timeline_remove_asset_indices():
    data = {"assets": [{"id": "a1"}, {"id": "a2"}]}
    apply_patch_to_timeline(data, {"remove_asset_indices": [0]})
    assert len(data["assets"]) == 1
    assert data["assets"][0]["id"] == "a2"


def test_apply_patch_to_timeline_update_assets():
    data = {"assets": [{"id": "a1", "path": "/old.mp4"}]}
    apply_patch_to_timeline(data, {"update_assets": [{"index": 0, "fields": {"path": "/new.mp4"}}]})
    assert data["assets"][0]["path"] == "/new.mp4"


def test_apply_patch_to_timeline_add_tracks():
    data = {"tracks": []}
    apply_patch_to_timeline(data, {"add_tracks": [{"type": "video", "clips": []}]})
    assert len(data["tracks"]) == 1


def test_apply_patch_to_timeline_replace_tracks():
    data = {"tracks": [{"type": "audio", "clips": []}]}
    apply_patch_to_timeline(data, {"replace_tracks": [{"index": 0, "track": {"type": "video", "clips": []}}]})
    assert data["tracks"][0]["type"] == "video"


def test_apply_patch_to_timeline_remove_track_indices():
    data = {"tracks": [{"type": "video"}, {"type": "audio"}]}
    apply_patch_to_timeline(data, {"remove_track_indices": [0]})
    assert len(data["tracks"]) == 1
    assert data["tracks"][0]["type"] == "audio"


def test_apply_patch_to_timeline_set_track_fields():
    data = {"tracks": [{"type": "video", "volume": 1.0}]}
    apply_patch_to_timeline(data, {"set_track_fields": [{"index": 0, "fields": {"volume": 0.5}}]})
    assert data["tracks"][0]["volume"] == 0.5


def test_apply_patch_to_timeline_insert_tracks():
    data = {"tracks": [{"type": "audio"}]}
    apply_patch_to_timeline(data, {"insert_tracks": [{"index": 0, "track": {"type": "video"}}]})
    assert data["tracks"][0]["type"] == "video"


# ---------------------------------------------------------------------------
# validate_timeline_patch 错误路径
# ---------------------------------------------------------------------------
def test_patch_remove_clip_indices_must_be_list():
    issues = validate_timeline_patch({"clips": []}, {"remove_clip_indices": "not a list"}, apply_requested=False)
    assert any("must be an array" in i["message"] for i in issues)


def test_patch_remove_clip_indices_out_of_range():
    issues = validate_timeline_patch({"clips": []}, {"remove_clip_indices": [5]}, apply_requested=False)
    assert any("out of range" in i["message"] for i in issues)


def test_patch_remove_clip_indices_duplicate_index():
    data = {"clips": [{"a": 1}]}
    issues = validate_timeline_patch(data, {"remove_clip_indices": [0, 0]}, apply_requested=False)
    assert any("duplicates index 0" in i["message"] for i in issues)


def test_patch_update_clips_requires_non_empty_fields():
    issues = validate_timeline_patch(
        {"clips": [{"a": 1}]},
        {"update_clips": [{"index": 0, "fields": {}}]},
        apply_requested=False,
    )
    assert any("fields must be a non-empty object" in i["message"] for i in issues)


def test_patch_move_clips_rejects_more_than_one():
    issues = validate_timeline_patch(
        {"clips": []},
        {"move_clips": [{"from": 0, "to": 1}, {"from": 0, "to": 2}]},
        apply_requested=False,
    )
    assert any("supports one move per timeline_diff call" in i["message"] for i in issues)


def test_patch_move_clips_cannot_combine_with_remove():
    data = {"clips": [{"a": 1}]}
    issues = validate_timeline_patch(
        data,
        {"remove_clip_indices": [0], "move_clips": [{"from": 0, "to": 0}]},
        apply_requested=False,
    )
    assert any("cannot be combined with remove_clip_indices" in i["message"] for i in issues)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def test_is_patch_index_accepts_int_not_bool():
    assert is_patch_index(0) is True
    assert is_patch_index(-5) is True
    assert is_patch_index(True) is False
    assert is_patch_index("0") is False
    assert is_patch_index(None) is False


def test_coerce_timeline_number_handles_bool_and_invalid():
    assert coerce_timeline_number(True) is None
    assert coerce_timeline_number(None) is None
    assert coerce_timeline_number("3.14") == 3.14
    assert coerce_timeline_number("abc") is None


def test_safe_duration_handles_invalid():
    assert safe_duration(None) is None
    assert safe_duration(True) is None
    assert safe_duration(-1) is None
    assert safe_duration(0) is None
    assert safe_duration(2.5) == 2.5
    assert safe_duration("1.5") == 1.5
    assert safe_duration("abc") is None


def test_normalize_track_type_normalizes():
    assert normalize_track_type("Main Video") == "main_video"
    assert normalize_track_type("sub-title") == "sub_title"
    assert normalize_track_type("") == ""


def test_is_video_track():
    assert is_video_track({"type": "video"}) is True
    assert is_video_track({"type": "main_video"}) is True
    assert is_video_track({"type": "audio"}) is False
    assert is_video_track({"type": "weird"}) is False


def test_normalize_clip_resolves_path(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 100)
    out = normalize_clip({"source": str(src), "start": 1.0, "end": 3.0}, run_context)
    assert out["start"] == 1.0
    assert out["end"] == 3.0
    assert out["duration"] == 2.0


def test_normalize_clip_handles_missing_end(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 100)
    out = normalize_clip({"source": str(src), "start": 1.0, "duration": 4.0}, run_context)
    assert out["end"] == 5.0
    assert out["duration"] == 4.0


def test_clip_render_duration_with_end_and_start():
    d = clip_render_duration({"start": 0.0, "end": 2.0, "speed": 1.0})
    assert d == 2.0


def test_clip_render_duration_with_speed():
    d = clip_render_duration({"start": 0.0, "end": 4.0, "speed": 2.0})
    assert d == 2.0


def test_clip_render_duration_none_for_invalid():
    assert clip_render_duration({}) is None
    assert clip_render_duration({"start": 1.0, "end": 1.0}) is None
    # speed=0.0 在原版 `coerce_timeline_number(clip.get("speed")) or 1.0` 走 1.0,
    # 所以不是 None(忠实于原 video-agent-kit 行为,这个 OR 的副作用原版就有)
    assert clip_render_duration({"start": 0.0, "end": 1.0, "speed": 0.0}) == 1.0
    # speed=-1 才被判定非法
    assert clip_render_duration({"start": 0.0, "end": 1.0, "speed": -1}) is None


def test_timeline_clips_top_level_vs_tracks():
    a = {"clips": [{"a": 1}, {"b": 2}]}
    assert len(timeline_clips(a)) == 2
    b = {"tracks": [{"type": "video", "clips": [{"c": 3}]}]}
    out = timeline_clips(b)
    assert len(out) == 1
    assert out[0]["c"] == 3
    # track_type 由父轨注入
    assert out[0]["track_type"] == "video"


def test_default_clip_container_returns_existing_clips():
    data = {"clips": [{"a": 1}]}
    assert default_clip_container(data) is data["clips"]


def test_default_clip_container_creates_video_track_when_missing():
    data = {"tracks": []}
    container = default_clip_container(data)
    assert isinstance(container, list)
    assert data["tracks"][0]["type"] == "video"


def test_default_clip_container_reuses_existing_video_track():
    data = {"tracks": [{"type": "video", "clips": [{"x": 1}]}]}
    container = default_clip_container(data)
    assert container is data["tracks"][0]["clips"]


def test_track_clips_list_creates_when_missing():
    track: dict = {}
    out = track_clips_list(track)
    assert out == []
    assert track["clips"] == []


def test_file_sha256_known(tmp_path):
    import hashlib
    p = tmp_path / "sha.txt"
    p.write_bytes(b"hello")
    assert file_sha256(p) == hashlib.sha256(b"hello").hexdigest()


def test_load_timeline_parses_json(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert load_timeline(p) == {"a": 1}


def test_timeline_project_contract_lists_missing_keys():
    contract = timeline_project_contract({})
    assert contract["ok"] is False
    assert "project" in contract["missing"]
    assert "assets" in contract["missing"]
    assert "tracks" in contract["missing"]


def test_timeline_project_contract_passes_for_full_project():
    contract = timeline_project_contract(_empty_project())
    assert contract["ok"] is True
    assert contract["missing"] == []


def test_timeline_duration_top_level_clips():
    data = {"clips": [{"start": 0, "end": 2}, {"start": 2, "end": 5}]}
    assert timeline_duration(data) == 5.0


def test_timeline_duration_tracks_with_speed():
    data = {"tracks": [{"type": "video", "clips": [
        {"start": 0, "end": 2, "speed": 2.0},  # render_duration = 1.0
        {"start": 2, "end": 4, "speed": 1.0},  # render_duration = 2.0
    ]}]}
    d = timeline_duration(data)
    assert d == 3.0


def test_timeline_duration_returns_none_for_empty():
    assert timeline_duration({}) is None
    assert timeline_duration({"tracks": [{"type": "video", "clips": []}]}) is None


def test_patch_has_non_empty_operation():
    assert patch_has_non_empty_operation({}) is False
    assert patch_has_non_empty_operation({"add_clips": []}) is False
    assert patch_has_non_empty_operation({"add_clips": [{}]}) is True
    assert patch_has_non_empty_operation({"set_timeline_fields": {}}) is False
    assert patch_has_non_empty_operation({"set_timeline_fields": {"x": 1}}) is True


def test_operation_is_non_empty():
    assert operation_is_non_empty(None) is False
    assert operation_is_non_empty([]) is False
    assert operation_is_non_empty([1]) is True
    assert operation_is_non_empty({}) is False
    assert operation_is_non_empty({"a": 1}) is True
    assert operation_is_non_empty("string") is False


def test_with_suffix_before_ext_for_json():
    from assembly_capabilities.visual_observe import with_suffix_before_ext
    assert with_suffix_before_ext(Path("out/timeline.json"), "v1") == Path("out/timeline_v1.json")
