"""文本链原生升级单测(2026-11-07):make_text_material 双形态 + text 轨道同步 + SRT 双形态读取。

关卡③ 的人工编辑前提:字幕必须铺在原生 text 轨道上(剪映时间轴可见可改);
人工保存后私有键被剪映丢弃,读取侧(node_16)必须能从 content JSON + 轨道段
还原字幕 —— 本文件同时钉住写出与读取两侧契约。
"""

from __future__ import annotations

import json
from pathlib import Path

from jy_common.native_text import text_display_text
from nodes._subtitle_texts import (
    build_text_track,
    make_text_material,
    make_text_segment,
    sync_text_track,
)
from nodes.node_16_translate_subtitles import _read_segments_from_draft

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

    def test_display_text_dual(self) -> None:
        m = _two_materials()[0]
        assert text_display_text(m) == "txt0", "原生 content JSON 解析"
        assert text_display_text({"content": "Plain"}) == "Plain", "纯文本形态兜底"


# ---------------------------------------------------------------------------
# 写出侧:text 轨道段 / 整轨同步
# ---------------------------------------------------------------------------
class TestTextTrack:
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

    def test_sync_replaces_and_idempotent(self) -> None:
        texts = _two_materials()
        draft: dict = {
            "tracks": [
                {"type": "video", "id": "v1", "segments": []},
                {"type": "text", "id": "old-text-track", "segments": [{"old": True}]},
            ]
        }
        sync_text_track(draft, texts)
        text_tracks = [t for t in draft["tracks"] if t.get("type") == "text"]
        assert len(text_tracks) == 1, "旧 text 轨被整轨替换"
        assert len(text_tracks[0]["segments"]) == 2
        assert text_tracks[0]["segments"][0]["material_id"] == "text-1"
        assert [t["id"] for t in draft["tracks"] if t.get("type") == "video"] == ["v1"]
        sync_text_track(draft, texts)
        assert len([t for t in draft["tracks"] if t.get("type") == "text"]) == 1, "重放幂等"

    def test_sync_skips_zero_duration(self) -> None:
        texts = _two_materials()
        texts[1]["target_timerange"] = {"start": 0, "duration": 0}
        track = build_text_track(texts)
        assert len(track["segments"]) == 1, "零时长段不铺轨"


# ---------------------------------------------------------------------------
# 读取侧:SRT 重建双形态
# ---------------------------------------------------------------------------
class TestReadSegmentsDualShape:
    def test_native_shape_after_jianying_save(self, tmp_path: Path) -> None:
        """剪映保存后(私有键被丢):计时取轨道段、文本取 content JSON。"""
        texts = _two_materials(id_prefix="text-en", extra={"text_en": "EN"})
        native_texts = [
            {k: v for k, v in t.items()
             if k not in ("target_timerange", "style", "track_name", "text_en")}
            for t in texts
        ]
        draft = {"materials": {"texts": native_texts}, "tracks": [build_text_track(texts)]}
        draft_dir = tmp_path / "d1"
        draft_dir.mkdir()
        (draft_dir / "draft_content.json").write_text(
            json.dumps(draft, ensure_ascii=False), encoding="utf-8"
        )
        segs = _read_segments_from_draft(draft_dir)
        assert len(segs) == 2
        assert [s["text_en"] for s in segs] == ["txt0", "txt1"]
        assert segs[0]["start_ms"] == 0 and segs[0]["end_ms"] == 500
        assert segs[1]["start_ms"] == 1000 and segs[1]["end_ms"] == 1500

    def test_native_shape_sorts_by_start(self, tmp_path: Path) -> None:
        texts = _two_materials(id_prefix="text-en")
        track = build_text_track(list(reversed(texts)))
        draft = {"materials": {"texts": texts}, "tracks": [track]}
        draft_dir = tmp_path / "d2"
        draft_dir.mkdir()
        (draft_dir / "draft_content.json").write_text(
            json.dumps(draft, ensure_ascii=False), encoding="utf-8"
        )
        segs = _read_segments_from_draft(draft_dir)
        assert [s["start_ms"] for s in segs] == [0, 1000], "按 start 排序"
        assert [s["text_en"] for s in segs] == ["txt0", "txt1"], "index 随排序重建"
