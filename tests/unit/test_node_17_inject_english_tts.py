"""node_17_inject_english_tts 单测 — 9 工具迁移 §6.3 / §6.4 阶段四。

覆盖:
- 空 segments → en_audio_path=None,error_log 不增(无错误,只是没内容)
- 调 ``speech_synthesize`` 成功 → 写 en_audio_path / en_dub_audio_path,字段一致
- ``speech_synthesize`` 失败 → en_audio_path=None + error_log 追加 + tts_issue 非空
- delta-only 返回:不包含上游字段
- 文本拼接:多段 text_en 用空格拼成一段
- ``tts_run_id`` 12 位 hex(状态审计字段)
- ``status_log`` 含 ``node_17_inject_english_tts_done``
"""
from __future__ import annotations

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
