"""draft_content_audit 单测 — 引用口径统计(第四期阶段 0,T0.2)。

口径依据原计划《剪映草稿缺失转场/动画/文案/配音》附录 B:素材桶有条目 ≠ 剪映
能看到;只有被片段 ``material_id`` / ``extra_material_refs`` 引用的素材才渲染。
这些用例同时是交付前 QA 孤儿检测(阶段 2 复用 ``orphan_ids``)的口径锁定。
"""

from __future__ import annotations

import json
from pathlib import Path

from draft_ops.draft_content_audit import (
    collect_referenced_ids,
    inspect,
    orphan_ids,
    read_sidecar,
    sidecar_summary,
)


def _draft() -> dict:
    """一份「健康」小草稿:转场挂后一段、特效/动画/淡入淡出各挂对位置;
    贴纸无人引用(孤儿);speeds 伴随桶孤儿不计内容桶。"""
    return {
        "duration": 10_000_000,
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {
            "videos": [{"id": "v1", "path": "C:/a.mp4"}],
            "transitions": [{
                "id": "transition-1", "effect_id": "e1", "resource_id": "r1",
                "duration": 500_000, "type": "transition",
            }],
            "video_effects": [{"id": "vfx-1", "effect_id": "e2", "resource_id": "r2", "type": "video_effect"}],
            "texts": [{
                "id": "text-zh-0", "type": "text",
                "content": json.dumps({"text": "你好世界", "styles": []}, ensure_ascii=False),
            }],
            "material_animations": [{"id": "anim-1", "type": "sticker_animation", "animations": []}],
            "stickers": [{"id": "sticker-1", "resource_id": "PLACEHOLDER_STICKER_A", "type": "sticker"}],
            "audios": [{"id": "audio-1", "type": "extract_music", "path": "C:/b.wav"}],
            "audio_fades": [{
                "id": "fade-1", "fade_in_duration": 0, "fade_out_duration": 0, "type": "audio_fade",
            }],
            "speeds": [{"id": "speed-1"}],  # 伴随桶:不被引用也不算内容孤儿
        },
        "tracks": [
            {"type": "video", "id": "tv", "segments": [
                {"id": "s1", "material_id": "v1",
                 "target_timerange": {"start": 0, "duration": 5_000_000},
                 "extra_material_refs": ["vfx-1", "speed-1"]},
                {"id": "s2", "material_id": "v1",
                 "target_timerange": {"start": 5_000_000, "duration": 5_000_000},
                 "extra_material_refs": ["transition-1"]},
            ]},
            {"type": "text", "id": "tt", "segments": [
                {"id": "ts1", "material_id": "text-zh-0",
                 "target_timerange": {"start": 0, "duration": 2_000_000},
                 "extra_material_refs": ["anim-1"]},
            ]},
            {"type": "audio", "id": "ta", "segments": [
                {"id": "as1", "material_id": "audio-1",
                 "target_timerange": {"start": 0, "duration": 10_000_000},
                 "extra_material_refs": ["fade-1"]},
            ]},
        ],
    }


def test_inspect_healthy_draft_metrics() -> None:
    """健康草稿:被引用口径指标与轨道/桶计数逐项正确。"""
    rep = inspect(_draft())
    m = rep["metrics"]
    assert m["video_segments"] == 2
    assert m["audio_segments"] == 1
    assert m["text_segments"] == 1
    assert m["sticker_segments"] == 0
    assert m["effect_segments"] == 0
    assert m["transitions_referenced"] == 1
    assert m["video_effects_referenced"] == 1
    assert m["text_animations_referenced"] == 1
    assert m["stickers_referenced"] == 0
    assert m["audio_fades_referenced"] == 1
    # 唯一孤儿 = 贴纸(内容桶);speeds 属伴随桶不计
    assert m["orphan_materials"] == 1
    assert m["misplaced_references"] == 0
    assert m["dangling_references"] == 0
    # 贴纸 resource_id 是占位符
    assert m["placeholder_values"] == 1
    assert m["shape_warnings"] == 0
    # 原生文本 content(JSON 字符串)被解出纯文本
    assert rep["texts_plain"] == ["你好世界"]


def test_orphan_ids_reports_unreferenced_transitions() -> None:
    """摘掉转场引用 → orphan_ids 报 transition-1;引用在时为空(阶段 2 QA 口径)。"""
    draft = _draft()
    assert orphan_ids(draft, "transitions") == []
    draft["tracks"][0]["segments"][1]["extra_material_refs"] = []
    assert orphan_ids(draft, "transitions") == ["transition-1"]
    # QA 用的引用口径只看 material_id + extra_material_refs
    refs = collect_referenced_ids(draft)
    assert "transition-1" not in refs and "vfx-1" in refs and "v1" in refs


def test_inspect_orphan_and_misplaced_after_unbinding() -> None:
    """摘引用后:transitions_referenced=0、孤儿 +1;贴纸挂视频段 = 错位引用。"""
    draft = _draft()
    draft["tracks"][0]["segments"][1]["extra_material_refs"] = []
    draft["tracks"][0]["segments"][0]["extra_material_refs"] = ["vfx-1", "speed-1", "sticker-1"]
    m = inspect(draft)["metrics"]
    assert m["transitions_referenced"] == 0
    assert m["orphan_materials"] == 1  # 仅 transition-1;贴纸被视频段引用后算错位不算孤儿
    assert m["misplaced_references"] == 1  # sticker 被视频段引用 = 剪映不渲染
    assert m["stickers_referenced"] == 0


def test_inspect_dangling_reference_detected() -> None:
    """extra_material_refs 指向不存在的素材 → dangling_references=1。"""
    draft = _draft()
    draft["tracks"][0]["segments"][0]["extra_material_refs"].append("ghost-1")
    rep = inspect(draft)
    assert rep["metrics"]["dangling_references"] == 1
    assert rep["dangling_ids_sample"] == ["ghost-1"]


def test_inspect_shape_warning_on_missing_required_key() -> None:
    """转场条目缺 duration(原生必需键)→ shape_warnings=1 并指名条目。"""
    draft = _draft()
    del draft["materials"]["transitions"][0]["duration"]
    rep = inspect(draft)
    assert rep["metrics"]["shape_warnings"] == 1
    assert "transition-1" in rep["shape_warnings"][0] and "duration" in rep["shape_warnings"][0]


def test_keyframe_refs_do_not_count_as_segment_reference() -> None:
    """keyframe_refs 不算「片段引用素材」:不进入 collect_referenced_ids,
    但 inspect 的孤儿判定按原计划附录 B 把它计入 referenced(仅统计语义)。"""
    draft = _draft()  # sticker-1 目前无人引用(orphan_materials=1)
    draft["tracks"][0]["segments"][0]["keyframe_refs"] = ["sticker-1"]
    assert "sticker-1" not in collect_referenced_ids(draft)
    # inspect 的孤儿统计口径含 keyframe_refs(附录 B 原样):sticker-1 不再算孤儿
    assert inspect(draft)["metrics"]["orphan_materials"] == 0


def test_sidecar_reading_and_summary(tmp_path: Path) -> None:
    """sidecar 读取 + 摘要行;无 sidecar 返回 None。"""
    draft_file = tmp_path / "draft_content.json"
    draft_file.write_text(json.dumps(_draft(), ensure_ascii=False), encoding="utf-8")
    assert read_sidecar(draft_file) is None

    sidecar = {
        "dropped": {
            "dropped_text_track": {"segments": [{"text": "马六甲海上清真寺,日落时分海面金光闪烁"}]},
            "dropped_audio_segments": [{"id": "bgm-1"}, {"id": "voiceover-1"}],
            "unresolved_audio_refs": {"bgm-1": "bgm_upbeat_energetic"},
        }
    }
    (tmp_path / "autovideo_storyline_source.json").write_text(
        json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
    )
    sc = read_sidecar(draft_file)
    assert sc is not None
    lines = sidecar_summary(sc)
    assert any("dropped_text_track: 1 段文字被丢弃" in x for x in lines)
    assert any("dropped_audio_segments: 2 段音频被丢弃" in x for x in lines)
    assert any("unresolved_audio_refs" in x for x in lines)
    # 空 dropped → 明确说明
    assert any("dropped 为空" in x for x in sidecar_summary({"dropped": {}}))
