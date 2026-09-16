"""节点 13 真实实现单测 — 音量 / 淡入淡出 / audio_fades 写入。

覆盖:
(a) 主音轨存在 → volume=1.0、audio_fades 不变
(b) BGM 存在 → volume=0.35、audio_fades 含 BGM fade-in 2s / fade-out 3s 条目
(c) 无 audio track → 写 error_log + volume_adjusted=False + 通过(不抛异常)
(d) 字段名完整性:audio_fades 条目含必要键(不硬编码剪映实际值,允许占位字段名)
(e) draft_path 缺失 → 写 error_log,跳过
(f) 节点 11 既有单测不破:`test_adjust_volume_placeholder_passes` 已删除,
    此文件是节点 13 的"真实实现"覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_13_adjust_volume import (
    _PLACEHOLDER_FADE_IN_KEY,
    _PLACEHOLDER_FADE_OUT_KEY,
    _PLACEHOLDER_VOLUME_KEY,
    adjust_volume,
    jianying_adjust_volume,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def draft_with_audio(tmp_path: Path) -> Path:
    """构造含 main + bgm 两条 audio segment 的草稿。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": {
            "audios": [
                {"id": "audio-main-1", _PLACEHOLDER_VOLUME_KEY: 0.5},
                {"id": "audio-bgm-1", _PLACEHOLDER_VOLUME_KEY: 0.8},
            ],
            "audio_fades": [],
        },
        "tracks": [
            {
                "type": "video",
                "segments": [
                    {"id": "v1", "target_timerange": {"start": 0, "duration": 35_000_000}},
                ],
            },
            {
                "type": "audio",
                "segments": [
                    {
                        "id": "audio-main-1",
                        "name": "audio_main",
                        "type": "audio_main",
                        "target_timerange": {"start": 0, "duration": 35_000_000},
                    },
                    {
                        "id": "audio-bgm-1",
                        "name": "audio_bgm",
                        "type": "audio_bgm",
                        "target_timerange": {"start": 0, "duration": 35_000_000},
                    },
                ],
            },
        ],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


@pytest.fixture
def draft_without_audio(tmp_path: Path) -> Path:
    """构造仅有 video track 的草稿(无 audio)。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": {},
        "tracks": [{"type": "video", "segments": []}],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _state(draft_path: Path) -> dict:
    return {"draft_path": str(draft_path), "status_log": [], "error_log": []}


# ---------------------------------------------------------------------------
# (a) + (b) 主音轨与 BGM 各自应用 volume 与 fade
# ---------------------------------------------------------------------------
def test_jianying_adjust_volume_sets_main_volume(draft_with_audio: Path) -> None:
    """主音轨:volume 应被设为 1.0(无 fade)。"""
    jianying_adjust_volume(str(draft_with_audio), "audio_main", volume_level=1.0)

    draft = json.loads(draft_with_audio.read_text(encoding="utf-8"))
    audios = {a["id"]: a for a in draft["materials"]["audios"]}
    assert audios["audio-main-1"][_PLACEHOLDER_VOLUME_KEY] == 1.0


def test_jianying_adjust_volume_sets_bgm_volume_and_fade(draft_with_audio: Path) -> None:
    """BGM:volume=0.35,fade_in=2s,fade_out=3s;audio_fades 应含 BGM 条目。"""
    entry = jianying_adjust_volume(
        str(draft_with_audio),
        "audio_bgm",
        volume_level=0.35,
        fade_in_seconds=2.0,
        fade_out_seconds=3.0,
    )

    assert entry is not None
    assert entry[_PLACEHOLDER_FADE_IN_KEY] == 2_000_000
    assert entry[_PLACEHOLDER_FADE_OUT_KEY] == 3_000_000

    draft = json.loads(draft_with_audio.read_text(encoding="utf-8"))
    audios = {a["id"]: a for a in draft["materials"]["audios"]}
    assert audios["audio-bgm-1"][_PLACEHOLDER_VOLUME_KEY] == 0.35
    assert len(draft["materials"]["audio_fades"]) >= 1


def test_audio_fades_entry_has_required_keys(draft_with_audio: Path) -> None:
    """audio_fades 条目应含 id + track_id + fade_in/out 字段键(不硬编码剪映实际值)。"""
    entry = jianying_adjust_volume(
        str(draft_with_audio),
        "audio_bgm",
        volume_level=0.35,
        fade_in_seconds=2.0,
        fade_out_seconds=3.0,
    )
    assert entry is not None
    assert "id" in entry
    assert "track_id" in entry  # FIXME 字段名逆向 TODO:用户回填后这里应改为 _PLACEHOLDER_TRACK_ID_KEY
    assert _PLACEHOLDER_FADE_IN_KEY in entry
    assert _PLACEHOLDER_FADE_OUT_KEY in entry


# ---------------------------------------------------------------------------
# (c) 无 audio track 时降级
# ---------------------------------------------------------------------------
def test_jianying_adjust_volume_returns_none_when_no_audio_track(draft_without_audio: Path) -> None:
    """无 audio track:返回 None,不抛异常。"""
    result = jianying_adjust_volume(str(draft_without_audio), "audio_main", volume_level=1.0)
    assert result is None


def test_adjust_volume_node_writes_error_log_when_no_audio(draft_without_audio: Path) -> None:
    """节点包装:无 audio track → 写 error_log + volume_adjusted=False + 通过。"""
    state = _state(draft_without_audio)
    out = adjust_volume(state)

    assert out["volume_adjusted"] is False
    assert any("audio" in e for e in out["error_log"])
    assert "node_13_adjust_volume_no_audio_track" in out["status_log"]


# ---------------------------------------------------------------------------
# (d) 节点 13 端到端:同时应用 main + bgm
# ---------------------------------------------------------------------------
def test_adjust_volume_node_applies_main_and_bgm(draft_with_audio: Path) -> None:
    """节点 13 同时对 main + bgm 执行:audio_fades 应含 bgm 条目,volume_adjusted=True。"""
    state = _state(draft_with_audio)
    out = adjust_volume(state)

    assert out["volume_adjusted"] is True
    assert "node_13_adjust_volume_done" in out["status_log"]
    assert len(out["audio_fade_targets"]) >= 1

    draft = json.loads(draft_with_audio.read_text(encoding="utf-8"))
    assert len(draft["materials"]["audio_fades"]) >= 1


# ---------------------------------------------------------------------------
# (e) draft_path 缺失
# ---------------------------------------------------------------------------
def test_adjust_volume_node_skips_when_no_draft_path() -> None:
    """state.draft_path 缺失 → 跳过,写 error_log。"""
    state = {"status_log": [], "error_log": []}
    out = adjust_volume(state)

    assert out["volume_adjusted"] is False
    assert any("draft_path" in e for e in out["error_log"])
    assert "node_13_adjust_volume_skipped" in out["status_log"]


# ---------------------------------------------------------------------------
# 兼容性 — 保留旧占位节点测试的降级路径覆盖
# ---------------------------------------------------------------------------
def test_adjust_volume_handles_missing_draft_path_file(tmp_path: Path) -> None:
    """draft_path 指向不存在的文件 → 写 error_log + 通过。"""
    state = {
        "draft_path": str(tmp_path / "no_such.json"),
        "status_log": [],
        "error_log": [],
    }
    out = adjust_volume(state)
    # 任一 audio 通道触发 FileNotFoundError,被 _try_audio_track 吞掉
    # 最终 volume_adjusted 取决于是否某通道成功写入;此处应 False
    assert out["volume_adjusted"] is False
    assert any("不存在" in e or "no_such" in e for e in out["error_log"])