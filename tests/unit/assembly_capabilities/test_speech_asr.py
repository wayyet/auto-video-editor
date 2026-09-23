"""``speech_asr`` 的纯逻辑测试(不调云端 ASR)。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from assembly_capabilities.speech_asr import (
    _build_transcript_payload,
    collect_text_segments,
    first_present,
    speech_transcribe,
)
from assembly_capabilities.result import ToolResult


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------
def test_speech_transcribe_requires_input_path(run_context):
    r = speech_transcribe({}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "input_path" in r.text


def test_speech_transcribe_rejects_missing_input_file(run_context, tmp_path):
    r = speech_transcribe({"input_path": str(tmp_path / "missing.mp4")}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "File not found" in r.text


def test_speech_transcribe_without_transcript_returns_helpful_error(run_context, tmp_path):
    """核心契约:本仓库未配置云 ASR,空 transcript 时返回明确错误而非默默成功。"""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    r = speech_transcribe({"input_path": str(src)}, run_context)
    assert r.text.startswith("[ERROR]")
    assert "cloud ASR is not configured" in r.text
    assert "transcript_path" in r.text
    assert "inline_text" in r.text
    # data 应带 recoverable=False hint
    assert r.data.get("recoverable") is False
    assert "supply transcript_path or inline_text" in r.data.get("hint", "")


def test_speech_transcribe_rejects_missing_transcript_path(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    r = speech_transcribe(
        {"input_path": str(src), "transcript_path": str(tmp_path / "missing.json")},
        run_context,
    )
    assert r.text.startswith("[ERROR]")
    assert "transcript_path not found" in r.text


# ---------------------------------------------------------------------------
# 外部 transcript 透传
# ---------------------------------------------------------------------------
def test_speech_transcribe_passes_through_inline_text(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    output = tmp_path / "transcript.json"
    r = speech_transcribe(
        {
            "input_path": str(src),
            "inline_text": "你好,世界",
            "output_json": str(output),
        },
        run_context,
    )
    assert not r.text.startswith("[ERROR]"), r.text
    assert output.is_file()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["provider"] == "external"
    assert payload["source_media"] == str(src)
    assert payload["text"] == "你好,世界"
    # data 字段契约
    assert r.data["provider"] == "external"
    assert r.data["channel"] == "external_passthrough"
    assert r.data["output_json"] == str(output)
    assert r.data["language"] == "zh"
    assert r.data["segment_count"] == 1
    assert r.data["word_count"] == 0


def test_speech_transcribe_passes_through_transcript_path(run_context, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    # 准备已有 transcript.json(segments 数组)
    external = tmp_path / "external_transcript.json"
    external.write_text(
        json.dumps({"segments": [{"start": 0.0, "end": 1.0, "text": "first"},
                                 {"start": 1.0, "end": 2.0, "text": "second"}]}),
        encoding="utf-8",
    )
    output = tmp_path / "transcript.json"
    r = speech_transcribe(
        {
            "input_path": str(src),
            "transcript_path": str(external),
            "output_json": str(output),
        },
        run_context,
    )
    assert not r.text.startswith("[ERROR]"), r.text
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["provider"] == "external"
    assert payload["source_media"] == str(src)
    # 至少有一段被传递
    assert len(payload["segments"]) >= 1
    assert r.data["segment_count"] >= 1


def test_speech_transcribe_accepts_parsed_json_inline(run_context, tmp_path):
    """``inline_text`` 是已 JSON 序列化的 dict 时,应透传整段结构。"""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    inline = json.dumps({"language": "en", "segments": [{"start": 0, "end": 1, "text": "hi"}]})
    output = tmp_path / "transcript.json"
    r = speech_transcribe(
        {"input_path": str(src), "inline_text": inline, "output_json": str(output)},
        run_context,
    )
    assert not r.text.startswith("[ERROR]"), r.text
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["language"] == "en"
    assert payload["segments"][0]["text"] == "hi"


# ---------------------------------------------------------------------------
# _build_transcript_payload 纯函数
# ---------------------------------------------------------------------------
def test_build_payload_from_raw_text():
    p = _build_transcript_payload("plain text", "/tmp/in.mp4", language="zh")
    assert p["provider"] == "external"
    assert p["source_media"] == "/tmp/in.mp4"
    assert p["language"] == "zh"
    assert p["text"] == "plain text"
    assert p["segments"][0]["text"] == "plain text"


def test_build_payload_from_inline_json_dict():
    inline = json.dumps({"language": "en", "segments": [{"text": "a"}]})
    p = _build_transcript_payload(inline, "/tmp/in.mp4", language="zh")
    assert p["language"] == "en"
    assert p["segments"][0]["text"] == "a"


def test_build_payload_from_inline_json_list():
    inline = json.dumps([{"text": "seg1"}, {"text": "seg2"}])
    p = _build_transcript_payload(inline, "/tmp/in.mp4", language="zh")
    assert len(p["segments"]) == 2
    assert p["segments"][0]["text"] == "seg1"


def test_build_payload_from_empty_string():
    p = _build_transcript_payload("", "/tmp/in.mp4", language="zh")
    # empty string → falls through to raw text branch with empty text
    assert p["text"] == ""


def test_collect_text_segments_round_trip():
    payload = {"segments": [{"start": 0, "end": 1, "text": "hello"},
                            {"start": 1, "end": 2, "text": "world"}]}
    segs = collect_text_segments(payload)
    assert any("hello" in s for s in segs)
    assert any("world" in s for s in segs)


def test_first_present_returns_first_match():
    assert first_present({"a": 1, "b": 2}, ["a", "b"]) == 1
    assert first_present({"a": None, "b": 2}, ["a", "b"]) == 2
    assert first_present({}, ["a"]) is None
