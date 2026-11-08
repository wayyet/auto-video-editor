"""node_17_inject_english_tts 单测 — 9 工具迁移 §6.3 / §6.4 阶段四。

覆盖:
- 空 segments → en_audio_path=None,error_log 不增(无错误,只是没内容)
- 调 ``speech_synthesize`` 成功 → 写 en_audio_path / en_dub_audio_path,字段一致
- ``speech_synthesize`` 失败 → en_audio_path=None + error_log 追加 + tts_issue 非空
- delta-only 返回:不包含上游字段
- 文本拼接:多段 text_en 用空格拼成一段
- ``tts_run_id`` 12 位 hex(状态审计字段)
- ``status_log`` 含 ``node_17_inject_english_tts_done``
- P3b:成功路径把英文配音写进英文分支草稿(en_voiceover 标记轨 + material)
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from assembly_capabilities.result import ToolResult
from nodes.node_17_inject_english_tts import node_17_inject_english_tts


def _empty_state() -> dict:
    return {"status_log": [], "error_log": []}


def _state_with_segments(segments: list[dict]) -> dict:
    return {
        "subtitle_segments_en": segments,
        "status_log": [],
        "error_log": [],
    }


# ---------------------------------------------------------------------------
# 1. 空字幕段 → 不报错,只返回 None + done 标记
# ---------------------------------------------------------------------------
def test_node_17_empty_segments_returns_none() -> None:
    """空 subtitle_segments_en → en_audio_path=None,无 error_log 增量。"""
    state = _state_with_segments([])
    out = node_17_inject_english_tts(state)

    assert out["en_audio_path"] is None
    assert out["en_dub_audio_path"] is None
    assert out["tts_issue"] is not None
    assert "empty_text" in out["tts_issue"]
    assert "node_17_inject_english_tts_done" in out["status_log"]
    # 没有错误,error_log 不应追加
    assert out.get("error_log", []) == []


def test_node_17_missing_segments_key_returns_none() -> None:
    """state 完全没 subtitle_segments_en 字段 → 同样走空路径。"""
    state = _empty_state()
    out = node_17_inject_english_tts(state)

    assert out["en_audio_path"] is None
    assert out["en_dub_audio_path"] is None


# ---------------------------------------------------------------------------
# 2. 成功路径
# ---------------------------------------------------------------------------
def test_node_17_success_writes_audio_path_and_double_writes_alias() -> None:
    """``speech_synthesize`` 成功 → 写 en_audio_path 与 en_dub_audio_path(双写)。"""
    fake_result = ToolResult(
        text="firered TTS synthesized 11 chars in 0.5s",
        data={"audio_path": "/out/tts.wav", "duration_ms": 500},
    )
    segments = [
        {"index": 0, "start_ms": 0, "end_ms": 1000, "text_en": "Hello"},
        {"index": 1, "start_ms": 1000, "end_ms": 2000, "text_en": "World"},
    ]
    state = _state_with_segments(segments)

    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        return_value=fake_result,
    ):
        out = node_17_inject_english_tts(state)

    # 双写兼容:Week 4 / Week 5 字段值一致
    assert out["en_audio_path"] == "/out/tts.wav"
    assert out["en_dub_audio_path"] == "/out/tts.wav"
    assert out["tts_issue"] is None
    assert "node_17_inject_english_tts_done" in out["status_log"]
    # 成功不应追加 error_log
    assert out.get("error_log", []) == []


def test_node_17_text_joining_concatenates_segments_with_space() -> None:
    """多段 text_en 用空格拼接(对照 §6.3 step 5)。"""
    captured_args: list[dict] = []

    def capture(args, ctx):
        captured_args.append(args)
        return ToolResult(
            text="ok",
            data={"audio_path": "/x.wav", "duration_ms": 100},
        )

    segments = [
        {"index": 0, "text_en": "Hello"},
        {"index": 1, "text_en": "World"},
        {"index": 2, "text_en": "Foo"},
    ]
    state = _state_with_segments(segments)

    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        side_effect=capture,
    ):
        node_17_inject_english_tts(state)

    assert len(captured_args) == 1
    assert captured_args[0]["text"] == "Hello World Foo"


def test_node_17_skips_segments_with_empty_text_en() -> None:
    """text_en 为空的段被跳过(避免 join 后留连续空格)。"""
    captured_args: list[dict] = []

    def capture(args, ctx):
        captured_args.append(args)
        return ToolResult(
            text="ok",
            data={"audio_path": "/x.wav", "duration_ms": 100},
        )

    segments = [
        {"index": 0, "text_en": "Hello"},
        {"index": 1, "text_en": "  "},  # 全空白
        {"index": 2, "text_en": "World"},
    ]
    state = _state_with_segments(segments)

    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        side_effect=capture,
    ):
        node_17_inject_english_tts(state)

    assert captured_args[0]["text"] == "Hello World"


def test_node_17_tts_run_id_is_12_hex() -> None:
    """``tts_run_id`` 是 12 位 hex(uuid4 前 12 位),便于审计回溯。"""
    fake_result = ToolResult(
        text="ok",
        data={"audio_path": "/x.wav", "duration_ms": 1},
    )
    state = _state_with_segments([{"text_en": "hi"}])

    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        return_value=fake_result,
    ):
        out = node_17_inject_english_tts(state)

    run_id = out["tts_run_id"]
    assert isinstance(run_id, str)
    assert len(run_id) == 12
    int(run_id, 16)  # 必须可解析为 hex


def test_node_17_uses_firered_provider_and_3_retries() -> None:
    """调 ``speech_synthesize`` 时 preferred_provider=firered,retries=3
    (对照 9 工具迁移 §6.3 step 5)。
    """
    captured_args: list[dict] = []

    def capture(args, ctx):
        captured_args.append(args)
        return ToolResult(
            text="ok",
            data={"audio_path": "/x.wav", "duration_ms": 1},
        )

    state = _state_with_segments([{"text_en": "hi"}])

    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        side_effect=capture,
    ):
        node_17_inject_english_tts(state)

    sent = captured_args[0]
    assert sent["preferred_provider"] == "firered"
    assert sent["retries"] == 3


# ---------------------------------------------------------------------------
# 3. 失败路径
# ---------------------------------------------------------------------------
def test_node_17_failure_writes_none_and_logs_error() -> None:
    """``speech_synthesize`` 返回 ``[ERROR]`` → en_audio_path=None +
    error_log 追加 + tts_issue 非空。
    """
    fake_result = ToolResult(
        text="[ERROR] all TTS attempts failed: service unavailable",
        data={"selected_provider": "firered", "max_retries": 3, "failures": []},
    )
    state = _state_with_segments([{"text_en": "hi"}])

    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        return_value=fake_result,
    ):
        out = node_17_inject_english_tts(state)

    # 不阻塞主链:en_audio_path 写 None,而不是抛异常
    assert out["en_audio_path"] is None
    assert out["en_dub_audio_path"] is None
    assert "all TTS attempts failed" in out["tts_issue"]
    # error_log 追加(便于审计)
    error_log = out["error_log"]
    assert any("node_17" in e and "TTS failed" in e for e in error_log)
    # 不调用 interrupt,主链继续
    assert "interrupt" not in out
    assert "node_17_inject_english_tts_done" in out["status_log"]


def test_node_17_failure_preserves_status_log_only_delta() -> None:
    """delta-only:节点不返回上游 status_log,LangGraph reducer 会合并。"""
    fake_result = ToolResult(
        text="[ERROR] fail",
        data={"failures": []},
    )
    state = _state_with_segments([{"text_en": "hi"}])

    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        return_value=fake_result,
    ):
        out = node_17_inject_english_tts(state)

    assert out["status_log"] == ["node_17_inject_english_tts_done"]


# ---------------------------------------------------------------------------
# 4. delta-only 行为
# ---------------------------------------------------------------------------
def test_node_17_delta_only_does_not_echo_input_state() -> None:
    """节点不返回上游字段(draft_path / asr_segments_zh 等)。"""
    fake_result = ToolResult(
        text="ok",
        data={"audio_path": "/x.wav", "duration_ms": 1},
    )
    state = {
        "subtitle_segments_en": [{"text_en": "hi"}],
        "draft_path": "/x/y.json",
        "video_input_path": "/v.mp4",
        "asr_segments_zh": [{"index": 0, "text_zh": "你"}],
        "status_log": [],
        "error_log": [],
    }

    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        return_value=fake_result,
    ):
        out = node_17_inject_english_tts(state)

    assert "draft_path" not in out
    assert "video_input_path" not in out
    assert "asr_segments_zh" not in out
    assert "subtitle_segments_en" not in out


# ---------------------------------------------------------------------------
# 5. 异常 → 透传到外层(节点本身不静默吞)
# ---------------------------------------------------------------------------
def test_node_17_unexpected_exception_propagates() -> None:
    """speech_synthesize 抛非 TTSUnavailable 异常时(比如编程错误),
    节点不静默吞 — 让 LangGraph superstep 报错,避免脏状态。
    """
    state = _state_with_segments([{"text_en": "hi"}])

    def boom(args, ctx):
        raise RuntimeError("unexpected")

    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        side_effect=boom,
    ):
        with pytest.raises(RuntimeError):
            node_17_inject_english_tts(state)


# ---------------------------------------------------------------------------
# 6. P3b:英文配音写进英文分支草稿
# ---------------------------------------------------------------------------
_EN_VOICEOVER_TRACK_NAME = "en_voiceover"


def _write_en_draft(tmp_path: Path, draft: dict) -> Path:
    p = tmp_path / "en_branch" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _minimal_en_draft(duration_us: int = 35_000_000) -> dict:
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": duration_us,
        "materials": {},
        "tracks": [
            {"type": "video", "segments": [
                {"id": "v1", "target_timerange": {"start": 0, "duration": duration_us}}]},
        ],
    }


def _run_node_17_with_en_draft(tmp_path: Path, duration_ms: int = 3000) -> tuple[dict, Path]:
    """成功 mock 下跑节点,返回 (节点输出, 英文草稿文件路径)。"""
    en_file = _write_en_draft(tmp_path, _minimal_en_draft())
    fake_result = ToolResult(
        text="ok",
        data={"audio_path": str(tmp_path / "tts.wav"), "duration_ms": duration_ms},
    )
    state = {
        "subtitle_segments_en": [{"text_en": "hello"}],
        "status_log": [],
        "error_log": [],
        "draft_dir_en_branch": str(en_file.parent),
    }
    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        return_value=fake_result,
    ):
        out = node_17_inject_english_tts(state)
    return out, en_file


def test_node_17_writes_en_voiceover_track(tmp_path: Path) -> None:
    """P3b:成功 → 英文草稿多一条 en_voiceover 标记配音轨 + en-dub-1 material。"""
    out, en_file = _run_node_17_with_en_draft(tmp_path, duration_ms=3000)

    draft = json.loads(en_file.read_text(encoding="utf-8"))
    voiceover_tracks = [
        t for t in draft["tracks"] if t.get("name") == _EN_VOICEOVER_TRACK_NAME
    ]
    assert len(voiceover_tracks) == 1
    track = voiceover_tracks[0]
    assert track["type"] == "audio"
    seg = track["segments"][0]
    assert seg["material_id"] == "en-dub-1"
    # 3000ms < 草稿 35s → 不触发 clamp
    assert seg["target_timerange"] == {"start": 0, "duration": 3_000_000}

    audio_materials = {m["id"]: m for m in draft["materials"]["audios"]}
    assert audio_materials["en-dub-1"]["path"].endswith("tts.wav")
    assert audio_materials["en-dub-1"]["duration_ms"] == 3000

    assert "node_17_en_voiceover_written" in out["status_log"]
    assert out.get("error_log", []) == []
    # state 双写契约不变
    assert out["en_audio_path"] == out["en_dub_audio_path"]


def test_node_17_en_voiceover_clamps_to_draft_duration(tmp_path: Path) -> None:
    """配音时长超过草稿时长 → clamp 到草稿总时长。"""
    out, en_file = _run_node_17_with_en_draft(tmp_path, duration_ms=60_000)
    draft = json.loads(en_file.read_text(encoding="utf-8"))
    track = next(
        t for t in draft["tracks"] if t.get("name") == _EN_VOICEOVER_TRACK_NAME
    )
    assert track["segments"][0]["target_timerange"]["duration"] == 35_000_000


def test_node_17_en_voiceover_idempotent(tmp_path: Path) -> None:
    """重放(断点恢复重跑)→ 配音轨与 material 不翻倍。"""
    en_file = _write_en_draft(tmp_path, _minimal_en_draft())
    fake_result = ToolResult(
        text="ok",
        data={"audio_path": str(tmp_path / "tts.wav"), "duration_ms": 3000},
    )
    state = {
        "subtitle_segments_en": [{"text_en": "hello"}],
        "status_log": [],
        "error_log": [],
        "draft_dir_en_branch": str(en_file.parent),
    }
    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        return_value=fake_result,
    ):
        node_17_inject_english_tts(state)
        node_17_inject_english_tts(state)

    draft = json.loads(en_file.read_text(encoding="utf-8"))
    assert (
        len([t for t in draft["tracks"] if t.get("name") == _EN_VOICEOVER_TRACK_NAME])
        == 1
    )
    assert len([m for m in draft["materials"]["audios"] if m["id"] == "en-dub-1"]) == 1


def test_node_17_skips_draft_write_without_fork() -> None:
    """fork 早退(draft_dir_en_branch 缺失)→ 静默跳过草稿写入,state 契约不变。"""
    fake_result = ToolResult(text="ok", data={"audio_path": "/x.wav", "duration_ms": 1})
    state = {"subtitle_segments_en": [{"text_en": "hi"}], "status_log": [], "error_log": []}
    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        return_value=fake_result,
    ):
        out = node_17_inject_english_tts(state)
    assert out["en_audio_path"] == "/x.wav"
    assert out.get("error_log", []) == []
    assert "node_17_en_voiceover_written" not in out["status_log"]


def test_node_17_en_draft_missing_file_notes(tmp_path: Path) -> None:
    """fork 跑了但英文草稿文件缺失 → error_log 降级 note,不抛异常。"""
    fake_result = ToolResult(text="ok", data={"audio_path": "/x.wav", "duration_ms": 1})
    state = {
        "subtitle_segments_en": [{"text_en": "hi"}],
        "status_log": [],
        "error_log": [],
        "draft_dir_en_branch": str(tmp_path / "nope"),
    }
    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        return_value=fake_result,
    ):
        out = node_17_inject_english_tts(state)
    assert any("英文草稿不存在" in e for e in out["error_log"])
    assert "node_17_en_voiceover_written" not in out["status_log"]


def test_node_17_duration_unknown_skips_track(tmp_path: Path) -> None:
    """duration_ms 缺失(服务异常返回)→ 不写 0 长段,记 error_log。"""
    en_file = _write_en_draft(tmp_path, _minimal_en_draft())
    fake_result = ToolResult(
        text="ok", data={"audio_path": str(tmp_path / "tts.wav"), "duration_ms": 0}
    )
    state = {
        "subtitle_segments_en": [{"text_en": "hello"}],
        "status_log": [],
        "error_log": [],
        "draft_dir_en_branch": str(en_file.parent),
    }
    with patch(
        "nodes.node_17_inject_english_tts.speech_synthesize",
        return_value=fake_result,
    ):
        out = node_17_inject_english_tts(state)
    draft = json.loads(en_file.read_text(encoding="utf-8"))
    assert not [t for t in draft["tracks"] if t.get("name") == _EN_VOICEOVER_TRACK_NAME]
    assert any("duration" in e for e in out["error_log"])
