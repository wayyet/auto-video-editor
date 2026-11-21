"""文本链单测(2026-11-07 原生升级;2026-11-22 SDK 链转正裁剪)。

保留:make_text_material 双形态构造(仍是 spec 发射与开场钩子的构造源)、
make_text_segment 形态(node_09b 标题轨)、canvas 宽度助手;
删除:build_text_track/sync_text_track 轨道测试与 _read_segments_from_draft
读取测试(随六段 legacy 原生字幕写入退役),读取侧改测 marker 译文路径。
"""

from __future__ import annotations

import json
from pathlib import Path

from jy_common.native_text import text_display_text
from nodes._subtitle_texts import (
    make_text_material,
    make_text_segment,
)

_DEFAULT_STYLE: dict = {
    "size": 5.0,
    "bold": True,
    "color": [1.0, 1.0, 1.0],
    "align": 1,
    "border": {"color": [0, 0, 0], "width": 40.0},
    "transform_y": -0.8,
}


def _two_materials(**kwargs) -> list[dict]:
    prefix = kwargs.pop("id_prefix", "text")
    return [
        make_text_material(
            index=i,
            content=f"txt{i}",
            start_us=i * 1_000_000,
            duration_us=500_000,
            style=_DEFAULT_STYLE,
            track_name="Subtitles_en",
            id_prefix=prefix,
            **kwargs,
        )
        for i in range(2)
    ]


# ---------------------------------------------------------------------------
# 写出侧:material 双形态
# ---------------------------------------------------------------------------
class TestMakeTextMaterial:
    def test_native_required_keys(self) -> None:
        m = _two_materials()[0]
        for key in (
            "id", "content", "typesetting", "alignment", "letter_spacing",
            "line_spacing", "line_feed", "line_max_width",
            "force_apply_line_max_width", "check_flag", "type", "global_alpha",
        ):
            assert key in m, f"缺原生必填键 {key}"
        assert m["type"] == "text"
        assert m["check_flag"] == 15, "基值 7 + 描边 |8"

    def test_content_json_structure(self) -> None:
        m = _two_materials()[0]
        obj = json.loads(m["content"])
        assert obj["text"] == "txt0"
        st = obj["styles"][0]
        assert st["size"] == 5.0 and st["bold"] is True
        assert st["fill"]["content"]["render_type"] == "solid"
        assert st["fill"]["content"]["solid"]["color"] == [1.0, 1.0, 1.0]
        assert st["range"] == [0, 4]
        assert st["strokes"][0]["width"] == 40.0 * 0.002, "pyJianYingDraft 描边口径"

    def test_private_keys_kept(self) -> None:
        m = _two_materials(extra={"text_en": "EN"})[0]
        assert m["target_timerange"] == {"start": 0, "duration": 500_000}
        assert m["style"]["transform_y"] == -0.8
        assert m["track_name"] == "Subtitles_en"
        assert m["text_en"] == "EN"

    def test_style_deepcopied(self) -> None:
        mats = _two_materials()
        mats[0]["style"]["size"] = 99.0
        assert mats[1]["style"]["size"] == 5.0, "每条字幕样式互不污染"

    def test_long_content_wrapped_with_range_synced(self) -> None:
        """2026-11-18 折行:超宽文本 → content 内嵌 \n;range 覆盖折行后全文。"""
        m = make_text_material(
            index=0,
            content="字" * 40,
            start_us=0,
            duration_us=1_000_000,
            style=_DEFAULT_STYLE,
            track_name="Subtitles",
        )
        obj = json.loads(m["content"])
        assert "\n" in obj["text"], "size 5.0 容量 26 单位,40 全角字必须折行"
        assert obj["text"].replace("\n", "") == "字" * 40, "折行不丢字符"
        assert obj["styles"][0]["range"] == [0, len(obj["text"])], "range 覆盖含 \n 全文"

    def test_wrapped_display_text_roundtrip(self) -> None:
        """折行后的显示文本经 text_display_text 回读原样(读取侧契约不破)。"""
        m = make_text_material(
            index=0,
            content="字" * 40,
            start_us=0,
            duration_us=1_000_000,
            style=_DEFAULT_STYLE,
            track_name="Subtitles",
        )
        assert text_display_text(m) == json.loads(m["content"])["text"]

    def test_canvas_width_param_overrides_config(self) -> None:
        """P2-03:canvas_width 参数生效 —— 窄画布预算更小,折行更多。"""
        # 20 全角字:size 5.0 下 1080 预算 26 → 不折;720 预算 17 → 折 2 行
        m1080 = make_text_material(
            index=0, content="字" * 20, start_us=0, duration_us=1_000_000,
            style=_DEFAULT_STYLE, track_name="Subtitles", canvas_width=1080,
        )
        m720 = make_text_material(
            index=0, content="字" * 20, start_us=0, duration_us=1_000_000,
            style=_DEFAULT_STYLE, track_name="Subtitles", canvas_width=720,
        )
        assert "\n" not in json.loads(m1080["content"])["text"], "1080 口径 20 字不折"
        assert "\n" in json.loads(m720["content"])["text"], "720 口径 20 字必须折行"

    def test_canvas_width_none_falls_back_to_config(self) -> None:
        """canvas_width=None/非法 → 回落 config.CANVAS_WIDTH(1080,兼容旧行为)。"""
        m = make_text_material(
            index=0, content="字" * 20, start_us=0, duration_us=1_000_000,
            style=_DEFAULT_STYLE, track_name="Subtitles",
        )
        assert "\n" not in json.loads(m["content"])["text"], "config 默认 1080 下 20 字不折"

    def test_canvas_width_from_draft_helper(self) -> None:
        """canvas_width_from_draft:草稿画布提取 + 非法/缺失回落 None。"""
        from nodes._subtitle_texts import canvas_width_from_draft

        assert canvas_width_from_draft({"canvas_config": {"width": 720}}) == 720
        assert canvas_width_from_draft({"canvas_config": {"width": "720"}}) == 720
        assert canvas_width_from_draft({"canvas_config": {"width": 0}}) is None
        assert canvas_width_from_draft({"canvas_config": {}}) is None
        assert canvas_width_from_draft({}) is None
        assert canvas_width_from_draft(None) is None
        assert canvas_width_from_draft({"canvas_config": {"width": "abc"}}) is None

    def test_display_text_dual(self) -> None:
        m = _two_materials()[0]
        assert text_display_text(m) == "txt0", "原生 content JSON 解析"
        assert text_display_text({"content": "Plain"}) == "Plain", "纯文本形态兜底"


# ---------------------------------------------------------------------------
# text 轨道段形态(node_09b 开场钩子标题轨仍用 make_text_segment)
# ---------------------------------------------------------------------------
class TestTextSegment:
    def test_segment_shape(self) -> None:
        seg = make_text_segment(
            index=0, material_id="text-1",
            start_us=500_000, duration_us=1_500_000, style=_DEFAULT_STYLE,
        )
        assert len(seg) == 34, "与真实样本 segment 键数一致"
        assert seg["material_id"] == "text-1"
        assert seg["render_index"] == 15000
        assert seg["clip"]["transform"]["y"] == -0.8
        assert seg["target_timerange"] == {"start": 500_000, "duration": 1_500_000}
        assert seg["hdr_settings"] is None and seg["uniform_scale"] is None
        assert seg["source_timerange"] is None


# ---------------------------------------------------------------------------
# 读取侧:marker 译文(2026-11-22 SDK 链转正)
# ---------------------------------------------------------------------------
class TestReadSegmentsFromMarker:
    def test_reads_segments_from_marker_file(self, tmp_path: Path) -> None:
        from nodes.node_16_translate_subtitles import _read_segments_from_marker

        marker = {
            "schema_version": 3,
            "target_language": "en",
            "source_fingerprint": "fp",
            "translator_id": "t",
            "prompt_version": "p",
            "reference_sha256": "r",
            "reference_sources": [],
            "segments": [
                {"index": 0, "start_ms": 0, "end_ms": 500, "text_en": "hello"},
                {"index": 1, "start_ms": 1000, "end_ms": 1500, "text_en": "world"},
            ],
        }
        (tmp_path / "subtitle_en.json").write_text(
            json.dumps(marker, ensure_ascii=False), encoding="utf-8"
        )
        segs = _read_segments_from_marker(tmp_path)
        assert [s["text_en"] for s in segs] == ["hello", "world"]

    def test_missing_or_corrupt_marker_returns_empty(self, tmp_path: Path) -> None:
        from nodes.node_16_translate_subtitles import _read_segments_from_marker

        assert _read_segments_from_marker(tmp_path) == []
        (tmp_path / "subtitle_en.json").write_text("not-json{", encoding="utf-8")
        assert _read_segments_from_marker(tmp_path) == []
