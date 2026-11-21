"""第10期阶段1 单测:jianying_sdk 骨架 + 安全发布桥 + 指纹 + 资源注册表。

覆盖迁移设计文档 §9 阶段1 验收:
- SDK 输出 JSON 可解析、受控轨道形状正确;
- 指纹一致跳过编译、内容敏感;
- 双写内容一致、失败回退、未知文件(draft_meta_info.json)不被误删;
- 资源注册表 ID 级解析(快门/水墨 unsupported → None)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jianying_sdk import (
    DraftBuildSpec,
    FingerprintUnchangedError,
    SDK_VERSION,
    compile_spec,
    compute_build_fingerprint,
    needs_compile,
    publish_compiled_draft,
    validate_sdk_payload,
)
from jianying_sdk._sdk_loader import sdk
from jianying_sdk import resource_registry
from jianying_sdk.compiler import (
    TRACK_VIDEO_MAIN,
)


# ---------------------------------------------------------------------------
# SDK loader
# ---------------------------------------------------------------------------
def test_sdk_import_and_enum_sizes() -> None:
    jy = sdk()
    assert SDK_VERSION == "0.3.1"
    # 锁定 commit 的枚举规模(阶段0 实测;SDK 换版须同步 PROVENANCE 与交叉表)
    assert len(list(jy.TransitionType)) == 453
    assert len(list(jy.VideoSceneEffectType)) == 1097
    assert len(list(jy.IntroType)) == 155


# ---------------------------------------------------------------------------
# compiler:无效果最小草稿
# ---------------------------------------------------------------------------
def _minimal_spec() -> DraftBuildSpec:
    return DraftBuildSpec(job_id="phase1-smoke", language="zh")


def test_compile_minimal_spec_shape() -> None:
    spec = _minimal_spec()
    result = compile_spec(spec)
    payload = result.payload
    assert result.duration_us == 0
    assert result.fingerprint == compute_build_fingerprint(spec)

    canvas = payload["canvas_config"]
    assert (canvas["width"], canvas["height"]) == (1080, 1920)
    assert payload["fps"] == 30

    # 阶段2 起轨道按需创建:无字幕/贴纸计划的最小草稿只有主视频轨
    tracks = payload["tracks"]
    assert [t["name"] for t in tracks] == [TRACK_VIDEO_MAIN]
    assert [t["type"] for t in tracks] == ["video"]
    for track in tracks:
        assert track["segments"] == []
    # SDK 模板素材桶存在且为空(无孤儿素材)
    materials = payload["materials"]
    assert isinstance(materials, dict)
    assert result.summary["counts"]["materials_total"] == 0
    # 载荷可再次 JSON 往返(dumps 产物即发布产物)
    round_trip = json.loads(json.dumps(payload, ensure_ascii=False))
    assert round_trip == payload


def test_compile_rejects_populated_spec_phase_boundary() -> None:
    """阶段边界守卫:装饰/效果编译未交付前,非空 spec 不产出半成品草稿。

    字幕编译已在阶段3 交付;此处用尚未交付的 text_decorations 触发守卫。
    """
    from jianying_sdk.spec import SubtitleSpec, TextDecorationSpec

    spec = _minimal_spec()
    spec.text_decorations.append(TextDecorationSpec(subtitle_id="sub-1"))
    with pytest.raises(NotImplementedError):
        compile_spec(spec)
    # 字幕已可编译(阶段3),不再是守卫触发条件
    spec2 = _minimal_spec()
    spec2.subtitles.append(
        SubtitleSpec(subtitle_id="sub-1", text="你好", start_us=0, end_us=1_000_000, language="zh")
    )
    result = compile_spec(spec2)
    assert result.summary["counts"]["subtitles"] == 1


def test_compile_fingerprint_skip_and_force() -> None:
    spec = _minimal_spec()
    spec.compile.last_compiled_fingerprint = compute_build_fingerprint(spec)
    assert needs_compile(spec) is False
    with pytest.raises(FingerprintUnchangedError):
        compile_spec(spec)
    # force 允许重编译(灰度/修复场景)
    result = compile_spec(spec, force=True)
    assert result.payload["canvas_config"]["width"] == 1080


def test_fingerprint_content_sensitive() -> None:
    base = _minimal_spec()
    other = _minimal_spec()
    assert compute_build_fingerprint(base) == compute_build_fingerprint(other)
    other.canvas.width = 1920
    assert compute_build_fingerprint(base) != compute_build_fingerprint(other)
    # compile 块变化不影响指纹
    other2 = _minimal_spec()
    other2.compile.last_compiled_fingerprint = "deadbeef"
    assert compute_build_fingerprint(base) == compute_build_fingerprint(other2)


# ---------------------------------------------------------------------------
# publisher:双写 / 未知文件保留 / 校验失败回退
# ---------------------------------------------------------------------------
def _valid_payload() -> dict:
    return compile_spec(_minimal_spec(), force=True).payload


def test_validate_sdk_payload_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError):
        validate_sdk_payload([])
    payload = _valid_payload()
    payload.pop("canvas_config")
    with pytest.raises(ValueError):
        validate_sdk_payload(payload)
    payload = _valid_payload()
    payload["tracks"] = []
    with pytest.raises(ValueError):
        validate_sdk_payload(payload)


def test_publisher_double_write_and_preserve_unknown_files(tmp_path: Path) -> None:
    draft_dir = tmp_path / "AutoVideo_phase1"
    draft_dir.mkdir()
    (draft_dir / "draft_meta_info.json").write_text('{"tm_duration": 0}', encoding="utf-8")
    (draft_dir / ".autovideo_draft_mutations.jsonl").write_text("", encoding="utf-8")

    payload = _valid_payload()
    result = publish_compiled_draft(
        draft_dir,
        payload,
        proc_checker=lambda: False,  # 测试封闭:不探测真实剪映进程
    )
    assert result["ok"] is True
    content = json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
    info = json.loads((draft_dir / "draft_info.json").read_text(encoding="utf-8"))
    assert content == payload
    assert info == payload
    # 未知文件不被安全写入器误删(阶段1 验收项)
    assert (draft_dir / "draft_meta_info.json").is_file()
    assert (draft_dir / ".autovideo_draft_mutations.jsonl").is_file()


def test_publisher_duration_index_passthrough(tmp_path: Path) -> None:
    draft_dir = tmp_path / "AutoVideo_phase1_dur"
    calls: list[tuple[Path, int]] = []

    def _updater(draft_dir: Path, duration_us: int) -> dict[str, bool]:
        calls.append((draft_dir, duration_us))
        return {"draft_meta_info.json": True}

    publish_compiled_draft(
        draft_dir,
        _valid_payload(),
        duration_us=123_456,
        proc_checker=lambda: False,
        duration_updater=_updater,
    )
    assert calls == [(draft_dir, 123_456)]


def test_publisher_verify_failure_triggers_restore(tmp_path: Path) -> None:
    draft_dir = tmp_path / "AutoVideo_phase1_rollback"
    snapshotter_calls: list[Path] = []
    restored: list[Path] = []

    def _snapshotter(d: Path) -> Path:
        snapshotter_calls.append(d)
        return d

    def _restorer(d: Path) -> Path:
        restored.append(d)
        return d

    with pytest.raises(RuntimeError) as excinfo:
        publish_compiled_draft(
            draft_dir,
            _valid_payload(),
            proc_checker=lambda: False,
            snapshotter=_snapshotter,
            restorer=_restorer,
            verifier=lambda d: (False, "boom"),
        )
    assert "boom" in str(excinfo.value)
    assert snapshotter_calls == [draft_dir]
    assert restored == [draft_dir]


# ---------------------------------------------------------------------------
# resource_registry:ID 级解析(禁止按名称猜映射)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_registry_cache() -> None:
    resource_registry.reset_cache()
    yield
    resource_registry.reset_cache()


def _supported_item(category: str) -> dict:
    from jianying_sdk.resource_registry import load_compat_table

    table = load_compat_table()
    for item in table["categories"][category]["items"]:
        if item["status"] == "supported":
            return item
    raise AssertionError(f"交叉表 {category} 无 supported 条目")


def test_resolve_transition_supported() -> None:
    item = _supported_item("transition")
    resolved = resource_registry.resolve_transition(item["effect_id"])
    assert resolved is not None
    assert resolved.enum_name == "TransitionType"
    assert resolved.effect_id == item["effect_id"]
    assert resolved.resource_id == item["resource_id"]
    assert resolved.anim_kind is None


def test_resolve_transition_unsupported_returns_none() -> None:
    table = resource_registry.load_compat_table()
    missing = [
        item
        for item in table["categories"]["transition"]["items"]
        if item["status"] == "unsupported_missing_in_sdk"
    ]
    assert missing, "阶段0 交叉表应有 unsupported_missing_in_sdk 转场(快门/水墨)"
    for item in missing:
        assert resource_registry.resolve_transition(item["effect_id"]) is None
        assert resource_registry.resolve_transition(
            item["effect_id"], require_supported=False
        ) is None  # 枚举里确实不存在,连宽松解析也没有


def test_resolve_video_effect_and_text_animation() -> None:
    vfx = _supported_item("video_effect")
    resolved = resource_registry.resolve_video_effect(vfx["effect_id"])
    assert resolved is not None
    assert resolved.enum_name in ("VideoSceneEffectType", "VideoCharacterEffectType")

    intro = _supported_item("text_intro")
    resolved = resource_registry.resolve_text_animation(intro["effect_id"])
    assert resolved is not None
    assert resolved.anim_kind == "in"
    assert resolved.enum_name == "TextIntro"


def test_resolve_video_animation_by_enum_scan() -> None:
    jy = sdk()
    member = next(iter(jy.IntroType))
    resolved = resource_registry.resolve_video_animation(
        str(member.value.effect_id), require_supported=False
    )
    assert resolved is not None
    assert resolved.anim_kind == "in"
    assert resolved.enum_name == "IntroType"


def test_load_compat_table_missing_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        resource_registry, "COMPAT_TABLE_PATH", tmp_path / "nope.json"
    )
    with pytest.raises(RuntimeError):
        resource_registry.load_compat_table()
