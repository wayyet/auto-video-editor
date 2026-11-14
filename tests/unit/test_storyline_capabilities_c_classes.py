"""C 类(阶段 4/5)单元测试。

覆盖:
- tts_runner:StubTTSClient + read_wav_duration_ms + load_tts_providers
- bgm_selector:rank_candidates + compute_beats_ms + load_bgm_library
- generate_voiceover:stub 默认 + 单段失败 + 空输入
- select_bgm:启发式 + LLM + fallback + empty library
- timeline_planner:TimeLine 简化版(节拍对齐 + TTS 对齐 + target_duration 缩放)
- plan_timeline_pro:组合 capability

(speech_rough_cut / asr_runner 用例已随 2026-11-08 B1 ASR 移除删除。)
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
    rank_candidates_with_llm,
    _match_mood_tags,
)
from storyline_capabilities.generate_voiceover import generate_voiceover
from storyline_capabilities.select_bgm import select_bgm
# Stage 5 modules
from storyline_capabilities.timeline_planner import TimeLine
from storyline_capabilities.plan_timeline_pro import plan_timeline_pro
from storyline_capabilities.ai_transition_client import (
    is_ai_transition_enabled,
)
from storyline_capabilities.ai_transition_provider import resolve_default_provider
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


def test_select_bgm_prefers_track_covering_voiceover() -> None:
    """任务B(2026-11-13 边审计阶段2):有配音总时长时,时长能覆盖配音的候选稳定排前。

    构造:两条候选同 mood("欢快"→happy 同分,启发式原序 short_hit 在前);
    voiceover_total_ms 大于短轨时长 → 覆盖重排后 fallback(candidates[0])
    翻转为长轨;同库不传配音时长时仍选短轨 —— 证明偏好确实由配音驱动。
    """
    library = [
        {"bgm_id": "short_hit", "mood": ["happy"], "duration_ms": 20000,
         "default_volume": 0.4, "beat_period_ms": 500},
        {"bgm_id": "long_cover", "mood": ["happy"], "duration_ms": 60000,
         "default_volume": 0.4, "beat_period_ms": 500},
    ]
    res = select_bgm(
        user_request="欢快",
        groups=[],
        library=library,
        voiceover_total_ms=45000,
    )
    assert res["bgm_ref"] == "long_cover"
    assert res["voiceover_total_ms"] == 45000

    # 对照:同库不传配音时长 → 无覆盖重排 → 启发式原第1(short_hit)
    res_no_vo = select_bgm(user_request="欢快", groups=[], library=library)
    assert res_no_vo["bgm_ref"] == "short_hit"
    assert res_no_vo["voiceover_total_ms"] == 0


def test_select_bgm_voiceover_zero_matches_default_behavior() -> None:
    """任务B 容缺省语义:voiceover_total_ms=0(配音缺失/失败容缺省路径)
    与不传参数时输出逐字段一致 —— 保证存量行为零漂移。"""
    library = [
        {"bgm_id": "warm", "mood": ["warm"], "tempo": "slow", "duration_ms": 60000,
         "default_volume": 0.3, "beat_period_ms": 1000},
        {"bgm_id": "happy", "mood": ["happy"], "tempo": "fast", "duration_ms": 60000,
         "default_volume": 0.4, "beat_period_ms": 500},
    ]
    a = select_bgm(user_request="温暖故事", groups=[], library=library)
    b = select_bgm(user_request="温暖故事", groups=[], library=library, voiceover_total_ms=0)
    assert a == b
    assert b["voiceover_total_ms"] == 0


# ===========================================================================
# timeline_planner & plan_timeline_pro(阶段 5 TimeLine 简化版)
# (speech_rough_cut 用例已随 2026-11-08 B1 ASR 移除删除)
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
# ai_transition_client / provider
# (asr_runner 用例已随 2026-11-08 B1 ASR 移除删除;
#  2026-11-12 阶段2:Stub 已删除,默认未配置 Provider → resolve 返回 None)
# ===========================================================================
def test_ai_transition_provider_unresolved_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置 STORYLINE_AI_TRANSITION_PROVIDER → 显式 None(fail-closed,0 外呼)。"""
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PROVIDER", "")
    assert resolve_default_provider() is None


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
        node_generate_ai_transition,
        node_plan_timeline_ai_transition,
    )

# ---------------------------------------------------------------------------
# 20261114 阶段2:stub 语义修正 / LLM 候选白名单投影 / 新情绪标签 / 本机曲库接入
# ---------------------------------------------------------------------------
class _CapturingLLMClient:
    """捕获 prompt 的假 client(chat_json 只要求 .chat)。"""

    def __init__(self, response: dict[str, str]) -> None:
        self._response = response
        self.captured: dict[str, str] = {}

    def chat(self, *, system_prompt: str, user_prompt: str, media=None,
             json_schema=None, temperature: float = 0.3, max_tokens: int = 4096) -> str:
        self.captured = {"system_prompt": system_prompt, "user_prompt": user_prompt}
        import json as _json
        return _json.dumps(self._response, ensure_ascii=False)


def _cache_file(tmp_path: Path, name: str = "m.mp3") -> Path:
    p = tmp_path / name
    p.write_bytes(b"x" * 128)
    return p


def test_select_bgm_default_match_with_valid_cache_is_not_stub(tmp_path: Path) -> None:
    """修订 §2.5:stub 只由缓存有效性决定,与选择策略(method)无关。

    说明:stub 环境下 LLM 让位启发式,单候选时 method="heuristic";
    default_match 标签仅在空库路径可达(空库 → empty selection,仍 stub=True,
    由 test_select_bgm_empty_library_returns_empty 覆盖)。
    """
    cache = _cache_file(tmp_path)
    lib = [{
        "bgm_id": "m1", "name": "旅途的风景", "path": "", "cache_path": str(cache),
        "mood": ["calm"], "tempo": "unknown", "duration_ms": 50000,
        "music_id": "m1", "category_name": "会员热榜", "asset_kind": "jianying_music",
    }]
    res = select_bgm(user_request="", groups=[], library=lib, default_match={"bgm_id": "m1"})
    assert res["method"] == "heuristic"
    assert res["stub"] is False
    assert res["asset_available"] is True
    assert res["selection_source"] == "heuristic"
    assert res["music_id"] == "m1"
    assert res["music_name"] == "旅途的风景"
    assert res["cache_path"] == str(cache)
    assert res["asset_kind"] == "jianying_music"
    assert res["tempo"] == "unknown"


def test_select_bgm_missing_cache_is_stub(tmp_path: Path) -> None:
    """缓存文件缺失 → stub=True(哪怕 method 不是 default_match)。"""
    lib = [{
        "bgm_id": "m1", "path": "", "cache_path": str(tmp_path / "gone.mp3"),
        "mood": ["calm"], "tempo": "unknown", "duration_ms": 50000,
    }]
    res = select_bgm(user_request="", groups=[], library=lib, default_match={"bgm_id": "m1"})
    assert res["stub"] is True
    assert res["asset_available"] is False


def test_select_bgm_yaml_placeholder_still_stub(tmp_path: Path) -> None:
    """旧 YAML 占位条目(path 为空):行为与阶段2之前一致(stub=True)。"""
    lib = [{
        "bgm_id": "bgm_calm_lofi", "path": "", "mood": ["calm"],
        "tempo": "medium", "duration_ms": 180000,
    }]
    res = select_bgm(user_request="", groups=[], library=lib, default_match={"bgm_id": "bgm_calm_lofi"})
    assert res["stub"] is True


def test_select_bgm_duration_falls_back_to_duration_us() -> None:
    """新曲库条目只有 duration_us 时按 //1000 规范换算。"""
    lib = [{
        "bgm_id": "m1", "path": "", "mood": [], "tempo": "unknown",
        "duration_us": 190766666,
    }]
    res = select_bgm(user_request="", groups=[], library=lib, default_match={})
    assert res["duration_ms"] == 190766


def test_rank_candidates_travel_and_fresh_hit() -> None:
    """新增 travel/fresh 标签:"旅行"命中 travel,"清新"命中 fresh。"""
    lib = [
        {"bgm_id": "travel_1", "mood": ["travel", "fresh"]},
        {"bgm_id": "calm_1", "mood": ["calm", "soft"]},
        {"bgm_id": "upbeat_1", "mood": ["happy"]},
    ]
    ranked = rank_candidates(library=lib, user_request="旅行视频", narration_text="")
    assert ranked[0]["bgm_id"] == "travel_1"
    ranked = rank_candidates(library=lib, user_request="清新舒缓旅行", narration_text="")
    # 非命中条目不会被剔除(upbeat_1 仍在列表尾),但前二必须是命中者
    assert {r["bgm_id"] for r in ranked[:2]} == {"travel_1", "calm_1"}
    assert ranked[-1]["bgm_id"] == "upbeat_1"
    assert _match_mood_tags("清新旅行vlog") == {"fresh", "travel"}


def test_rank_candidates_llm_prompt_projection(tmp_path: Path) -> None:
    """修订 §4.3 红线:本机缓存路径/request_id/原始素材对象不进 LLM prompt。"""
    cache = _cache_file(tmp_path)
    candidates = [{
        "bgm_id": "m1", "name": "旅途的风景", "mood": ["calm"], "tempo": "unknown",
        "duration_ms": 1000, "notes": "钢琴", "cache_path": str(cache),
        "request_id": "REQ_SECRET", "path": str(cache),
        "raw_material": {"secret_field": 1},
    }]
    fake = _CapturingLLMClient({"bgm_id": "m1", "reason": "ok"})
    res = rank_candidates_with_llm(candidates=candidates, user_request="u", client=fake)
    assert res and res["bgm_id"] == "m1"
    prompt = fake.captured["user_prompt"]
    assert "旅途的风景" in prompt and "m1" in prompt
    for banned in ("cache_path", "request_id", "raw_material", "REQ_SECRET", "secret_field"):
        assert banned not in prompt


def test_load_bgm_library_prefers_local_music_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本机曲库非空时优先;契约外层形状不变。"""
    cache = _cache_file(tmp_path)
    entry = {
        "music_id": "cloud1", "name": "云端曲", "duration_us": 60000000,
        "mood": ["calm"], "tempo": "unknown", "is_default": True,
        "raw_material": {"path": str(cache)},
    }
    idx = tmp_path / "lib.jsonl"
    idx.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JY_MUSIC_LIBRARY_PATH", str(idx))
    meta = load_bgm_library()
    assert [e["bgm_id"] for e in meta["library"]] == ["cloud1"]
    assert meta["default_match"] == {"bgm_id": "cloud1"}
    assert meta["matching"]["max_candidates_for_llm"] == 10


def test_load_bgm_library_falls_back_to_yaml_when_local_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """索引缺失 → 回退 YAML 占位库(旧环境兼容)。"""
    monkeypatch.setenv("JY_MUSIC_LIBRARY_PATH", str(tmp_path / "missing.jsonl"))
    meta = load_bgm_library()
    assert len(meta["library"]) >= 3
    assert all(e["bgm_id"].startswith("bgm_") for e in meta["library"])


# ---------------------------------------------------------------------------
# 20261114 阶段3:云音乐模式抑制 mapper 占位音轨(audio.bgm_ref=None)
# ---------------------------------------------------------------------------
def _plan_groups_min() -> list[dict[str, Any]]:
    return [
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


def test_plan_timeline_pro_cloud_music_suppresses_bgm_ref() -> None:
    """selection 带 music_id(云音乐模式)→ audio.bgm_ref=None,节拍仍驱动 beat_align。"""
    from storyline.contract import CanonicalTimeline

    cloud_bgm = {
        "bgm_ref": "7553871402870491155",
        "music_id": "7553871402870491155",
        "beats": [3000, 6000],
        "duration_ms": 9000,
        "beat_period_ms": 3000,
    }
    timeline = plan_timeline_pro(
        groups=_plan_groups_min(),
        voiceover={},
        bgm_selection=cloud_bgm,
        targets={"target_duration_ms": 9000},
        source_media=[
            {
                "media_id": "media_0001",
                "file_uri": "file:///x.mp4",
                "duration_ms": 5000,
                "media_type": "video",
            }
        ],
        job_id="job_cloud",
    )
    assert timeline["audio"]["bgm_ref"] is None
    # 节拍规划与 audio.bgm_ref 相互独立:beat 仍参与切镜节奏(method=beat_align)
    assert timeline["method"] == "beat_align"
    # 契约不破:CanonicalTimeline(extra="forbid")照常校验通过
    CanonicalTimeline.model_validate(timeline)


def test_plan_timeline_pro_legacy_selection_bgm_ref_unchanged() -> None:
    """旧 selection(无 music_id):bgm_ref 原样写入,行为逐字节不变。"""
    legacy_bgm = {
        "bgm_ref": "bgm_story_warm",
        "beats": [3000, 6000],
        "duration_ms": 9000,
        "beat_period_ms": 3000,
    }
    timeline = plan_timeline_pro(
        groups=_plan_groups_min(),
        voiceover={},
        bgm_selection=legacy_bgm,
        targets={"target_duration_ms": 9000},
        source_media=[
            {
                "media_id": "media_0001",
                "file_uri": "file:///x.mp4",
                "duration_ms": 5000,
                "media_type": "video",
            }
        ],
        job_id="job_legacy",
    )
    assert timeline["audio"]["bgm_ref"] == "bgm_story_warm"
