"""``speech_asr.py``(替代 video-agent-kit 0.4.3 ``media.py`` 的 ASR 部分)。

按 ADR-1,本仓库完全解耦、**不调任何云端 ASR**。video-agent-kit 原版的
``speech_transcribe`` 100% 依赖 ``cloud_asr`` / ``zcode_speech`` / ``speech_service``
三个云服务模块(后者还需要 MCP 官方身份与 endpoint 配置),与本仓库"本地离线"
的设定不符。

本模块提供 ``speech_transcribe(args, ctx)``:
1. 若 ``args`` 携带 ``transcript_path`` 或 ``inline_text``,**直接读已有转写**
   写出标准 ``transcript.json``,**不调任何云端服务**。这是本仓库的常规用法
   (阶段五实施时,可由外部脚本/已有 FireRedASR 产物喂入)。
2. 否则返回一个明确的 ``[ERROR]`` ToolResult,告诉调用方"本仓库未配置云端
   ASR;请传入 transcript_path(外部已生成的 transcript.json)或 inline_text"。
3. 删除 video-agent-kit 全部 retry / chunk / 端点 fallback 逻辑——本仓库
   不该重做云服务编排。

返回值契约与 video-agent-kit 完全一致(``ToolResult(text, data, artifacts)``),
``data`` 字段含 ``provider="external"`` / ``tool="speech_transcribe"`` /
``output_json`` / ``source_media`` / ``language`` / ``segment_count`` / ``word_count``,
保证下游节点能一致处理。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .result import ToolResult
from .run_context import RunContext
from .transcript import (
    collect_segments,
    first_present,
    format_segment,
    read_transcript_text,
)


def speech_transcribe(args: dict, ctx: RunContext) -> ToolResult:
    """按 ``transcript_path`` / ``inline_text`` 直接写出标准 ``transcript.json``。

    不调任何云端 ASR(详见模块顶部说明)。语言默认 ``zh``,与原 video-agent-kit
    默认对齐。
    """
    input_path_arg = args.get("input_path")
    if not input_path_arg:
        return ToolResult(text="[ERROR] input_path is required")
    input_path = ctx.resolve(input_path_arg)
    if not input_path.is_file():
        return ToolResult(text=f"[ERROR] File not found: {input_path}")

    output_path = ctx.resolve(args.get("output_json") or "out/transcript.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    transcript_path_arg = args.get("transcript_path")
    inline_text = args.get("inline_text") or args.get("transcript_text")
    if not transcript_path_arg and not inline_text:
        return ToolResult(
            text=(
                "[ERROR] cloud ASR is not configured in this project. "
                "speech_transcribe requires either transcript_path (an external transcript.json) "
                "or inline_text (the full transcript string) to be supplied. "
                "The original video-agent-kit tool called a cloud ASR provider here; "
                "we deliberately do not — call sites in this project must arrange transcription "
                "externally (e.g. FireRedASR / vendor service) and pass the result back in."
            ),
            data={
                "provider": None,
                "tool": "speech_transcribe",
                "recoverable": False,
                "hint": "supply transcript_path or inline_text in args",
            },
        )

    transcript_path: Path | None = None
    transcript_text: str = ""
    if transcript_path_arg:
        transcript_path = ctx.resolve(transcript_path_arg)
        if not transcript_path.is_file():
            return ToolResult(text=f"[ERROR] transcript_path not found: {transcript_path}")
        transcript_text = read_transcript_text(transcript_path)
    if inline_text:
        transcript_text = inline_text if transcript_text == "" else transcript_text

    # 解析 segment / word 统计(供 data 字段)
    payload = _build_transcript_payload(transcript_text, str(input_path), language=str(args.get("language") or "zh"))
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    language = payload.get("language") or "zh"
    word_count = len(payload.get("words", []))
    segment_count = len(payload.get("segments", []))
    return ToolResult(
        text=f"external transcript written: {ctx.virtualize(output_path)}",
        data={
            "provider": "external",
            "tool": "speech_transcribe",
            "channel": "external_passthrough",
            "output_json": str(output_path),
            "source_media": str(input_path),
            "language": language,
            "word_count": word_count,
            "segment_count": segment_count,
            "elapsed_seconds": 0.0,
        },
        artifacts=[str(output_path)],
    )


def _build_transcript_payload(transcript_text: str, source_media: str, language: str) -> dict[str, Any]:
    """把全文 transcript 文本塞进标准 schema。

    若 transcript_text 已含 JSON 结构(以 ``{`` 开头且能 json.loads),则沿用
    该结构;否则当作纯文本,合成一个单段。
    """
    stripped = (transcript_text or "").strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            payload = dict(parsed)
            payload.setdefault("language", language)
            payload["source_media"] = source_media
            payload.setdefault("provider", "external")
            return payload
        if isinstance(parsed, list):
            return {
                "language": language,
                "source_media": source_media,
                "provider": "external",
                "segments": [
                    {"index": i, "start": None, "end": None, "text": str(item.get("text") if isinstance(item, dict) else item)}
                    for i, item in enumerate(parsed) if item is not None
                ],
                "words": [],
            }

    # 纯文本情形:合成一个单段。
    return {
        "language": language,
        "source_media": source_media,
        "provider": "external",
        "text": transcript_text,
        "segments": [
            {
                "index": 0,
                "start": None,
                "end": None,
                "text": transcript_text,
            }
        ],
        "words": [],
    }


def _count_segments_and_words(payload: dict[str, Any]) -> tuple[int, int]:
    """对 transcript payload 统计 segment / word 数量(给 schema 校验用)。"""
    segments = payload.get("segments") or []
    words = payload.get("words") or []
    seg_n = len(segments) if isinstance(segments, list) else 0
    word_n = len(words) if isinstance(words, list) else 0
    return seg_n, word_n


def collect_text_segments(payload: Any) -> list[str]:
    """把 payload 内的 segments 转成纯文本 list,供 ``transcript_text_for_range`` 之外的 fallback 使用。"""
    out: list[str] = []
    for seg in collect_segments(payload):
        out.append(format_segment(seg))
    return [s for s in out if s]


# 暴露 first_present 便于单测与外部调用复用(否则删掉)
__all__ = [
    "speech_transcribe",
    "collect_text_segments",
    "first_present",
]
