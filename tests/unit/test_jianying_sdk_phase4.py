"""第10期阶段4 单测:文本装饰(动画/花字/气泡)TextDecorationSpec + SDK 编译。

覆盖迁移设计文档 §9 阶段4 验收(结构级):
- 文本动画 in/out/loop 由 SDK TextSegment.add_animation 编译,顺序先出入场后循环;
- 花字走 ``add_effect(effect_id)``:双 ID 合一约束 + 验证门禁,不满足 → 降级记录;
- 气泡走 ``add_bubble(effect_id, resource_id)`` 双 ID 分传;
- 不支持的装饰资源逐条降级,不关闭全部装饰(§6.2 非破坏性);
- node_10/10b 经 apply_text_fx 把装饰计划发射进 spec(legacy 行为不变)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jianying_sdk._sdk_loader import ensure_sdk_on_path

ensure_sdk_on_path()

from jianying_sdk.compiler import compile_spec
from jianying_sdk.resource_registry import load_compat_table
from jianying_sdk.spec import (
    DraftBuildSpec,
    ResourceRef,
    SubtitleSpec,
    TextAnimationEntry,
    TextDecorationSpec,
)
from tests.unit.test_jianying_sdk_phase2 import (
    _audio_factory,
    _video_factory,
)

_ZH_STYLE = {
    "size": 5.0, "bold": True, "color": [1.0, 1.0, 1.0], "align": 1,
    "border": {"color": [0, 0, 0], "width": 40.0}, "transform_y": -0.8,
}


def _supported_effect_id(category: str) -> str:
    table = load_compat_table()
    for item in table["categories"][category]["items"]:
        if item["status"] == "supported":
            return str(item["effect_id"])
    raise AssertionError(f"交叉表 {category} 无 supported 条目")


def _spec_with_subtitle() -> DraftBuildSpec:
    spec = DraftBuildSpec(job_id="p4", language="zh")
    spec.subtitles = [
        SubtitleSpec(subtitle_id="text-zh-1", text="第一句", start_us=0,
                     end_us=2_000_000, language="zh", style=dict(_ZH_STYLE),
                     wrap_content="第一句"),
        SubtitleSpec(subtitle_id="text-zh-2", text="第二句", start_us=2_000_000,
                     end_us=4_000_000, language="zh", style=dict(_ZH_STYLE),
                     wrap_content="第二句"),
    ]
    return spec


def _compile(spec: DraftBuildSpec) -> dict:
    result = compile_spec(spec, video_material_factory=_video_factory, audio_material_factory=_audio_factory)
    return result


def _payload_texts(result) -> list[dict]:
    return result.payload["materials"]["texts"]


def _segments_by_material(result) -> dict[str, dict]:
    """按字幕显示文本索引 text 轨段(SDK material id 是 uuid,不携带业务 ID)。"""
    text_track = next(t for t in result.payload["tracks"] if t["type"] == "text")
    display_text_of = {
        t["id"]: str(json.loads(t["content"]).get("text") or "")
        for t in result.payload["materials"]["texts"]
    }
    out = {}
    for seg in text_track["segments"]:
        text = display_text_of.get(str(seg["material_id"]))
        if text:
            out[text] = seg
    return out


# ---------------------------------------------------------------------------
# 动画编译
# ---------------------------------------------------------------------------
def test_compile_text_intro_animation() -> None:
    effect_id = _supported_effect_id("text_intro")
    spec = _spec_with_subtitle()
    spec.text_decorations = [
        TextDecorationSpec(
            subtitle_id="text-zh-1",
            animations=[TextAnimationEntry(
                kind="in", effect_id=effect_id, resource_id=effect_id,
                name="测试入场", duration_us=500_000,
            )],
        ),
    ]
    result = _compile(spec)
    anim_bucket = result.payload["materials"]["material_animations"]
    assert len(anim_bucket) == 1
    entry = anim_bucket[0]["animations"][0]
    assert entry["type"] == "in"
    assert entry["duration"] == 500_000
    assert str(entry["resource_id"])
    # ref 绑定到 text-zh-1 的段
    segs = _segments_by_material(result)
    refs = segs["第一句"]["extra_material_refs"]
    assert anim_bucket[0]["id"] in refs
    assert result.summary["counts"]["text_decorations_applied"] == 1
    assert result.summary["degradations"] == []


def test_compile_animation_order_in_before_loop() -> None:
    """SDK 约束:先出入场后循环;spec 乱序给出时编译器排序兜底。"""
    intro = _supported_effect_id("text_intro")
    loop = _supported_effect_id("text_loop")
    spec = _spec_with_subtitle()
    spec.text_decorations = [
        TextDecorationSpec(
            subtitle_id="text-zh-1",
            animations=[
                TextAnimationEntry(kind="loop", effect_id=loop, resource_id=loop),
                TextAnimationEntry(kind="in", effect_id=intro, resource_id=intro),
            ],
        ),
    ]
    result = _compile(spec)
    entries = result.payload["materials"]["material_animations"][0]["animations"]
    assert [e["type"] for e in entries] == ["in", "loop"]


def test_compile_unsupported_animation_degrades() -> None:
    spec = _spec_with_subtitle()
    spec.text_decorations = [
        TextDecorationSpec(
            subtitle_id="text-zh-1",
            animations=[TextAnimationEntry(kind="in", effect_id="nonexistent-fx", resource_id="x")],
        ),
        TextDecorationSpec(
            subtitle_id="text-zh-2",
            animations=[TextAnimationEntry(
                kind="in", effect_id=_supported_effect_id("text_intro"), resource_id="y",
            )],
        ),
    ]
    result = _compile(spec)
    # 第一条降级,第二条照常应用 —— 单资源失败不关闭全部装饰
    assert len(result.payload["materials"]["material_animations"]) == 1
    degraded = [d for d in result.summary["degradations"] if d["kind"] == "text_animation"]
    assert len(degraded) == 1
    assert degraded[0]["subtitle_id"] == "text-zh-1"


# ---------------------------------------------------------------------------
# 花字编译(双 ID 合一约束 + 验证门禁)
# ---------------------------------------------------------------------------
_FLOWER_ID = "7407340226189757733"  # 互动立体(交叉表 supported,effect_id==resource_id)


def test_compile_flower_applied_when_verified() -> None:
    spec = _spec_with_subtitle()
    spec.text_decorations = [
        TextDecorationSpec(
            subtitle_id="text-zh-1",
            text_effect=ResourceRef(
                effect_id=_FLOWER_ID, resource_id=_FLOWER_ID,
                name="互动立体", verification="cache_verified",
            ),
        ),
    ]
    result = _compile(spec)
    effects = result.payload["materials"]["effects"]
    assert len(effects) == 1
    assert effects[0]["type"] == "text_effect"
    assert effects[0]["effect_id"] == _FLOWER_ID
    segs = _segments_by_material(result)
    assert effects[0]["id"] in segs["第一句"]["extra_material_refs"]


def test_compile_flower_degrades_on_id_mismatch() -> None:
    """§4.2:effect_id != resource_id 的资源 SDK 无法无损表达 → 降级不伪装。"""
    spec = _spec_with_subtitle()
    spec.text_decorations = [
        TextDecorationSpec(
            subtitle_id="text-zh-1",
            text_effect=ResourceRef(
                effect_id=_FLOWER_ID, resource_id="different-resource-id",
                verification="cache_verified",
            ),
        ),
    ]
    result = _compile(spec)
    assert result.payload["materials"]["effects"] == []
    degraded = [d for d in result.summary["degradations"] if d["kind"] == "text_flower"]
    assert degraded and degraded[0]["reason"] == "needs_sdk_extension_or_unverified"


def test_compile_flower_degrades_when_unverified() -> None:
    spec = _spec_with_subtitle()
    spec.text_decorations = [
        TextDecorationSpec(
            subtitle_id="text-zh-1",
            text_effect=ResourceRef(
                effect_id=_FLOWER_ID, resource_id=_FLOWER_ID,
                verification="structure_verified",
            ),
        ),
    ]
    result = _compile(spec)
    assert result.payload["materials"]["effects"] == []
    assert any(d["kind"] == "text_flower" for d in result.summary["degradations"])


def test_compile_flower_degrades_when_not_in_compat_table() -> None:
    """不在阶段0 交叉表 supported 集合内的花字 ID 不落草稿(快门/水墨同口径)。"""
    spec = _spec_with_subtitle()
    spec.text_decorations = [
        TextDecorationSpec(
            subtitle_id="text-zh-1",
            text_effect=ResourceRef(
                effect_id="9999999999999999999", resource_id="9999999999999999999",
                verification="cache_verified",
            ),
        ),
    ]
    result = _compile(spec)
    assert result.payload["materials"]["effects"] == []


# ---------------------------------------------------------------------------
# 气泡编译(双 ID 分传)
# ---------------------------------------------------------------------------
def test_compile_bubble_applied_with_two_ids() -> None:
    spec = _spec_with_subtitle()
    spec.text_decorations = [
        TextDecorationSpec(
            subtitle_id="text-zh-1",
            text_bubble=ResourceRef(
                effect_id="bubble-effect-id", resource_id="bubble-resource-id",
                name="测试气泡", verification="cache_verified",
            ),
        ),
    ]
    result = _compile(spec)
    effects = result.payload["materials"]["effects"]
    assert len(effects) == 1
    assert effects[0]["type"] == "text_shape"
    assert effects[0]["effect_id"] == "bubble-effect-id"
    assert effects[0]["resource_id"] == "bubble-resource-id"


def test_compile_bubble_degrades_when_unverified() -> None:
    spec = _spec_with_subtitle()
    spec.text_decorations = [
        TextDecorationSpec(
            subtitle_id="text-zh-1",
            text_bubble=ResourceRef(
                effect_id="b-e", resource_id="b-r", verification="unsupported",
            ),
        ),
    ]
    result = _compile(spec)
    assert result.payload["materials"]["effects"] == []
    assert any(d["kind"] == "text_bubble" for d in result.summary["degradations"])


def test_compile_decoration_for_missing_subtitle_degrades() -> None:
    spec = _spec_with_subtitle()
    spec.text_decorations = [
        TextDecorationSpec(subtitle_id="text-zh-gone"),
    ]
    result = _compile(spec)
    assert any(
        d["kind"] == "text_decoration" and d["reason"].startswith("subtitle_not_built")
        for d in result.summary["degradations"]
    )


# ---------------------------------------------------------------------------
# node_10 spec 发射(hermetic 缓存下动画必选中;花字默认关)
# ---------------------------------------------------------------------------
def test_apply_text_fx_emits_spec(tmp_path: Path) -> None:
    """2026-11-22 SDK 链转正:装饰对象来自 spec.subtitles;草稿不被 node_10 改写。"""
    from config import TEXT_FLOWER_ENABLED
    from nodes.node_10_inject_text_fx import apply_text_fx
    from nodes._draft_build_spec import load_build_spec, replace_spec_sections

    draft_path = tmp_path / "draft_content.json"
    draft_bytes = json.dumps({"canvas_config": {"width": 1080, "height": 1920}}, ensure_ascii=False)
    draft_path.write_text(draft_bytes, encoding="utf-8")

    # 字幕对象来源 = spec(node_08 已发射,subtitle_id 即我方口径)
    replace_spec_sections(tmp_path, subtitles=[SubtitleSpec(
        subtitle_id="text-zh-1", text="你好", start_us=0, end_us=2_000_000,
        language="zh", style=dict(_ZH_STYLE),
    )])

    assert TEXT_FLOWER_ENABLED in (True, False)
    log, summary = apply_text_fx(
        tmp_path, [], node_tag="node_10",
        done_tag="node_10_inject_text_fx_done", job_id="sess-p4",
    )
    # 动画选取(hermetic 缓存使 intro 池可用)
    assert summary["animations_selected"] == 1
    spec = load_build_spec(tmp_path)
    assert spec is not None
    assert [d.subtitle_id for d in spec.text_decorations] == ["text-zh-1"]
    deco = spec.text_decorations[0]
    assert deco.animations and deco.animations[0].kind == "in"
    assert deco.animations[0].effect_id
    if not TEXT_FLOWER_ENABLED:
        assert deco.text_effect is None
    assert any("文本装饰计划已入 spec" in tag for tag in log)
    assert any("node_10_inject_text_fx_done" in tag for tag in log)
    # 草稿不被本节点改写
    assert draft_path.read_text(encoding="utf-8") == draft_bytes


def test_apply_text_fx_degradation_keeps_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非破坏性降级(审查 P1-01):本轮零选取 → spec 既有装饰段原样保留。"""
    import nodes.node_10_inject_text_fx as node10
    from nodes.node_10_inject_text_fx import apply_text_fx
    from nodes._draft_build_spec import load_build_spec, replace_spec_sections

    (tmp_path / "draft_content.json").write_text(
        json.dumps({"canvas_config": {"width": 1080, "height": 1920}}, ensure_ascii=False),
        encoding="utf-8",
    )

    # 既有 spec 装饰段(上一轮写入)
    old = [TextDecorationSpec(
        subtitle_id="text-zh-1",
        animations=[TextAnimationEntry(kind="in", effect_id="old-fx", resource_id="old-fx")],
    )]
    replace_spec_sections(tmp_path, text_decorations=old)

    # 让动画选取降级(空池);花字经 select_text_flower 空库自然降级
    monkeypatch.setattr(node10, "_pick_intro_animation", lambda lib, job_id="": (None, {}))

    log, summary = apply_text_fx(
        tmp_path, [], node_tag="node_10",
        done_tag="node_10_inject_text_fx_done", job_id="sess-p4",
    )
    assert summary["animations_selected"] == 0
    spec = load_build_spec(tmp_path)
    assert [d.subtitle_id for d in spec.text_decorations] == ["text-zh-1"]
    assert spec.text_decorations[0].animations[0].effect_id == "old-fx"
