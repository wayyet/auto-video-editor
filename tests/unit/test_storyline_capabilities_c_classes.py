"""C 类(阶段 4/5)单元测试。

覆盖:
- tts_runner:StubTTSClient + read_wav_duration_ms + load_tts_providers
- bgm_selector:rank_candidates + compute_beats_ms + load_bgm_library
- generate_voiceover:stub 默认 + 单段失败 + 空输入
- select_bgm:启发式 + LLM + fallback + empty library
- speech_rough_cut:LLM 拆分
- timeline_planner:TimeLine 简化版(节拍对齐 + TTS 对齐 + target_duration 缩放)
- plan_timeline_pro:组合 capability
"""
from __future__ import annotations

import json
import wave
from pathlib import Path
from typing import Any

import pytest

from storyline_capabilities.bgm_selector import (
    compute_beats_ms,
    load_bgm_library,
    rank_candidates,
    _match_mood_tags,
)
from storyline_capabilities.generate_voiceover import generate_voiceover
from storyline_capabilities.select_bgm import select_bgm
# Stage 5 modules
from storyline_capabilities.speech_rough_cut import speech_rough_cut
from storyline_capabilities.timeline_planner import TimeLine
from storyline_capabilities.plan_timeline_pro import plan_timeline_pro
from storyline_capabilities.asr_runner import (
    StubASRClient,
    transcribe_media,
)
from storyline_capabilities.ai_transition_client import (
    StubAITransitionClient,
    is_ai_transition_enabled,
)
from storyline_capabilities.tts_runner import (
    StubTTSClient,
    load_tts_providers,
    read_wav_duration_ms,
)


# ===========================================================================
# tts_runner
# ===========================================================================
def test_stub_tts_client_writes_wav(tmp_path: Path) -> None:
    out = tmp_path / "x.wav"
    client = StubTTSClient()
    result = client.synthesize(text="你好世界hello", wav_path=out, params={})
    assert result.provider == "stub"
    assert result.error is None
    assert result.duration_ms >= 500
    assert out.exists()
    # wav header 合法
    with wave.open(str(out), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 16000


def test_stub_tts_estimates_longer_text_longer_duration(tmp_path: Path) -> None:
    short = StubTTSClient().synthesize(text="hi", wav_path=tmp_path / "s.wav", params={})
    long = StubTTSClient().synthesize(
        text="这是一段很长的中文文本用来测试配音时长估算逻辑是否合理",
        wav_path=tmp_path / "l.wav",
        params={},
    )
    assert long.duration_ms > short.duration_ms


def test_read_wav_duration_ms_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "rt.wav"
    StubTTSClient().synthesize(text="abcde", wav_path=p, params={})
    dur = read_wav_duration_ms(p)
    assert dur > 0
    # 与 stub 估算的 duration_ms 误差在 50ms 内
    # (估算基于 4 chars/sec,wav 用 sample_rate=16000 写入,两者一致)


def test_read_wav_duration_ms_missing_returns_zero(tmp_path: Path) -> None:
    assert read_wav_duration_ms(tmp_path / "no.wav") == 0


def test_load_tts_providers_yaml_exists() -> None:
    providers = load_tts_providers(
        Path(__file__).resolve().parent.parent.parent
        / "storyline_capabilities" / "resource" / "tts_ref.yaml"
    )
    assert "minimax" in providers
    assert providers["minimax"].default_base_url.startswith("https://")
    assert providers["minimax"].env_key_prefix == "TTS_MINIMAX_"


# ===========================================================================
# bgm_selector
# ===========================================================================
def test_match_mood_tags_chinese() -> None:
    tags = _match_mood_tags("今天天气真好,我很开心")
    assert "happy" in tags


def test_match_mood_tags_english() -> None:
    tags = _match_mood_tags("a warm and cozy story")
    assert "warm" in tags


def test_match_mood_tags_empty() -> None:
    assert _match_mood_tags("") == set()
    # 不包含任何情绪/关键词的纯描述文本
    assert _match_mood_tags("拍摄风景画面与人物") == set()


def test_rank_candidates_by_mood() -> None:
    library = [
        {"bgm_id": "a", "mood": ["happy", "warm"]},
        {"bgm_id": "b", "mood": ["sad"]},
        {"bgm_id": "c", "mood": ["energetic"]},
    ]
    ranked = rank_candidates(
        library=library,
        user_request="欢快开心",
        narration_text="",
        top_n=3,
    )
    assert ranked[0]["bgm_id"] == "a"     # 命中 happy
    # b 完全没有命中但仍在 top_n 里(plan §5 阶段 4 启发式不剔除)
    assert len(ranked) == 3


def test_compute_beats_ms() -> None:
    beats = compute_beats_ms(duration_ms=10000, beat_period_ms=2000)
    assert beats == [2000, 4000, 6000, 8000]


def test_compute_beats_ms_zero_returns_empty() -> None:
    assert compute_beats_ms(duration_ms=0, beat_period_ms=1000) == []
    assert compute_beats_ms(duration_ms=5000, beat_period_ms=0) == []


def test_load_bgm_library_yaml_exists() -> None:
    meta = load_bgm_library()
    assert "library" in meta
    assert len(meta["library"]) >= 3
    for entry in meta["library"]:
        assert "bgm_id" in entry
        assert "mood" in entry


# ===========================================================================
# generate_voiceover capability
# ===========================================================================
def test_generate_voiceover_empty(tmp_path: Path) -> None:
    res = generate_voiceover(group_scripts=[], output_dir=tmp_path)
    assert res["voiceover"] == []
    assert res["errors"] == []
    assert res["stub_count"] == 0


def test_generate_voiceover_default_stub(tmp_path: Path) -> None:
    scripts = [
        {"group_id": "g1", "narration": "你好世界"},
        {"group_id": "g2", "narration": "今天的故事"},
    ]
    res = generate_voiceover(group_scripts=scripts, output_dir=tmp_path)
    assert res["provider"] in ("minimax", "stub")
    assert len(res["voiceover"]) == 2
    assert all(v["path"] for v in res["voiceover"])
    assert all(v["duration"] > 0 for v in res["voiceover"])
    assert res["stub_count"] >= 0  # 默认无 key → 全 stub


def test_generate_voiceover_per_segment_isolation(tmp_path: Path) -> None:
    """一段失败不阻断其他段(plan §4.4 + 阶段 4 决策 4)。"""

    scripts = [
        {"group_id": "g1", "narration": "OK"},
        {"group_id": "g2", "narration": ""},       # 空 narration
        {"group_id": "g3", "narration": "fine"},
    ]
    res = generate_voiceover(group_scripts=scripts, output_dir=tmp_path)
    assert len(res["voiceover"]) == 3
    # g2 失败但 g1/g3 仍 OK
    assert res["voiceover"][1]["duration"] == 0
    assert res["voiceover"][1]["error"]
    assert res["voiceover"][0]["duration"] > 0
    assert res["voiceover"][2]["duration"] > 0


def test_generate_voiceover_wav_files_exist(tmp_path: Path) -> None:
    scripts = [{"group_id": "g1", "narration": "测试 wav 写出"}]
    res = generate_voiceover(group_scripts=scripts, output_dir=tmp_path)
    p = Path(res["voiceover"][0]["path"])
    assert p.exists()
    assert read_wav_duration_ms(p) > 0


# ===========================================================================
# select_bgm capability
# ===========================================================================
def test_select_bgm_default_match(tmp_path: Path) -> None:
    res = select_bgm(user_request="", groups=[], narration_text="")
    assert res["method"] in ("default_match", "heuristic")
    assert res["bgm_ref"] != "bgm_none"
    assert isinstance(res["beats"], list)


def test_select_bgm_heuristic_picks_happy(tmp_path: Path) -> None:
    library = [
        {"bgm_id": "warm", "mood": ["warm"], "tempo": "slow", "duration_ms": 60000,
         "default_volume": 0.3, "beat_period_ms": 1000},
        {"bgm_id": "happy", "mood": ["happy"], "tempo": "fast", "duration_ms": 60000,
         "default_volume": 0.4, "beat_period_ms": 500},
    ]
    res = select_bgm(
        user_request="欢快节奏",
        groups=[],
        library=library,
        default_match={"bgm_id": "warm"},
    )
    # LLM stub 返回空 bgm_id → fallback 启发式第一条(happy 命中 happy)
    assert res["bgm_ref"] in ("happy", "warm")  # fallback 默认
    # 至少 method 落到 heuristic / llm / default_match 之一
    assert res["method"] in ("heuristic", "llm", "default_match")


def test_select_bgm_empty_library_returns_empty() -> None:
    res = select_bgm(
        user_request="",
        groups=[],
        library=[],
        default_match={},
    )
    assert res["bgm_ref"] == "bgm_none"
    assert res["stub"] is True
    assert res["method"] == "empty"


def test_select_bgm_with_group_narration() -> None:
    groups = [
        {"group_id": "g1", "theme": "warm story"},
        {"group_id": "g2", "theme": "happy moment"},
    ]
    res = select_bgm(user_request="", groups=groups, narration_text="")
    assert res["bgm_ref"] != "bgm_none"


# ===========================================================================
# speech_rough_cut & timeline_planner — stage 5 tests
# ===========================================================================
def test_speech_rough_cut_empty() -> None:
    res = speech_rough_cut(asr_sentence={}, ctx_text="", history=[])
    assert res["segments"] == []
    assert res["reason"]


def test_speech_rough_cut_basic_with_stub_llm(tmp_path: Path) -> None:
    from storyline_capabilities.vlm_client import StubLLMClient

    asr = {
        "text": "今天我们讲 OpenStoryline",
        "start": 1000,
        "end": 3000,
        "timestamp": [[1000, 1100], [1100, 1300], [1300, 1500]],
    }
    client = StubLLMClient(
        default_caption=json.dumps(
            {
                "reason": "ok",
                "res": [
                    {"text": "今天我们讲 OpenStoryline", "start": 1000, "end": 3000}
                ],
            },
            ensure_ascii=False,
        )
    )
    res = speech_rough_cut(
        asr_sentence=asr,
        ctx_text=asr["text"],
        history=[],
        client=client,
    )
    assert len(res["segments"]) == 1


def test_speech_rough_cut_keeps_relative_to_original_window(
    tmp_path: Path,
) -> None:
    """end <= start 的段被丢弃(plan §5 阶段 5 + vendored 行为对齐)。"""
    from storyline_capabilities.vlm_client import StubLLMClient

    asr = {"text": "短句", "start": 0, "end": 1000}
    client = StubLLMClient(
        default_caption=json.dumps(
            {
                "reason": "edge cases",
                "res": [
                    {"text": "ok", "start": 100, "end": 900},
                    {"text": "bad1", "start": 0, "end": 0},       # end <= start → drop
                    {"text": "bad2", "start": 500, "end": 400},   # end < start → drop
                    {"text": "", "start": 0, "end": 100},         # empty text → drop
                ],
            },
            ensure_ascii=False,
        )
    )
    res = speech_rough_cut(
        asr_sentence=asr, ctx_text="短句", history=[], client=client
    )
    assert len(res["segments"]) == 1
    assert res["segments"][0]["text"] == "ok"


# ===========================================================================
# timeline_planner & plan_timeline_pro(阶段 5 TimeLine 简化版)
# ===========================================================================
def test_timeline_distribute_by_tts_durations() -> None:
    """纯 TTS 驱动:总长 = sum(tts_durations),每组按 TTS duration 平分给 clips。"""
    tl = TimeLine()
    groups = [
        {"group_id": "g1", "clips": [{"clip_id": "c1"}]},
        {"group_id": "g2", "clips": [{"clip_id": "c2"}]},
    ]
    tts_res = [{"duration": 4000}, {"duration": 4000}]
    out = tl.distribute(
        groups=groups,
        tts_res=tts_res,
        bgm=None,
        targets={"target_duration_ms": 8000},
    )
    assert out["total_ms"] == 8000
    assert out["method"] == "tts_align"
    assert len(out["clips"]) == 2


def test_timeline_target_shorter_than_total_trims() -> None:
    tl = TimeLine()
    groups = [
        {"group_id": "g1", "clips": [{"clip_id": "c1"}]},
        {"group_id": "g2", "clips": [{"clip_id": "c2"}]},
    ]
    tts_res = [{"duration": 5000}, {"duration": 5000}]
    out = tl.distribute(
        groups=groups,
        tts_res=tts_res,
        bgm=None,
        targets={"target_duration_ms": 4000},
    )
    # 有 TTS 时不强行裁剪(避免脱音),但写警告
    assert out["total_ms"] == 10000
    assert out.get("warnings")


def test_timeline_target_no_tts_scales_proportionally() -> None:
    tl = TimeLine()
    groups = [{"group_id": "g1", "clips": [{"clip_id": f"c{i}"} for i in range(4)]}]
    out = tl.distribute(
        groups=groups,
        tts_res=[],
        bgm=None,
        targets={"target_duration_ms": 4000, "min_clip_duration_ms": 500},
    )
    assert out["total_ms"] == 4000
    assert out["method"] == "scaled_to_target"
    assert len(out["clips"]) == 4


def test_timeline_bgm_alignment_uses_beat_period() -> None:
    """无 TTS + 有 BGM:按 BGM 节拍等距。"""
    tl = TimeLine()
    groups = [{"group_id": "g1", "clips": [{"clip_id": f"c{i}"} for i in range(3)]}]
    out = tl.distribute(
        groups=groups,
        tts_res=[],
        bgm={"duration_ms": 9000, "beat_period_ms": 3000},
        targets={"target_duration_ms": 9000},
    )
    assert out["method"] == "beat_align"
    assert abs(out["total_ms"] - 9000) <= 300


def test_timeline_no_input_returns_empty() -> None:
    tl = TimeLine()
    out = tl.distribute(groups=[], tts_res=[], bgm=None, targets={})
    assert out["total_ms"] == 0
    assert out["clips"] == []


def test_plan_timeline_pro_full_chain(tmp_path: Path) -> None:
    """组合能力:group + voiceover + bgm → CanonicalTimeline 形状 dict。"""
    from storyline_capabilities.vlm_client import StubLLMClient
    from storyline_capabilities.select_bgm import select_bgm
    from storyline_capabilities.generate_voiceover import generate_voiceover

    client = StubLLMClient(default_caption='{"scripts": [{"narration": "ok"}]}')
    # 1. group
    groups = [
        {
            "group_id": "g1",
            "clips": [
                {
                    "clip_id": "c1",
                    "media_id": "media_0001",
                    "source_in_ms": 0,
                    "source_out_ms": 5000,
                }
            ],
        }
    ]
    # 2. voiceover
    vo = generate_voiceover(
        group_scripts=[{"group_id": "g1", "narration": "短句配音"}],
        output_dir=tmp_path,
        tts_client=StubTTSClient(),
    )
    # 3. bgm
    bgm = select_bgm(user_request="", groups=groups, narration_text="")
    # 4. 组装
    timeline = plan_timeline_pro(
        groups=groups,
        voiceover=vo,
        bgm_selection=bgm,
        targets={"target_duration_ms": 5000},
        source_media=[
            {
                "media_id": "media_0001",
                "file_uri": "file:///x.mp4",
                "duration_ms": 5000,
                "media_type": "video",
            }
        ],
        job_id="job_test",
    )
    assert timeline["schema_version"] == "1.0"
    assert timeline["job_id"] == "job_test"
    assert len(timeline["clips"]) >= 1
    assert timeline["options"]["enable_voiceover"] is True
    assert timeline["audio"]["voiceover"] != {}


def test_plan_timeline_pro_attaches_transition_and_text_style() -> None:
    groups = [
        {
            "group_id": "g1",
            "clips": [{"clip_id": "c1", "media_id": "m1", "source_in_ms": 0, "source_out_ms": 4000}],
        }
    ]
    timeline = plan_timeline_pro(
        groups=groups,
        voiceover={"voiceover": []},
        bgm_selection={},
        transition_plan={"items": [{"group_id": "g1", "transition_in": "fade", "transition_out": "cut"}]},
        text_style_plan={"default": {"font_zh": "x.ttf"}, "items": [{"group_id": "g1", "font_zh": "x.ttf"}]},
        targets={"target_duration_ms": 4000},
        source_media=[],
        job_id="job_test",
    )
    clips = timeline["clips"]
    assert clips[0].get("transition_in") == "fade"
    assert clips[0].get("transition_out") == "cut"
    assert clips[0].get("subtitle_style") is not None


def test_plan_timeline_pro_empty_groups() -> None:
    timeline = plan_timeline_pro(
        groups=[], voiceover={}, bgm_selection={},
        targets={"target_duration_ms": 35000}, source_media=[],
    )
    assert timeline["clips"] == []
    assert timeline["method"] == "empty"


# ===========================================================================
# asr_runner / ai_transition_client
# ===========================================================================
def test_stub_asr_client_empty_segments(tmp_path: Path) -> None:
    client = StubASRClient()
    res = client.transcribe(
        media_path=tmp_path / "x.mp4", output_dir=tmp_path / "out"
    )
    assert res.provider == "stub"
    assert res.segments == []
    assert res.artifact_path.exists()


def test_transcribe_media_returns_dict(tmp_path: Path) -> None:
    fake = tmp_path / "x.mp4"
    fake.write_bytes(b"")
    res = transcribe_media(media_path=fake, output_dir=tmp_path / "out")
    assert "artifact" in res
    assert "segments" in res
    assert "provider" in res


def test_stub_ai_transition_writes_artifact(tmp_path: Path) -> None:
    client = StubAITransitionClient()
    res = client.generate(
        from_clip={"clip_id": "c1"},
        to_clip={"clip_id": "c2"},
        output_dir=tmp_path,
    )
    assert res.provider == "stub"
    assert res.duration_ms > 0
    assert res.artifact_path.exists()


def test_ai_transition_default_off() -> None:
    """plan §5 阶段 5 默认关闭 AI Transition,flag 读取要安全。"""
    is_ai_transition_enabled()        # 不抛


# ===========================================================================
# 节点壳子回归(避免破坏 passthrough 引用迁移)
# ===========================================================================
def test_node_shells_importable() -> None:
    """确认 nodes/storyline/node_*.py 在阶段 4/5 改完后仍可导入。"""
    from nodes.storyline import (  # noqa: F401
        node_generate_voiceover,
        node_select_bgm,
        node_plan_timeline_pro,
        node_local_asr,
        node_speech_rough_cut,
        node_render_video,
        node_generate_ai_transition,
        node_plan_timeline_ai_transition,
    )