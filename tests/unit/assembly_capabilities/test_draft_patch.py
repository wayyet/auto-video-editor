"""P1-4 契约:DraftPatch 与候选 CanonicalTimeline(第六期)。

锁定(设计文档 §3.3 / 验收矩阵"编辑计划"行):
- patch 应用可改变 clip 集合 / 顺序 / 入出点,且每条变化可追溯 op 与证据;
- 原始 storyline_plan 永不被覆盖(apply 只动深拷贝);
- base_plan_sha256 不匹配 / 基线内容漂移 → 拒绝应用;
- 重复 apply 同一 patch → 结果逐字节一致(幂等);
- BGM / voiceover / transition / 字幕字段原样保留(关联轨由 mapper 从 clips
  重推导,补丁层不丢不悬空);
- trim / select 的源区间溢出 → PatchError;未知 operation → PatchError。
"""

from __future__ import annotations

import copy
import json

import pytest

from assembly_capabilities.draft_patch import (
    PatchError,
    add_operation,
    apply_patch,
    diff_plans,
    new_patch,
    plan_sha256,
    strip_internal_marks,
)
from tests.support.draft_influence import load_canonical_sample


@pytest.fixture
def base_plan() -> dict:
    return load_canonical_sample()


def _base_sha(base_plan: dict) -> str:
    return plan_sha256(base_plan)


# ---------------------------------------------------------------------------
# 基础:hash / 构造 / 幂等
# ---------------------------------------------------------------------------
def test_plan_sha256_stable_and_order_independent(base_plan: dict) -> None:
    a = plan_sha256(base_plan)
    reordered = {"clips": base_plan["clips"], "job_id": base_plan["job_id"]}
    b = plan_sha256({"job_id": base_plan["job_id"], "clips": base_plan["clips"]})
    assert a == plan_sha256(base_plan)
    assert b == plan_sha256(reordered)


def test_apply_empty_patch_is_noop_and_idempotent(base_plan: dict) -> None:
    sha = _base_sha(base_plan)
    patch = new_patch(sha, created_by="assembly_build_timeline")
    candidate, report = apply_patch(base_plan, patch, expect_base_sha256=sha)
    assert report["applied_operations"] == []
    # 空补丁候选 = 基线内容(保留原始计划;时间轴重排不改变原顺序的落位)
    assert json.dumps(candidate["clips"], sort_keys=True) == json.dumps(
        base_plan["clips"], sort_keys=True
    )
    # 幂等:再 apply 一次结果一致
    candidate2, report2 = apply_patch(base_plan, patch, expect_base_sha256=sha)
    assert json.dumps(candidate2, sort_keys=True) == json.dumps(candidate, sort_keys=True)


def test_apply_does_not_mutate_base_plan(base_plan: dict) -> None:
    sha = _base_sha(base_plan)
    patch = new_patch(sha, created_by="t")
    add_operation(
        patch, operation="trim", target_clip_id="clip_0001",
        source_in_ms=500, source_out_ms=2500, reason="黑帧剔除",
        evidence_refs=["assetA_c000"],
    )
    before = json.dumps(base_plan, sort_keys=True)
    apply_patch(base_plan, patch, expect_base_sha256=sha)
    assert json.dumps(base_plan, sort_keys=True) == before, "基线 plan 必须保持不可变"


# ---------------------------------------------------------------------------
# 基线漂移与非法 patch
# ---------------------------------------------------------------------------
def test_apply_rejects_base_hash_mismatch(base_plan: dict) -> None:
    patch = new_patch("b" * 64, created_by="t")
    with pytest.raises(PatchError, match="基线漂移|mismatch"):
        apply_patch(base_plan, patch, expect_base_sha256=_base_sha(base_plan))


def test_apply_rejects_content_drift(base_plan: dict) -> None:
    sha = _base_sha(base_plan)
    patch = new_patch(sha, created_by="t")
    drifted = copy.deepcopy(base_plan)
    drifted["clips"][0]["source_out_ms"] = 3999
    with pytest.raises(PatchError, match="drift"):
        apply_patch(drifted, patch, expect_base_sha256=sha)


def test_unknown_operation_rejected(base_plan: dict) -> None:
    patch = new_patch(_base_sha(base_plan), created_by="t")
    with pytest.raises(PatchError, match="unknown operation"):
        add_operation(patch, operation="explode", reason="x")


def test_non_dict_patch_rejected(base_plan: dict) -> None:
    with pytest.raises(PatchError):
        apply_patch(base_plan, {"operations": "not-a-list"}, expect_base_sha256=None)


# ---------------------------------------------------------------------------
# trim / remove / reorder / select
# ---------------------------------------------------------------------------
def test_trim_changes_source_range_and_relays_timeline(base_plan: dict) -> None:
    sha = _base_sha(base_plan)
    patch = new_patch(sha, created_by="t")
    add_operation(
        patch, operation="trim", target_clip_id="clip_0001",
        source_in_ms=1000, source_out_ms=3000, reason="剔除片头黑帧",
        evidence_refs=["assetA_c000", "black:1.0-2.0"],
    )
    candidate, report = apply_patch(base_plan, patch, expect_base_sha256=sha)
    assert report["applied_operations"] == ["op_0001"]
    clips = {c["clip_id"]: c for c in candidate["clips"]}
    assert (clips["clip_0001"]["source_in_ms"], clips["clip_0001"]["source_out_ms"]) == (1000, 3000)
    # preserve_and_remap:后继 clip 时间轴前移,不重叠不空洞
    assert clips["clip_0002"]["timeline_in_ms"] == 2000  # trim 后跨度 2000ms
    assert clips["clip_0003"]["timeline_in_ms"] == 6000
    # diff 可追溯 op 证据
    diffs = diff_plans(base_plan, candidate)
    trim_diff = next(d for d in diffs if d["clip_id"] == "clip_0001")
    assert trim_diff["change"] == "trimmed"
    assert "assetA_c000" in trim_diff["evidence_refs"]


def test_trim_preserves_implied_speed_for_compressed_clip(base_plan: dict) -> None:
    """P0 修复(20261117):trim 保留 trim 前隐含速度,压缩片段不再膨胀。

    clip_0001 改为压缩片段:源 0-15442ms 压进时间轴 0-1351ms(speed≈11.43)。
    trim 保尾 [12442,15442](新源跨度 3000ms)→ 新时间轴跨度 =
    round(3000 × 1351/15442) = 262ms,而不是 1:1 的 3000ms(那会把时间轴
    拉长 ≈11 倍,破坏总时长与配音对齐)。
    """
    plan = copy.deepcopy(base_plan)
    plan["clips"][0]["source_in_ms"] = 0
    plan["clips"][0]["source_out_ms"] = 15442
    plan["clips"][0]["timeline_in_ms"] = 0
    plan["clips"][0]["timeline_out_ms"] = 1351
    sha = _base_sha(plan)
    patch = new_patch(sha, created_by="t")
    add_operation(
        patch, operation="trim", target_clip_id="clip_0001",
        source_in_ms=12442, source_out_ms=15442, reason="剔除坏窗",
        evidence_refs=["black:12.0-13.0"],
    )
    candidate, report = apply_patch(plan, patch, expect_base_sha256=sha)
    assert report["applied_operations"] == ["op_0001"]
    clips = {c["clip_id"]: c for c in candidate["clips"]}
    assert (clips["clip_0001"]["source_in_ms"], clips["clip_0001"]["source_out_ms"]) == (12442, 15442)
    new_span = clips["clip_0001"]["timeline_out_ms"] - clips["clip_0001"]["timeline_in_ms"]
    assert new_span == 262, f"trim 必须保留隐含速度(期望 262ms,得到 {new_span}ms)"
    # preserve_and_remap:后继 clip 紧随其后落位
    assert clips["clip_0002"]["timeline_in_ms"] == 262


def test_remove_drops_clip_and_keeps_others(base_plan: dict) -> None:
    sha = _base_sha(base_plan)
    patch = new_patch(sha, created_by="t")
    add_operation(patch, operation="remove", target_clip_id="clip_0002", reason="坏素材")
    candidate, report = apply_patch(base_plan, patch, expect_base_sha256=sha)
    ids = [c["clip_id"] for c in candidate["clips"]]
    assert ids == ["clip_0001", "clip_0003"]
    assert report["clip_count_after"] == 2
    diffs = diff_plans(base_plan, candidate)
    assert any(d["change"] == "removed" and d["clip_id"] == "clip_0002" for d in diffs)


def test_remove_missing_target_is_skipped_for_idempotence(base_plan: dict) -> None:
    sha = _base_sha(base_plan)
    patch = new_patch(sha, created_by="t")
    add_operation(patch, operation="remove", target_clip_id="nope", reason="重复执行场景")
    candidate, report = apply_patch(base_plan, patch, expect_base_sha256=sha)
    assert report["applied_operations"] == []
    assert report["skipped_operations"]


def test_reorder_moves_clip_and_relays_timeline(base_plan: dict) -> None:
    sha = _base_sha(base_plan)
    patch = new_patch(sha, created_by="t")
    add_operation(patch, operation="reorder", target_clip_id="clip_0003", new_order=0, reason="钩子前置")
    candidate, _ = apply_patch(base_plan, patch, expect_base_sha256=sha)
    assert [c["clip_id"] for c in candidate["clips"]] == ["clip_0003", "clip_0001", "clip_0002"]
    # 时间轴仍单调且紧排(重排后各 clip span 不变)
    spans = [c["timeline_out_ms"] - c["timeline_in_ms"] for c in candidate["clips"]]
    assert spans == [4000, 4000, 4000]
    ins = [c["timeline_in_ms"] for c in candidate["clips"]]
    assert ins == sorted(ins)


def test_select_appends_new_clip_from_existing_media(base_plan: dict) -> None:
    sha = _base_sha(base_plan)
    patch = new_patch(sha, created_by="t")
    add_operation(
        patch, operation="select", target_clip_id="clip_sel_0001",
        source_media_id="media_0001", source_in_ms=20000, source_out_ms=24000,
        reason="LLM 选中的高光镜头", evidence_refs=["assetA_c005"],
    )
    candidate, report = apply_patch(base_plan, patch, expect_base_sha256=sha)
    assert report["clip_count_after"] == 4
    added = candidate["clips"][-1]
    assert added["clip_id"] == "clip_sel_0001"
    assert added["source_media_id"] == "media_0001"
    assert added["timeline_in_ms"] == 12000  # 追加在末尾(基线总长 12s)
    diffs = diff_plans(base_plan, candidate)
    added_diff = next(d for d in diffs if d["change"] == "added")
    assert added_diff["evidence_refs"] == ["assetA_c005"]


def test_select_rejects_unknown_media(base_plan: dict) -> None:
    sha = _base_sha(base_plan)
    patch = new_patch(sha, created_by="t")
    add_operation(
        patch, operation="select", source_media_id="media_9999",
        source_in_ms=0, source_out_ms=1000, reason="虚构素材",
    )
    with pytest.raises(PatchError, match="不在 source_media"):
        apply_patch(base_plan, patch, expect_base_sha256=sha)


def test_trim_rejects_source_overflow(base_plan: dict) -> None:
    sha = _base_sha(base_plan)
    patch = new_patch(sha, created_by="t")
    add_operation(
        patch, operation="trim", target_clip_id="clip_0001",
        source_in_ms=0, source_out_ms=999999, reason="越界",
    )
    with pytest.raises(PatchError, match="溢出"):
        apply_patch(base_plan, patch, expect_base_sha256=sha)


# ---------------------------------------------------------------------------
# 关联轨字段保留
# ---------------------------------------------------------------------------
def test_associated_track_fields_preserved(base_plan: dict) -> None:
    """trim/remove 后:audio(bgm_ref/voiceovers)、subtitles、options、clip 的
    voiceover_id / transition 语义原样保留 —— mapper 依据 clips 重推导,
    补丁层不得丢字段或制造悬空引用。"""
    sha = _base_sha(base_plan)
    patch = new_patch(sha, created_by="t")
    add_operation(
        patch, operation="trim", target_clip_id="clip_0001",
        source_in_ms=1000, source_out_ms=3000, reason="x",
    )
    add_operation(patch, operation="remove", target_clip_id="clip_0003", reason="y")
    candidate, _ = apply_patch(base_plan, patch, expect_base_sha256=sha)
    assert candidate["audio"] == base_plan["audio"]
    assert candidate["audio"]["bgm_ref"] == "bgm_0001"
    assert len(candidate["audio"]["voiceovers"]) == 2
    assert candidate["subtitles"] == base_plan["subtitles"]
    assert candidate["options"] == base_plan["options"]
    kept = {c["clip_id"]: c for c in candidate["clips"]}
    assert kept["clip_0001"]["voiceover_id"] == "voiceover_0001"
    assert kept["clip_0001"]["transition_out"] == "fade_out"
    assert kept["clip_0002"]["voiceover_id"] == "voiceover_0001"


def test_strip_internal_marks_removes_private_keys(base_plan: dict) -> None:
    sha = _base_sha(base_plan)
    patch = new_patch(sha, created_by="t")
    add_operation(
        patch, operation="trim", target_clip_id="clip_0001",
        source_in_ms=1000, source_out_ms=3000, reason="x", evidence_refs=["ev1"],
    )
    candidate, _ = apply_patch(base_plan, patch, expect_base_sha256=sha)
    cleaned = strip_internal_marks(candidate)
    for clip in cleaned["clips"]:
        assert "_patch_evidence" not in clip
        assert "_patch_reason" not in clip
    # 候选必须能过 CanonicalTimeline 校验(P1-4 硬门的前置条件)
    from storyline.contract import CanonicalTimeline

    model = CanonicalTimeline.model_validate(cleaned)
    assert len(model.clips) == 3
