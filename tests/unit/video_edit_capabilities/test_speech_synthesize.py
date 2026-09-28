"""``video_edit_capabilities.speech_synthesize`` 单测 — 9 工具迁移 §7.1 行 8-9。

覆盖用例(对照 9 工具迁移计划 §7.1 行 8-9):

- ``speech_synthesize``:
  - 空 ``text`` → ``[ERROR] text is required``
  - ``preferred_provider="unknown"`` → ``[ERROR] unknown preferred_provider``
  - mock ``call_firered_tts`` 前 2 次抛 ``TTSUnavailable``、第 3 次成功
    → 最终成功,``time.sleep`` 调用 2 次,退避间隔 ``3s → 6s``
  - mock 始终失败 → ``[ERROR] all TTS attempts failed``
  - ``speed=0`` / ``speed="abc"`` → ``[ERROR]``
  - ``retries="abc"`` → ``[ERROR] retries must be an integer``
- ``tts_generate``:
  - 与 ``speech_synthesize`` 返回值一致(对照 ``tts.py:46-48`` 一行转发)
- ``coerce_finite_number`` / ``coerce_retry_count``:边界值
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from assembly_capabilities.run_context import RunContext
from jy_common.tts_client import TTSUnavailable
from video_edit_capabilities.speech_synthesize import (
    TTS_DEFAULT_RETRIES,
    coerce_finite_number,
    coerce_retry_count,
    speech_synthesize,
    tts_generate,
)


# ---------------------------------------------------------------------------
# 1. 入参校验
# ---------------------------------------------------------------------------
def test_speech_synthesize_empty_text_returns_error() -> None:
    """空 text → ``[ERROR] text is required``(对照工具的入参校验)。"""
    ctx = RunContext()
    out = speech_synthesize({}, ctx)
    assert out.text.startswith("[ERROR]")
    assert "text is required" in out.text


def test_speech_synthesize_whitespace_text_returns_error() -> None:
    """只有空白的 text 也视为空。"""
    ctx = RunContext()
    out = speech_synthesize({"text": "   \n\t  "}, ctx)
    assert out.text.startswith("[ERROR]")
    assert "text is required" in out.text


def test_speech_synthesize_unknown_provider_returns_error() -> None:
    """``preferred_provider="bogus"`` → ``[ERROR]``,不进入重试循环。"""
    ctx = RunContext()
    out = speech_synthesize(
        {"text": "hi", "preferred_provider": "bogus", "retries": 0},
        ctx,
    )
    assert out.text.startswith("[ERROR]")
    assert "unknown preferred_provider" in out.text
    assert out.data.get("param_error") is True


def test_speech_synthesize_speed_zero_returns_error() -> None:
    """``speed=0`` → ``[ERROR] speed must be a finite number > 0``。"""
    ctx = RunContext()
    out = speech_synthesize({"text": "hi", "speed": 0}, ctx)
    assert out.text.startswith("[ERROR]")
    assert "speed" in out.text


def test_speech_synthesize_speed_non_numeric_returns_error() -> None:
    """``speed="abc"`` → ``[ERROR]``。"""
    ctx = RunContext()
    out = speech_synthesize({"text": "hi", "speed": "abc"}, ctx)
    assert out.text.startswith("[ERROR]")
    assert "speed must be numeric" in out.text


def test_speech_synthesize_retries_non_integer_returns_error() -> None:
    """``retries="abc"`` → ``[ERROR] retries must be an integer``。"""
    ctx = RunContext()
    out = speech_synthesize({"text": "hi", "retries": "abc"}, ctx)
    assert out.text.startswith("[ERROR]")
    assert "retries must be an integer" in out.text


# ---------------------------------------------------------------------------
# 2. 重试逻辑 — mock call_firered_tts 失败 N 次后成功
# ---------------------------------------------------------------------------
def test_speech_synthesize_succeeds_after_two_failures() -> None:
    """mock ``call_firered_tts`` 前 2 次抛 ``TTSUnavailable``、第 3 次成功
    → 最终成功,``time.sleep`` 调用 2 次,退避 ``3s → 6s``(2^0=1, 2^1=2)。
    """
    ctx = RunContext()

    success_payload = {
        "audio_path": "/tmp/fake.wav",
        "duration_ms": 1234,
        "voice": None,
        "speed": None,
    }
    failures = [TTSUnavailable("transient 1"), TTSUnavailable("transient 2")]

    def fake_call(text, *, voice=None, speed=None):
        if failures:
            raise failures.pop(0)
        return success_payload

    sleep_durations: list[float] = []
    with (
        patch("video_edit_capabilities.speech_synthesize.call_firered_tts", side_effect=fake_call),
        patch("video_edit_capabilities.speech_synthesize.time.sleep", side_effect=lambda s: sleep_durations.append(s)),
    ):
        out = speech_synthesize(
            {"text": "hello world", "preferred_provider": "firered", "retries": 3},
            ctx,
        )

    assert not out.text.startswith("[ERROR]"), out.text
    assert out.data["audio_path"] == "/tmp/fake.wav"
    assert out.data["duration_ms"] == 1234
    assert out.data["selected_provider"] == "firered"
    assert out.data["retry_attempts"] == 2  # 0-indexed:第 3 次才成功
    # 退避 3s → 6s(backoff * 2^0, backoff * 2^1)
    assert sleep_durations == [3.0, 6.0]


def test_speech_synthesize_returns_error_when_all_attempts_fail() -> None:
    """mock 始终失败 → ``[ERROR] all TTS attempts failed``,包含每次的失败原因。"""
    ctx = RunContext()

    def always_fail(text, *, voice=None, speed=None):
        raise TTSUnavailable("service unavailable")

    with (
        patch("video_edit_capabilities.speech_synthesize.call_firered_tts", side_effect=always_fail),
        patch("video_edit_capabilities.speech_synthesize.time.sleep"),
    ):
        out = speech_synthesize(
            {"text": "hi", "preferred_provider": "firered", "retries": 2},
            ctx,
        )

    assert out.text.startswith("[ERROR]")
    assert "all TTS attempts failed" in out.text
    assert out.data["selected_provider"] == "firered"
    assert out.data["max_retries"] == 2
    failures = out.data["failures"]
    assert len(failures) == 3  # retries=2 → 3 次尝试
    assert all("service unavailable" in f["error"] for f in failures)


def test_speech_synthesize_retries_zero_no_sleep_on_failure() -> None:
    """``retries=0`` 时第 1 次失败直接返回,无 sleep 调用。"""
    ctx = RunContext()

    def always_fail(text, *, voice=None, speed=None):
        raise TTSUnavailable("nope")

    sleep_calls: list[float] = []
    with (
        patch("video_edit_capabilities.speech_synthesize.call_firered_tts", side_effect=always_fail),
        patch("video_edit_capabilities.speech_synthesize.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
    ):
        out = speech_synthesize({"text": "hi", "retries": 0}, ctx)

    assert out.text.startswith("[ERROR]")
    assert sleep_calls == []


# ---------------------------------------------------------------------------
# 3. 一次性成功 — mock 直接返回音频路径
# ---------------------------------------------------------------------------
def test_speech_synthesize_success_path() -> None:
    """首次就成功:无 sleep,data 含 audio_path / duration_ms。"""
    ctx = RunContext()

    payload = {
        "audio_path": "/out/tts.wav",
        "duration_ms": 500,
        "voice": "en_female_1",
        "speed": 1.0,
    }
    sleep_calls: list[float] = []
    with (
        patch("video_edit_capabilities.speech_synthesize.call_firered_tts", return_value=payload),
        patch("video_edit_capabilities.speech_synthesize.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
    ):
        out = speech_synthesize(
            {"text": "Hello world", "voice": "en_female_1", "speed": 1.0, "preferred_provider": "cloud_tts"},
            ctx,
        )

    assert not out.text.startswith("[ERROR]")
    assert out.data["audio_path"] == "/out/tts.wav"
    assert out.data["duration_ms"] == 500
    assert out.data["voice"] == "en_female_1"
    assert out.data["speed"] == 1.0
    assert out.data["retry_attempts"] == 0
    assert sleep_calls == []


# ---------------------------------------------------------------------------
# 4. tts_generate — 一行转发,语义与 speech_synthesize 一致
# ---------------------------------------------------------------------------
def test_tts_generate_is_thin_forwarder() -> None:
    """``tts_generate`` 与 ``speech_synthesize`` 返回值结构完全一致(对照 9 工具迁移 §11 验收
    项 8:``tts_generate`` 确认只是一行转发,未实现成第二份独立逻辑)。

    只比较结构性字段(audio_path / duration_ms / selected_provider / retry_attempts),
    不比较 ``elapsed_seconds``(两次相邻调用的耗时会有微秒级差异)。
    """
    ctx = RunContext()
    payload = {"audio_path": "/out/x.wav", "duration_ms": 100}

    with patch("video_edit_capabilities.speech_synthesize.call_firered_tts", return_value=payload):
        out_a = tts_generate({"text": "hi"}, ctx)
        out_b = speech_synthesize({"text": "hi"}, ctx)

    # 同入参 + 同底层 mock → 两个工具的结构性字段应完全一致
    assert out_a.text == out_b.text
    assert out_a.data["audio_path"] == out_b.data["audio_path"]
    assert out_a.data["duration_ms"] == out_b.data["duration_ms"]
    assert out_a.data["selected_provider"] == out_b.data["selected_provider"]
    assert out_a.data["retry_attempts"] == out_b.data["retry_attempts"]


def test_tts_generate_success_returns_same_payload_as_speech_synthesize() -> None:
    """成功路径:两者返回相同 data(对照 ``tts.py:46-48`` 一行转发)。"""
    ctx = RunContext()
    payload = {"audio_path": "/out/x.wav", "duration_ms": 100}

    with patch("video_edit_capabilities.speech_synthesize.call_firered_tts", return_value=payload):
        out_a = tts_generate({"text": "hello"}, ctx)
        out_b = speech_synthesize({"text": "hello"}, ctx)

    assert out_a.data["audio_path"] == out_b.data["audio_path"]
    assert out_a.data["duration_ms"] == out_b.data["duration_ms"]


# ---------------------------------------------------------------------------
# 5. helpers 边界
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value, expected",
    [
        (1.5, 1.5),
        ("2.0", 2.0),
        (3, 3.0),
        (True, None),  # bool 不应被解析为数字
        (float("inf"), None),
        (float("nan"), None),
        ("abc", None),
        (None, None),
    ],
)
def test_coerce_finite_number(value, expected) -> None:
    """``coerce_finite_number`` 的所有边界。"""
    assert coerce_finite_number(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, TTS_DEFAULT_RETRIES),
        (3, 3),
        ("5", 5),
        (True, "ToolResult"),  # bool 触发 ToolResult
        ("abc", "ToolResult"),
        (-1, "ToolResult"),
        (11, "ToolResult"),  # > TTS_MAX_RETRIES
    ],
)
def test_coerce_retry_count(value, expected) -> None:
    """``coerce_retry_count`` 边界:合法值 / 非法值 / bool。"""
    out = coerce_retry_count(value)
    if expected == "ToolResult":
        assert hasattr(out, "text")
        assert out.text.startswith("[ERROR]")
    else:
        assert out == expected
