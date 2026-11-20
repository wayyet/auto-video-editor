"""node_16_translate_subtitles 纯函数单测 — SRT 序列化、草稿重读。

历史:本文件曾覆盖 layout 校验(``validate_layout``)与关卡③ payload 构造
(``_build_interrupt_payload``);2026-11-15 关卡③整体删除,两者随实现一并移除。

2026-11-12 noop 治理阶段4:原文件中的 shim 级用例(直接调用未注册进图的
``node_16_translate_subtitles()``)已随 shim 一起删除;其覆盖的节点行为由
``tests/unit/test_node_16a_translate_and_check.py`` 承接。
"""

from __future__ import annotations

import json
from pathlib import Path

from nodes.node_16_translate_subtitles import (
    _compute_source_fingerprint,
    _marker_exists,
    _ms_to_srt_time,
    _read_segments_from_draft,
    _segments_to_srt,
)


# ---------------------------------------------------------------------------
# 纯函数测试
# ---------------------------------------------------------------------------
def test_ms_to_srt_time_basic() -> None:
    assert _ms_to_srt_time(0) == "00:00:00,000"
    assert _ms_to_srt_time(1_500) == "00:00:01,500"
    assert _ms_to_srt_time(65_123) == "00:01:05,123"
    assert _ms_to_srt_time(3_661_500) == "01:01:01,500"


def test_ms_to_srt_time_negative_clamps() -> None:
    """负值应被钳到 0(防御性)。"""
    assert _ms_to_srt_time(-100) == "00:00:00,000"


def test_segments_to_srt_format() -> None:
    """_segments_to_srt 应输出标准 SRT 格式(编号 + 时间码 + 文本 + 空行)。"""
    segs = [
        {"index": 0, "start_ms": 0, "end_ms": 1_500, "text_en": "Hello"},
        {"index": 1, "start_ms": 1_500, "end_ms": 3_000, "text_en": "World"},
    ]
    srt = _segments_to_srt(segs)
    lines = srt.splitlines()
    # 段 1:1=编号,2=时间码,3=文本,4=空行;段 2:5/6/7/8
    assert lines[0] == "1"
    assert "00:00:00,000 --> 00:00:01,500" in lines[1]
    assert lines[2] == "Hello"
    assert lines[3] == ""
    assert lines[4] == "2"
    assert "00:00:01,500 --> 00:00:03,000" in lines[5]
    assert lines[6] == "World"


def test_read_segments_from_draft(tmp_path: Path) -> None:
    """从 draft_content.json 的 materials.texts 还原 SubtitleSegment。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    draft = {
        "canvas_config": {},
        "materials": {
            "texts": [
                {"content": "Hello", "target_timerange": {"start": 1_000_000, "duration": 2_000_000}},
                {"content": "World", "target_timerange": {"start": 3_000_000, "duration": 2_000_000}},
            ]
        },
    }
    (draft_dir / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    segs = _read_segments_from_draft(draft_dir)
    assert len(segs) == 2
    # start=1_000_000 us → 1000 ms; duration=2_000_000 us → 2000 ms
    assert segs[0]["start_ms"] == 1000
    assert segs[0]["end_ms"] == 3000
    assert segs[0]["text_en"] == "Hello"


def test_wrapped_native_content_srt_roundtrip(tmp_path: Path) -> None:
    """P2-05(20261118 审查):折行后的原生 content → native 回读 → SRT。

    回填脚本在 content.text 内嵌 \\n 后,node_16 读取侧经 text_display_text
    取到的是**折行后**文本 —— 本用例钉住该行为的预期(§4.2 P2-08):文字
    无丢失、时间戳不变、SRT cue 文本块内换行按设计保留(SRT 支持多行 cue)。
    """
    wrapped = "line one\nline two"
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {
            "texts": [
                {
                    "id": "text-en-1",
                    "content": json.dumps(
                        {"text": wrapped, "styles": [{"size": 5.0, "range": [0, len(wrapped)]}]},
                        ensure_ascii=False,
                    ),
                }
            ]
        },
        "tracks": [
            {
                "type": "text",
                "segments": [
                    {
                        "material_id": "text-en-1",
                        "target_timerange": {"start": 0, "duration": 2_000_000},
                    }
                ],
            }
        ],
    }
    (draft_dir / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False), encoding="utf-8"
    )

    segs = _read_segments_from_draft(draft_dir)
    assert len(segs) == 1
    assert segs[0]["start_ms"] == 0 and segs[0]["end_ms"] == 2000, "时间取轨道段,不因折行改变"
    assert segs[0]["text_en"] == wrapped, "折行文本原样回读,字符无丢失"

    srt = _segments_to_srt(segs)
    assert "00:00:00,000 --> 00:00:02,000" in srt
    assert "line one\nline two" in srt, "SRT cue 内多行按设计保留"


# ---------------------------------------------------------------------------
# 2026-11-20 第九期:指纹/ marker v3 的参考哈希维度(审查修订版 §10.4)
# ---------------------------------------------------------------------------
def test_fingerprint_reference_hash_is_a_fingerprint_dimension() -> None:
    segs = [{"index": 0, "start_ms": 0, "end_ms": 1, "text_zh": "你好"}]
    empty = _compute_source_fingerprint(segs, translator_id="t", reference_sha256="")
    other = _compute_source_fingerprint(segs, translator_id="t", reference_sha256="aa")
    again = _compute_source_fingerprint(segs, translator_id="t", reference_sha256="")
    assert empty != other, "参考哈希必须参与指纹"
    assert empty == again, "同参考指纹稳定"


def test_marker_exists_rejects_v2_marker(tmp_path) -> None:
    """v2 marker(缺 reference_sha256 字段)→ 未命中重翻(一次性升级代价)。"""
    draft_dir = tmp_path
    segs = [{"index": 0, "start_ms": 0, "end_ms": 1, "text_en": "hello"}]
    fingerprint = _compute_source_fingerprint(
        [{"index": 0, "start_ms": 0, "end_ms": 1, "text_zh": "你好"}],
        translator_id="t",
        reference_sha256="",
    )
    (draft_dir / "subtitle_en.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "target_language": "en",
                "source_fingerprint": fingerprint,
                "translator_id": "t",
                "prompt_version": "subtitle-translate-v3",
                "segments": segs,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert not _marker_exists(
        draft_dir, 1, fingerprint=fingerprint, translator_id="t", reference_sha256=""
    )
