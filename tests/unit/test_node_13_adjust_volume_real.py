"""节点 13 真实实现单测(阶段 5:角色化音量 + 原生 audio_fades)。

覆盖:
- 角色识别按轨名:Voiceover_zh/en_voiceover=配音,其余 audio 轨=BGM,video 轨段=main;
- 音量写段级 volume/last_nonzero_volume(34 键样本实证键);
- 淡入淡出写原生 audio_fades 素材 + 段 extra_material_refs 绑定,时长钳制到段长;
- 幂等:重放字节级一致,旧 fade 清理;守卫:配音轨不被当 BGM,不删轨;
- K2=B 双草稿;剪映在跑告警/严格拒绝。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config

from nodes.node_13_adjust_volume import (
    adjust_volume,
    is_voiceover_track,
    jianying_adjust_volume,
)


def _video_seg(seg_id: str, duration: int = 35_000_000) -> dict:
    return {"id": seg_id, "target_timerange": {"start": 0, "duration": duration}}


@pytest.fixture
def draft_with_audio(tmp_path: Path) -> Path:
    """video 段(v1)+ 无名 audio 轨(BGM)+ 中文配音轨(Voiceover_zh)。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": {"audio_fades": []},
        "tracks": [
            {"type": "video", "segments": [_video_seg("v1")]},
            {
                "type": "audio",
                "id": "track-audio-bgm",
                "segments": [{
                    "id": "audio-bgm-1", "material_id": "audio-bgm-1",
                    "target_timerange": {"start": 0, "duration": 35_000_000},
                }],
            },
            {
                "type": "audio",
                "id": "track-audio-zh-voiceover",
                "name": "Voiceover_zh",
                "segments": [{
                    "id": "audio-vo-1", "material_id": "audio-vo-1",
                    "target_timerange": {"start": 0, "duration": 35_000_000},
                }],
            },
        ],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


@pytest.fixture
def draft_without_audio(tmp_path: Path) -> Path:
    """仅有 video track(段也空)的草稿(无 audio)。"""
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
# 剪映进程告警 / 严格拒绝
# ---------------------------------------------------------------------------
def test_adjust_volume_warns_when_jianying_running(
    draft_with_audio: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """剪映在跑 + 放行模式 → 告警去重(3 角色 × 2 草稿共 6 次写入只 1 条)。"""
    monkeypatch.setattr(
        "draft_ops.atomic_writer._default_proc_checker", lambda: True
    )
    monkeypatch.setattr(config, "JY_WRITE_FAIL_ON_JIANYING_RUNNING", False)

    out = adjust_volume(_state(draft_with_audio))

    warnings = [t for t in out["status_log"] if "剪映进程在跑" in t]
    assert warnings == ["[node_13] 剪映进程在跑,写入仍继续(告警不阻断)"], (
        f"告警文案/条数与节点 08/09/10/11 不一致:{warnings}"
    )
    assert "node_13_adjust_volume_done" in out["status_log"]
    assert out["volume_adjusted"] is True
    info_file = draft_with_audio.parent / "draft_info.json"
    assert info_file.exists(), "safe_write_draft 未做双写"


def test_adjust_volume_strict_mode_refuses_when_jianying_running(
    draft_with_audio: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """严格互斥(默认):剪映在跑 → 写入被拒(RuntimeError 冒泡,节点失败重试)。"""
    monkeypatch.setattr(
        "draft_ops.atomic_writer._default_proc_checker", lambda: True
    )
    with pytest.raises(RuntimeError, match="写入互斥严格模式"):
        adjust_volume(_state(draft_with_audio))


def test_adjust_volume_no_warning_when_jianying_not_running(
    draft_with_audio: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """剪映没在跑 → 不产生该告警(避免无意义噪音)。"""
    monkeypatch.setattr(
        "draft_ops.atomic_writer._default_proc_checker", lambda: False
    )
    out = adjust_volume(_state(draft_with_audio))
    assert not [t for t in out["status_log"] if "剪映进程在跑" in t]


# ---------------------------------------------------------------------------
# 角色化:main / bgm / voiceover
# ---------------------------------------------------------------------------
def test_main_role_sets_video_segment_volume(draft_with_audio: Path) -> None:
    """main:video 轨段 volume=1.0(last_nonzero_volume 同步),无 fade。"""
    jianying_adjust_volume(str(draft_with_audio), "main")

    draft = json.loads(draft_with_audio.read_text(encoding="utf-8"))
    seg = draft["tracks"][0]["segments"][0]
    assert seg["volume"] == 1.0
    assert seg["last_nonzero_volume"] == 1.0
    assert not any(
        str(r).startswith("audio-fade-") for r in seg.get("extra_material_refs", [])
    )


def test_bgm_role_sets_volume_and_native_fade(draft_with_audio: Path) -> None:
    """bgm:段 volume=0.35,原生 audio_fades 条目(fade_in 2s/fade_out 3s)+ ref 绑定。"""
    entry = jianying_adjust_volume(str(draft_with_audio), "bgm")

    assert entry is not None
    assert entry["type"] == "audio_fade"
    assert entry["fade_in_duration"] == 2_000_000
    assert entry["fade_out_duration"] == 3_000_000

    draft = json.loads(draft_with_audio.read_text(encoding="utf-8"))
    bgm_seg = draft["tracks"][1]["segments"][0]
    assert bgm_seg["volume"] == 0.35
    assert bgm_seg["last_nonzero_volume"] == 0.35
    fades = draft["materials"]["audio_fades"]
    assert len(fades) == 1
    assert fades[0]["id"] == "audio-fade-audio-bgm-1"
    assert fades[0]["id"] in bgm_seg["extra_material_refs"]


def test_voiceover_role_sets_volume_without_fade(draft_with_audio: Path) -> None:
    """voiceover:配音段 volume=1.0,不写 fade(淡入淡出只归 BGM)。"""
    jianying_adjust_volume(str(draft_with_audio), "voiceover")

    draft = json.loads(draft_with_audio.read_text(encoding="utf-8"))
    vo_seg = draft["tracks"][2]["segments"][0]
    assert vo_seg["volume"] == 1.0
    assert not vo_seg.get("extra_material_refs")
    assert draft["materials"]["audio_fades"] == []


def test_voiceover_track_not_treated_as_bgm(draft_with_audio: Path) -> None:
    """bgm 角色不触碰配音轨;voiceover 角色不触碰 BGM 轨(按轨名分角色)。"""
    jianying_adjust_volume(str(draft_with_audio), "bgm")
    draft = json.loads(draft_with_audio.read_text(encoding="utf-8"))
    assert draft["tracks"][2]["segments"][0].get("volume", 1.0) == 1.0

    jianying_adjust_volume(str(draft_with_audio), "voiceover")
    draft = json.loads(draft_with_audio.read_text(encoding="utf-8"))
    assert draft["tracks"][1]["segments"][0]["volume"] == 0.35  # bgm 值不被 voiceover 改


def test_is_voiceover_track_matches_both_markers() -> None:
    assert is_voiceover_track({"name": "Voiceover_zh"})
    assert is_voiceover_track({"name": "en_voiceover"})
    assert not is_voiceover_track({"name": "user_picked_bgm"})
    assert not is_voiceover_track({})


def test_fade_clamped_to_segment_duration(tmp_path: Path) -> None:
    """fade 时长超过段长 → 钳制到段长(1s 段 + 2s/3s 配置)。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 1_000_000,
        "materials": {},
        "tracks": [{
            "type": "audio",
            "segments": [_video_seg("short-bgm", 1_000_000)],
        }],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft), encoding="utf-8")

    jianying_adjust_volume(str(p), "bgm")
    after = json.loads(p.read_text(encoding="utf-8"))
    (fade,) = after["materials"]["audio_fades"]
    assert fade["fade_in_duration"] == 1_000_000
    assert fade["fade_out_duration"] == 1_000_000


def test_unknown_role_raises() -> None:
    with pytest.raises(ValueError, match="未知角色"):
        jianying_adjust_volume("/tmp/whatever.json", "audio_main")  # 旧 selector 串已废弃


def test_role_returns_none_when_no_segments(draft_without_audio: Path) -> None:
    """角色无任何可调段 → 返回 None 且不写盘。"""
    before = draft_without_audio.read_text(encoding="utf-8")
    assert jianying_adjust_volume(str(draft_without_audio), "main") is None
    assert draft_without_audio.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# 节点包装:降级 / 全角色 / 双草稿 / 幂等
# ---------------------------------------------------------------------------
def test_adjust_volume_node_writes_error_log_when_no_audio(draft_without_audio: Path) -> None:
    """无 audio track 且 video 段为空 → error_log + volume_adjusted=False + 通过。"""
    out = adjust_volume(_state(draft_without_audio))
    assert out["volume_adjusted"] is False
    assert any("audio" in e for e in out["error_log"])
    assert "node_13_adjust_volume_no_audio_track" in out["status_log"]


def test_adjust_volume_node_applies_all_roles(draft_with_audio: Path) -> None:
    """三角色一次跑完:audio_fade_targets 记录 bgm 条目,volume_adjusted=True。"""
    out = adjust_volume(_state(draft_with_audio))
    assert out["volume_adjusted"] is True
    assert "node_13_adjust_volume_done" in out["status_log"]
    assert len(out["audio_fade_targets"]) >= 1
    assert out["audio_fade_targets"][0]["type"] == "audio_fade"

    draft = json.loads(draft_with_audio.read_text(encoding="utf-8"))
    assert len(draft["materials"]["audio_fades"]) == 1


def test_adjust_volume_node_skips_when_no_draft_path() -> None:
    """state.draft_path 缺失 → 跳过,写 error_log。"""
    out = adjust_volume({"status_log": [], "error_log": []})
    assert out["volume_adjusted"] is False
    assert any("draft_path" in e for e in out["error_log"])
    assert "node_13_adjust_volume_skipped" in out["status_log"]


def test_adjust_volume_handles_missing_draft_path_file(tmp_path: Path) -> None:
    """draft_path 指向不存在的文件 → 写 error_log + 通过。"""
    out = adjust_volume({
        "draft_path": str(tmp_path / "no_such.json"),
        "status_log": [],
        "error_log": [],
    })
    assert out["volume_adjusted"] is False
    assert any("不存在" in e or "no_such" in e for e in out["error_log"])


def test_adjust_volume_applies_to_en_draft_when_present(
    draft_with_audio: Path, tmp_path: Path
) -> None:
    """K2=B:英文分支草稿存在时,三角色同样写入英文草稿。"""
    en_dir = tmp_path / "en_branch"
    en_dir.mkdir(parents=True)
    en_file = en_dir / "draft_content.json"
    en_file.write_text(draft_with_audio.read_text(encoding="utf-8"), encoding="utf-8")

    state = {"draft_path": str(draft_with_audio), "draft_dir_en_branch": str(en_dir),
             "status_log": [], "error_log": []}
    out = adjust_volume(state)

    en = json.loads(en_file.read_text(encoding="utf-8"))
    bgm_seg = en["tracks"][1]["segments"][0]
    assert bgm_seg["volume"] == 0.35
    assert len(en["materials"]["audio_fades"]) == 1
    assert "node_13_adjust_volume_en_done" in out["status_log"]
    assert out["volume_adjusted"] is True


def test_adjust_volume_degrades_when_en_draft_has_no_audio(tmp_path: Path) -> None:
    """K2=B 降级:英文草稿无可调段 → 不抛异常,中文草稿照常调整。"""
    zh_file = tmp_path / "zh" / "draft_content.json"
    zh_file.parent.mkdir(parents=True)
    zh_file.write_text(json.dumps({
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": {},
        "tracks": [{
            "type": "audio",
            "segments": [{
                "id": "audio-bgm-1", "material_id": "audio-bgm-1",
                "target_timerange": {"start": 0, "duration": 35_000_000},
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")

    en_dir = tmp_path / "en_branch"
    en_dir.mkdir(parents=True)
    en_file = en_dir / "draft_content.json"
    en_file.write_text(json.dumps({
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": {},
        "tracks": [{"type": "video", "segments": []}],
    }, ensure_ascii=False), encoding="utf-8")

    state = {"draft_path": str(zh_file), "draft_dir_en_branch": str(en_dir),
             "status_log": [], "error_log": []}
    out = adjust_volume(state)

    assert out["volume_adjusted"] is True
    en = json.loads(en_file.read_text(encoding="utf-8"))
    assert not en.get("materials", {}).get("audio_fades")


def test_adjust_volume_degrades_when_en_draft_missing(
    draft_with_audio: Path, tmp_path: Path
) -> None:
    """K2=B 降级:英文草稿目录不存在 → 只处理中文,写 error_log,不抛异常。"""
    state = {"draft_path": str(draft_with_audio), "draft_dir_en_branch": str(tmp_path / "nope"),
             "status_log": [], "error_log": []}
    out = adjust_volume(state)

    assert out["volume_adjusted"] is True
    assert "node_13_adjust_volume_en_done" not in out["status_log"]
    assert any("英文草稿不存在" in e for e in out["error_log"])


def test_fade_replay_is_byte_identical(draft_with_audio: Path) -> None:
    """重放:fade 按 id 覆盖 + 旧 refs 剥离 → 整份草稿字节级一致。"""
    adjust_volume(_state(draft_with_audio))
    first = draft_with_audio.read_bytes()
    adjust_volume(_state(draft_with_audio))
    second = draft_with_audio.read_bytes()
    assert first == second


def test_stale_fades_cleaned_when_segment_removed(draft_with_audio: Path) -> None:
    """BGM 段被删后重放:其 fade 素材被清理(不留孤儿)。"""
    adjust_volume(_state(draft_with_audio))

    draft = json.loads(draft_with_audio.read_text(encoding="utf-8"))
    draft["tracks"][1]["segments"] = []
    draft_with_audio.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    adjust_volume(_state(draft_with_audio))
    after = json.loads(draft_with_audio.read_text(encoding="utf-8"))
    assert after["materials"]["audio_fades"] == []
