"""``transcript.py``(源自 video-agent-kit 0.4.3;B1 ASR 移除后仅保留仍被复用的纯函数)。

提供转写 JSON 解析与段收集等纯函数。当前唯一生产消费者是
``video_edit_capabilities/subtitle_build.py``(加载转写 JSON、收集段、
读取字段值、时间戳数值化),供字幕构建流程解析转写文件。

(B1 ASR 移除,2026-11-08)删除已无生产消费者的 ASR 专用解析链:
``read_transcript_text`` / ``transcript_data_to_text`` / ``format_segment`` /
``fmt_time`` / ``transcript_text_for_range`` / ``segment_overlaps_range``
(历史实现见 git 历史)。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


def load_transcript_data(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows = []
        for line in text.splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows
    return json.loads(text)


def first_present(seg: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in seg and seg[key] is not None:
            return seg[key]
    return None


def collect_segments(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("segments", "utterances", "results", "words"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
