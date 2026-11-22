"""交付 QA repair 闭环单测(2026-11-23 修复;审查计划 §5 阶段 3/4 集成矩阵)。

用**真实函数链**(无 LangGraph/PG)复现并锁死本次事故的闭环::

    带 hook 的 zh/en 原生草稿
        ↓ extract timeline(只豁免受信任 hook)
        ↓ QA repair 强制 SDK 编译(force_rebuild)
        ↓ SDK 全量重建(hook 被抹是预期行为)
        ↓ 按分支 replay hook(replay_hook_for_draft)
        ↓ 检查引用完整性与最终草稿内容
    (最终通过与否仍由 join_before_delivery 复检报告决定 —— §4.6)

场景矩阵(计划 §5 阶段 4):
- zh/en 均可编译 + hook 存在 → 双分支重建后 hook 均恢复;
- zh 被 hook 以外的人工文本阻断 → fail-closed,草稿保留,人工文本不丢;
- zh 成功 / en 失败 → 分支独立记录,只恢复成功分支;
- 指纹相同但草稿缺构建块 → force_rebuild 真实重建(不走 skipped_fingerprint);
- OPENING_HOOK_ENABLED=false → repair 不重放 hook。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jianying_sdk._sdk_loader import ensure_sdk_on_path

ensure_sdk_on_path()

import pyJianYingDraft
from pyJianYingDraft.local_materials import CropSettings
from tests.unit.test_jianying_sdk_phase2 import StubAudioMaterial, StubVideoMaterial
from tests.unit.test_jianying_sdk_phase7 import _setup_task, _write_spec_with_subtitles


@pytest.fixture(autouse=True)
def _stub_sdk_materials(monkeypatch: pytest.MonkeyPatch) -> None:
    """管线级测试替身:跳过 MediaInfo 探测(与 phase7 同款封闭化)。"""
    monkeypatch.setattr(
        pyJianYingDraft, "VideoMaterial",
        lambda path, material_name=None, crop_settings=None: StubVideoMaterial(
            path, crop_settings
        ),
    )
    monkeypatch.setattr(
        pyJianYingDraft, "AudioMaterial",
        lambda path, material_name=None: StubAudioMaterial(path),
    )


@pytest.fixture(autouse=True)
def _hermetic_proc_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    """写入互斥探测封闭化(测试进程不探测真实剪映)。"""
    import draft_ops.atomic_writer as aw

    monkeypatch.setattr(aw, "_default_proc_checker", lambda: False)


def _write_en_spec(tmp_path: Path) -> None:
    from jianying_sdk.spec import SubtitleSpec
    from nodes._draft_build_spec import replace_spec_sections

    replace_spec_sections(
        tmp_path,
        job_id="sess-loop",
        language="en",
        subtitles=[SubtitleSpec(
            subtitle_id="text-en-1", text="Hello", start_us=0, end_us=2_000_000,
            language="en",
            style={"size": 5.0, "bold": True, "color": [1.0, 1.0, 1.0], "align": 1,
                   "border": {"width": 40.0}, "transform_y": -0.8},
            wrap_content="Hello",
        )],
    )


def _dual_task(tmp_path: Path) -> tuple[Path, Path, Path]:
    """zh 主草稿 + en 分支草稿,各自带 spec(与 dual_draft_targets 布局一致)。"""
    zh_dir = tmp_path / "zh"
    zh_dir.mkdir(parents=True, exist_ok=True)
    _setup_task(zh_dir)
    _write_spec_with_subtitles(zh_dir)
    en_dir = tmp_path / "en_branch"
    en_dir.mkdir(parents=True, exist_ok=True)
    _setup_task(en_dir)
    _write_en_spec(en_dir)
    return zh_dir, en_dir, en_dir / "draft_content.json"


def _repair_state(zh_dir: Path, en_dir: Path) -> dict:
    return {
        "draft_path": str(zh_dir / "draft_content.json"),
        "draft_dir_en_branch": str(en_dir),
        "session_id": "sess-loop",
        "storyline_plan": None,
        "hook_title": "开场大字标题",
        "status_log": [],
        "error_log": [],
    }


def _track_by_id(draft: dict, track_id: str) -> dict | None:
    return next((t for t in draft.get("tracks") or [] if t.get("id") == track_id), None)


def _material_ids(draft: dict, bucket: str) -> list[str]:
    return [
        str(m.get("id")) for m in (draft.get("materials", {}).get(bucket) or [])
        if isinstance(m, dict) and m.get("id")
    ]


def _hook_refs_resolvable(draft: dict) -> bool:
    """hook 产物引用完整性:标题轨段 ref 都能解析到实际 material(计划 §4.5)。"""
    all_ids = {
        str(m.get("id"))
        for bucket in (draft.get("materials") or {}).values()
        if isinstance(bucket, list)
        for m in bucket
        if isinstance(m, dict) and m.get("id")
    }
    track = _track_by_id(draft, "track-text-hook")
    if track is None:
        return True  # 本稿无 hook 轨(如 en 分支),无引用可断
    return all(
        str(ref) in all_ids
        for seg in track.get("segments") or []
        for ref in (seg.get("extra_material_refs") or [])
    )


def test_loop_compile_wipes_hook_then_replay_restores(tmp_path: Path) -> None:
    """zh/en 均可编译 + hook 存在:重建抹 hook(replay 前置态)→ 重放恢复。"""
    import nodes.node_delivery_qa_repair as repair_mod
    from nodes.node_09b_inject_opening_hook import replay_hook_for_draft

    zh_dir, en_dir, _ = _dual_task(tmp_path)
    state = _repair_state(zh_dir, en_dir)

    # 主链形态:编译前 hook 已在(node_09b 于 node_11 之后写入)
    assert replay_hook_for_draft(zh_dir / "draft_content.json", state, lang="zh")
    assert replay_hook_for_draft(en_dir / "draft_content.json", state, lang="en")

    update = repair_mod._recompile_drafts(state)
    assert update["delivery_qa_compile"]["zh"]["status"] == "compiled", update
    assert update["delivery_qa_compile"]["en"]["status"] == "compiled", update

    # 编译后 hook 被抹(SDK 全量重建,spec 计划外产物不保留)—— 事故复现
    zh_wiped = json.loads((zh_dir / "draft_content.json").read_text(encoding="utf-8"))
    assert _track_by_id(zh_wiped, "track-text-hook") is None
    assert "hook-title-1" not in _material_ids(zh_wiped, "texts")
    assert "hook-effect-1" not in _material_ids(zh_wiped, "video_effects")

    # repair 重放 → hook 恢复且引用可解析;spec 字幕随编译落稿
    merged: dict = {"status_log": [], "error_log": []}
    replayed = repair_mod._replay_hooks_for_compiled(update["delivery_qa_compile"], state, merged)
    assert replayed == ["en", "zh"]

    zh_final = json.loads((zh_dir / "draft_content.json").read_text(encoding="utf-8"))
    assert _track_by_id(zh_final, "track-text-hook") is not None
    assert "hook-title-1" in _material_ids(zh_final, "texts")
    assert "hook-effect-1" in _material_ids(zh_final, "video_effects")
    assert _hook_refs_resolvable(zh_final)
    # spec 字幕随编译落稿(修复 subtitles_zh 类别的机制)
    assert len(zh_final["materials"]["texts"]) >= 2  # hook 标题 + spec 字幕

    en_final = json.loads((en_dir / "draft_content.json").read_text(encoding="utf-8"))
    # en 分支主链口径:只恢复特效,不建标题轨
    assert "hook-effect-1" in _material_ids(en_final, "video_effects")
    assert _track_by_id(en_final, "track-text-hook") is None
    assert _hook_refs_resolvable(en_final)


def test_loop_manual_text_still_blocks_compile(tmp_path: Path) -> None:
    """zh 被 hook 以外的人工文本阻断:fail-closed,草稿保留,人工文本不丢。"""
    import nodes.node_delivery_qa_repair as repair_mod

    zh_dir, en_dir, _ = _dual_task(tmp_path)
    state = _repair_state(zh_dir, en_dir)

    draft = json.loads((zh_dir / "draft_content.json").read_text(encoding="utf-8"))
    draft["tracks"].append({
        "type": "text", "id": "track-text-human", "name": "手工字幕",
        "segments": [{"id": "user-seg-1", "material_id": "user-mat-1",
                      "target_timerange": {"start": 0, "duration": 1_000_000}}],
    })
    (zh_dir / "draft_content.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    update = repair_mod._recompile_drafts(state)
    assert update["delivery_qa_compile"]["zh"]["status"] == "failed_blocked_features"
    assert update["delivery_qa_compile"]["en"]["status"] == "compiled"
    assert any("重编译未成功(zh)" in line for line in update["error_log"])

    # 人工文本原样保留(fail-closed 不覆盖)
    preserved = json.loads((zh_dir / "draft_content.json").read_text(encoding="utf-8"))
    assert _track_by_id(preserved, "track-text-human") is not None


def test_loop_replays_only_compiled_branch(tmp_path: Path) -> None:
    """zh 成功 / en 失败:只重放 zh;en 草稿不动。"""
    import nodes.node_delivery_qa_repair as repair_mod

    zh_dir, en_dir, _ = _dual_task(tmp_path)
    state = _repair_state(zh_dir, en_dir)

    # en 草稿注入人工内容 → 编译被阻
    en_draft = json.loads((en_dir / "draft_content.json").read_text(encoding="utf-8"))
    en_draft["tracks"].append({
        "type": "text", "id": "track-text-human-en", "name": "手工英文字幕",
        "segments": [{"id": "user-seg-en", "material_id": "user-mat-en",
                      "target_timerange": {"start": 0, "duration": 1_000_000}}],
    })
    (en_dir / "draft_content.json").write_text(json.dumps(en_draft, ensure_ascii=False), encoding="utf-8")
    en_before_hash = (en_dir / "draft_content.json").read_bytes()

    update = repair_mod._recompile_drafts(state)
    assert update["delivery_qa_compile"]["zh"]["status"] == "compiled"
    assert update["delivery_qa_compile"]["en"]["status"] == "failed_blocked_features"

    merged: dict = {"status_log": [], "error_log": []}
    replayed = repair_mod._replay_hooks_for_compiled(update["delivery_qa_compile"], state, merged)
    assert replayed == ["zh"]
    assert (en_dir / "draft_content.json").read_bytes() == en_before_hash  # en 草稿未动


def test_loop_force_rebuild_rebuilds_despite_same_fingerprint(tmp_path: Path) -> None:
    """指纹相同但草稿缺构建块:repair 恒 force_rebuild,真实重建而非跳过
    (QA 已判定缺失时 skipped_fingerprint 不是修复成功,审查 R-05)。"""
    import nodes.node_delivery_qa_repair as repair_mod
    from jianying_sdk.pipeline import compile_and_publish_draft
    from nodes._draft_build_spec import replace_spec_sections
    from jianying_sdk.spec import TransitionSpec

    zh_dir, _, _ = _dual_task(tmp_path)
    state = _repair_state(zh_dir, tmp_path / "nonexistent_en")

    # 首编成功(spec 无转场)→ 指纹短路对默认调用生效
    assert compile_and_publish_draft(
        zh_dir, zh_dir / "draft_content.json", job_id="sess-loop"
    )["status"] == "compiled"
    assert compile_and_publish_draft(
        zh_dir, zh_dir / "draft_content.json", job_id="sess-loop"
    )["status"] == "skipped_fingerprint"

    # node_09 重发射转场计划进 spec(repair 动作前置),repair 强制重建落稿
    # effect_id 取 SDK 注册表内 supported 条目(空/未注册会被编译器降级跳过)
    replace_spec_sections(zh_dir, transitions=[TransitionSpec(
        boundary_index=0, from_clip_id="clip_0001", to_clip_id="clip_0002",
        effect_id="1506926", resource_id="", name="测试转场", duration_us=500_000,
        is_overlap=False,
    )])
    update = repair_mod._recompile_drafts(state)
    assert update["delivery_qa_compile"]["zh"]["status"] == "compiled"
    zh_final = json.loads((zh_dir / "draft_content.json").read_text(encoding="utf-8"))
    # 转场真实落稿(transitions 类别的修复机制)
    assert len(zh_final["materials"].get("transitions") or []) == 1


def test_loop_no_hook_replay_when_disabled(tmp_path: Path, monkeypatch) -> None:
    """OPENING_HOOK_ENABLED=false:repair 不重放 hook(replayed_hook 空)。"""
    import config
    import nodes.node_delivery_qa_repair as repair_mod

    monkeypatch.setattr(config, "OPENING_HOOK_ENABLED", False)
    zh_dir, en_dir, _ = _dual_task(tmp_path)
    state = _repair_state(zh_dir, en_dir)

    update = repair_mod._recompile_drafts(state)
    assert update["delivery_qa_compile"]["zh"]["status"] == "compiled"

    merged: dict = {"status_log": [], "error_log": []}
    replayed = repair_mod._replay_hooks_for_compiled(update["delivery_qa_compile"], state, merged)
    assert replayed == []
    zh_final = json.loads((zh_dir / "draft_content.json").read_text(encoding="utf-8"))
    assert _track_by_id(zh_final, "track-text-hook") is None
    assert any("noop:disabled" in line for line in merged["status_log"])
