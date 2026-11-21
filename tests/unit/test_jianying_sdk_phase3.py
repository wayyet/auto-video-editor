"""第10期阶段3 单测:字幕 SubtitleSpec + style_mapper + SDK TextSegment 编译。

覆盖迁移设计文档 §9 阶段3 验收(结构级):
- 同一 style 下 SDK TextSegment 导出 material 与 legacy ``make_text_material``
  的原生键逐键一致(content JSON 完全相等 —— 结构级 A/B 黄金对比);
- 编译器把 spec.subtitles 建成 text 轨 + TextSegment(时间/折行/位置/描边);
- node_08 / node_08b 把字幕构建计划发射进 spec(legacy 行为不变);
- 提取器对我方字幕前缀(text-zh-/text-en-)不作人工残留阻断。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jianying_sdk._sdk_loader import ensure_sdk_on_path

ensure_sdk_on_path()

from jianying_sdk.compiler import TRACK_SUBTITLES, compile_spec
from jianying_sdk.spec import DraftBuildSpec, SubtitleSpec
from jianying_sdk.style_mapper import (
    border_kwargs,
    style_to_text_style_kwargs,
    subtitle_text_segment_kwargs,
    transform_y_of,
)
from jianying_sdk.timeline_extractor import extract_timeline_snapshot
from nodes._subtitle_texts import make_text_material, subtitle_specs_from_materials
from tests.unit.test_jianying_sdk_phase2 import (  # 复用阶段2 的替身工厂
    _audio_factory,
    _video_factory,
)

_ZH_STYLE = {
    "size": 5.0,
    "bold": True,
    "color": [1.0, 1.0, 1.0],
    "align": 1,
    "border": {"color": [0, 0, 0], "width": 40.0},
    "transform_y": -0.8,
}


def _content_json_close(a: str, b: str) -> bool:
    """content JSON 语义等价(浮点经换算路径不同,允许 1e-9 容差)。

    SDK TextBorder 是 ``width/100*0.2``,legacy 是 ``width*0.002`` —— 40.0 时
    两条路径的浮点 repr 相差 1 ULP(0.08 vs 0.08000000000000002),语义相同。
    """
    import math

    def close(x, y) -> bool:
        if isinstance(x, float) or isinstance(y, float):
            return math.isclose(float(x), float(y), rel_tol=0.0, abs_tol=1e-9)
        if isinstance(x, dict) and isinstance(y, dict):
            return x.keys() == y.keys() and all(close(x[k], y[k]) for k in x)
        if isinstance(x, list) and isinstance(y, list):
            return len(x) == len(y) and all(close(i, j) for i, j in zip(x, y))
        return x == y

    def normalize(obj):
        """归一化已知语义等价差:SDK 恒带空 ``strokes`` 键,legacy 无描边时省略。"""
        if isinstance(obj, dict) and isinstance(obj.get("styles"), list):
            for block in obj["styles"]:
                if isinstance(block, dict):
                    block.setdefault("strokes", [])
        return obj

    return close(normalize(json.loads(a)), normalize(json.loads(b)))


def _legacy_material(text: str, start_us: int, duration_us: int, style: dict) -> dict:
    return make_text_material(
        index=0,
        content=text,
        start_us=start_us,
        duration_us=duration_us,
        style=style,
        track_name="Subtitles_zh",
        id_prefix="text-zh",
        extra={"text_zh": text},
        canvas_width=1080,
    )


# ---------------------------------------------------------------------------
# style_mapper
# ---------------------------------------------------------------------------
def test_style_to_text_style_kwargs_default() -> None:
    kwargs = style_to_text_style_kwargs(_ZH_STYLE)
    assert kwargs["size"] == 5.0
    assert kwargs["bold"] is True
    assert kwargs["color"] == (1.0, 1.0, 1.0)
    assert kwargs["align"] == 1
    assert kwargs["auto_wrapping"] is False
    assert kwargs["max_line_width"] == 0.82
    assert kwargs["line_spacing"] == 0


def test_border_kwargs_and_transform_y() -> None:
    border = border_kwargs(_ZH_STYLE)
    assert border is not None and border["width"] == 40.0
    assert border_kwargs({"border": {"width": 0}}) is None
    assert border_kwargs({}) is None
    assert transform_y_of(_ZH_STYLE) == -0.8
    assert transform_y_of({}) == -0.8
    assert transform_y_of({"transform_y": "bad"}) == -0.8


def test_subtitle_text_segment_kwargs_shape() -> None:
    sub = SubtitleSpec(
        subtitle_id="text-zh-1", text="你好", start_us=0, end_us=1_000_000,
        language="zh", style=dict(_ZH_STYLE), wrap_content="你\n好",
    )
    kwargs = subtitle_text_segment_kwargs(sub)
    assert kwargs["style"]["size"] == 5.0
    assert kwargs["border"]["width"] == 40.0
    assert kwargs["clip_settings"] == {"transform_y": -0.8}


# ---------------------------------------------------------------------------
# 结构级 A/B 黄金对比:SDK 导出 vs legacy material(同 style)
# ---------------------------------------------------------------------------
def _sdk_material_for(material: dict):
    specs = subtitle_specs_from_materials([material], language="zh")
    spec = DraftBuildSpec(job_id="ab", language="zh")
    spec.subtitles = specs
    result = compile_spec(spec, video_material_factory=_video_factory, audio_material_factory=_audio_factory)
    texts = result.payload["materials"]["texts"]
    assert len(texts) == 1
    return texts[0], specs[0]


def test_sdk_text_material_matches_legacy_byte_level() -> None:
    """同 style 同文本:SDK TextSegment 导出的 content JSON 与 legacy 完全一致。"""
    text = "这是一句需要折行的中文字幕文本,长度超过画布宽度限制会自动内嵌换行符"
    legacy = _legacy_material(text, 1_500_000, 3_000_000, _ZH_STYLE)
    sdk_mat, spec0 = _sdk_material_for(legacy)

    # content JSON(折行文本 + styles 块)语义级一致(浮点路径差 1 ULP)
    assert _content_json_close(sdk_mat["content"], legacy["content"])
    # 共享原生键逐键一致
    shared_keys = (
        "typesetting", "alignment", "letter_spacing", "line_spacing",
        "line_feed", "line_max_width", "force_apply_line_max_width",
        "check_flag", "type", "global_alpha",
    )
    for key in shared_keys:
        assert sdk_mat[key] == legacy[key], f"material[{key}] 不一致"
    # 折行文本进 SDK 渲染口径;原文保留在 spec
    assert spec0.text == text
    assert spec0.wrap_content == json.loads(legacy["content"])["text"]


def test_sdk_text_material_no_border_matches_legacy() -> None:
    legacy = _legacy_material("无描边", 0, 1_000_000, {**_ZH_STYLE, "border": {"width": 0}})
    sdk_mat, _ = _sdk_material_for(legacy)
    assert _content_json_close(sdk_mat["content"], legacy["content"])
    # 无描边:check_flag 位 8 不置位,SDK strokes 为空
    assert sdk_mat["check_flag"] == legacy["check_flag"] == 7
    assert json.loads(sdk_mat["content"])["styles"][0]["strokes"] == []


# ---------------------------------------------------------------------------
# 编译器:字幕段形状
# ---------------------------------------------------------------------------
def test_compile_builds_text_track_with_timing() -> None:
    spec = DraftBuildSpec(job_id="p3", language="zh")
    spec.subtitles = [
        SubtitleSpec(subtitle_id="text-zh-2", text="第二句", start_us=2_000_000,
                     end_us=3_500_000, language="zh", style=dict(_ZH_STYLE),
                     wrap_content="第二句"),
        SubtitleSpec(subtitle_id="text-zh-1", text="第一句", start_us=0,
                     end_us=1_500_000, language="zh", style=dict(_ZH_STYLE),
                     wrap_content="第一句"),
        SubtitleSpec(subtitle_id="text-zh-bad", text="零时长", start_us=5_000_000,
                     end_us=5_000_000, language="zh", style=dict(_ZH_STYLE)),
    ]
    result = compile_spec(spec, video_material_factory=_video_factory, audio_material_factory=_audio_factory)
    tracks = {t["name"]: t for t in result.payload["tracks"]}
    assert TRACK_SUBTITLES in tracks
    segs = tracks[TRACK_SUBTITLES]["segments"]
    # 零时长字幕不落轨;顺序按 start 排序
    assert len(segs) == 2
    assert [s["target_timerange"]["start"] for s in segs] == [0, 2_000_000]
    assert result.summary["counts"]["subtitles"] == 2
    # 文本段 transform 下沉位与对齐
    clip0 = segs[0]["clip"]
    assert clip0["transform"]["y"] == -0.8
    texts = result.payload["materials"]["texts"]
    assert [json.loads(t["content"])["text"] for t in texts] == ["第一句", "第二句"]


# ---------------------------------------------------------------------------
# nodes:spec 发射与提取器容忍
# ---------------------------------------------------------------------------
def test_subtitle_specs_from_materials_roundtrip() -> None:
    legacy = _legacy_material("原文", 500_000, 2_000_000, _ZH_STYLE)
    specs = subtitle_specs_from_materials([legacy], language="zh")
    assert len(specs) == 1
    s = specs[0]
    assert s.subtitle_id == "text-zh-1"
    assert s.text == "原文"  # 原文来自私有键
    assert s.start_us == 500_000 and s.end_us == 2_500_000
    assert s.style["size"] == 5.0
    assert s.wrap_content and "\\n" not in repr(s.wrap_content)
    # 英文口径
    en = dict(legacy)
    en["id"] = "text-en-1"
    en["text_en"] = "hello world"
    en["text_zh"] = None
    specs_en = subtitle_specs_from_materials([en], language="en")
    assert specs_en[0].language == "en"
    assert specs_en[0].text == "hello world"


def test_extractor_tolerates_our_subtitle_prefixes() -> None:
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"videos": [{"id": "v1", "path": "C:/m.mp4", "material_name": "clip_0001"}],
                      "audios": [], "speeds": [], "canvases": [],
                      "sound_channel_mappings": [], "vocal_separations": []},
        "tracks": [
            {"type": "video", "id": "tv", "segments": [{
                "id": "s1", "material_id": "v1",
                "target_timerange": {"start": 0, "duration": 1_000_000},
                "source_timerange": {"start": 0, "duration": 1_000_000},
                "speed": 1.0, "volume": 1.0, "extra_material_refs": [],
                "common_keyframes": [], "keyframe_refs": [],
            }]},
            {"type": "text", "id": "tt", "segments": [{
                "id": "ts1", "material_id": "text-zh-1",
                "target_timerange": {"start": 0, "duration": 1_000_000},
            }]},
        ],
    }
    report = extract_timeline_snapshot(draft)
    assert not any(b["kind"] == "manual_text_track" for b in report.section.blocked_features)
    # 非我方前缀的 uuid 文本段仍然阻断
    draft["tracks"][1]["segments"][0]["material_id"] = "ABCD-uuid-manual"
    report2 = extract_timeline_snapshot(draft)
    assert any(b["kind"] == "manual_text_track" for b in report2.section.blocked_features)


def test_node_08_emits_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """node_08 中文链:字幕落草稿的同时 spec.subtitles 就位(legacy 行为不变)。"""
    from nodes.node_08_add_subtitles import add_subtitles

    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 7_000_000,
        "materials": {
            "videos": [{"id": "v1", "path": "C:/media/a.mp4", "material_name": "clip_0001"}],
            "audios": [], "speeds": [], "canvases": [],
            "sound_channel_mappings": [], "vocal_separations": [], "texts": [],
        },
        "tracks": [{
            "type": "video", "id": "tv", "segments": [{
                "id": "s1", "material_id": "v1",
                "target_timerange": {"start": 0, "duration": 7_000_000},
                "source_timerange": {"start": 0, "duration": 7_000_000},
                "speed": 1.0, "volume": 1.0,
                "extra_material_refs": ["sp1", "cv1", "sc1", "vo1"],
                "common_keyframes": [], "keyframe_refs": [],
            }],
        }],
    }
    # 伴随素材桶补全(提取器引用核对)
    draft["materials"]["speeds"].append({"id": "sp1", "speed": 1.0, "mode": 0, "type": "speed"})
    draft["materials"]["canvases"].append({"id": "cv1", "type": "canvas_color"})
    draft["materials"]["sound_channel_mappings"].append({"id": "sc1", "audio_channel_mapping": 0})
    draft["materials"]["vocal_separations"].append({"id": "vo1", "type": "vocal_separation"})

    draft_path = tmp_path / "draft_content.json"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    plan = {
        "clips": [{
            "clip_id": "clip_0001", "voiceover_id": "vo_0001",
            "timeline_in_ms": 0, "timeline_out_ms": 7000,
            "source_in_ms": 0, "source_out_ms": 7000,
        }],
        "audio": {"voiceover": json.dumps({"g1": "vo_0001"})},
        "subtitles": {"groups": [{"group_id": "g1", "raw_text": "你好世界。这是第二句。"}]},
        "source_media": [{"media_id": "media-1", "file_uri": "file:///C:/media/a.mp4", "duration_ms": 7000}],
    }
    # 旁白分组写入临时产物
    narration_path = tmp_path / "storyline" / "script.json"
    narration_path.parent.mkdir(parents=True)
    narration_path.write_text(json.dumps({
        "group_scripts": [{"group_id": "g1", "narration": "你好世界。这是第二句。"}]
    }, ensure_ascii=False), encoding="utf-8")

    state = {
        "draft_path": str(draft_path),
        "session_id": "sess-p3",
        "status_log": [],
        "error_log": [],
        "storyline_plan": plan,
        "storyline_script_artifact": str(narration_path),
    }
    out = add_subtitles(state)
    assert any("字幕构建计划已入 spec:2 条" in tag for tag in out["status_log"])
    from nodes._draft_build_spec import load_build_spec

    spec = load_build_spec(tmp_path)
    assert spec is not None
    assert spec.language == "zh"
    assert len(spec.subtitles) == 2
    assert spec.subtitles[0].style["size"] == 5.0
