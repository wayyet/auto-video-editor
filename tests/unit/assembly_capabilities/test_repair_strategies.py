"""P1-6 契约:真实 Timeline Repair Patch(第六期)。

锁定(设计文档 §3.5 / 验收矩阵"Repair"行):
- 黑帧/卡帧/静音 issue → 真实 trim 避开坏窗 或 remove 坏片段 或 restore 回基线,
  **绝不只改 metadata**;
- 悬空引用 → restore/remove(修复或阻止);
- track_desync → 恢复基线 clip;
- 修复前后 plan sha 不同且 diff 可解释;无变化 → repair_noop;
- 同 patch 指纹不重放;结构性 error 达上限 → route fail-closed;
- 无策略的 issue → manual,不伪造修复成功。
"""

from __future__ import annotations

import config
import json
from pathlib import Path

import pytest

from assembly_capabilities.draft_patch import (
    apply_patch,
    plan_sha256,
    strip_internal_marks,
)
from assembly_capabilities.repair_strategies import (
    build_repair_patch,
    has_structural_error,
    patch_fingerprint,
    STRUCTURAL_CODES,
)
from nodes.assembly.node_validate_render_qc import route_after_assembly_qc
from tests.support.draft_influence import load_canonical_sample


@pytest.fixture
def base_plan(tmp_path: Path) -> dict:
    plan = load_canonical_sample()
    a = tmp_path / "media_a.mp4"
    b = tmp_path / "media_b.mp4"
    a.write_bytes(b"A" * 64)
    b.write_bytes(b"B" * 64)
    plan["source_media"][0]["file_uri"] = f"file:///{a.as_posix()}"
    plan["source_media"][1]["file_uri"] = f"file:///{b.as_posix()}"
    return plan


def _apply(base_plan: dict, candidate: dict, patch: dict) -> dict:
    repaired, _report = apply_patch(
        candidate, patch, expect_base_sha256=plan_sha256(candidate)
    )
    return strip_internal_marks(repaired)


# ---------------------------------------------------------------------------
# 策略层
# ---------------------------------------------------------------------------
class TestBuildRepairPatch:
    def test_black_frame_window_trims_clip(self, base_plan: dict) -> None:
        """预览黑帧窗口 [1000,2500]ms 落在 clip_0001(时间轴 0-4000)→ trim 避开。"""
        candidate = json.loads(json.dumps(base_plan))
        issues = [{
            "severity": "error", "code": "black_frame",
            "check": "候选预览 QC", "message": "black frames detected",
            "range": [1000, 2500],
        }]
        patch, manual, notes = build_repair_patch(
            candidate, issues, baseline_plan=base_plan
        )
        assert not manual
        trims = [op for op in patch["operations"] if op["operation"] == "trim"]
        assert len(trims) == 1
        op = trims[0]
        assert op["target_clip_id"] == "clip_0001"
        # 干净部分:头部 1000ms vs 尾部 1500ms → 保留尾部 [2500,4000](速度 1.0,
        # 时间轴偏移直接映射回源区间)
        assert (op["source_in_ms"], op["source_out_ms"]) == (2500, 4000)
        repaired = _apply(base_plan, candidate, patch)
        clip = next(c for c in repaired["clips"] if c["clip_id"] == "clip_0001")
        assert clip["source_in_ms"] == 2500, "黑帧 issue 必须真实改变源区间,而非 metadata"

    def test_full_coverage_black_window_removes_clip(self, base_plan: dict) -> None:
        """坏窗覆盖整条 clip(≥80%)→ remove(坏片段剔除)。"""
        candidate = json.loads(json.dumps(base_plan))
        issues = [{
            "severity": "error", "code": "freeze_frame",
            "check": "候选预览 QC", "message": "freeze frames detected",
            "range": [0, 4000],  # clip_0001 全区间
        }]
        patch, manual, _ = build_repair_patch(candidate, issues, baseline_plan=base_plan)
        assert not manual
        assert [op["operation"] for op in patch["operations"]] == ["remove"]
        assert patch["operations"][0]["target_clip_id"] == "clip_0001"

    def test_broken_reference_restores_baseline_clip(self, base_plan: dict) -> None:
        """悬空引用 → 恢复基线 clip(有基线)或移除(候选新增)。"""
        candidate = json.loads(json.dumps(base_plan))
        candidate["clips"].append({
            "clip_id": "clip_sel_0001", "source_media_id": "media_0001",
            "source_in_ms": 0, "source_out_ms": 2000,
            "timeline_in_ms": 12000, "timeline_out_ms": 14000,
        })
        issues = [
            {"severity": "error", "code": "broken_reference", "check": "候选草稿一致性",
             "message": "dangling", "clip_id": "clip_sel_0001"},
            {"severity": "error", "code": "missing_asset", "check": "候选草稿一致性",
             "message": "missing", "clip_id": "clip_0002"},
        ]
        patch, manual, notes = build_repair_patch(candidate, issues, baseline_plan=base_plan)
        assert not manual
        ops = {op["operation_id"]: op for op in patch["operations"]}
        assert any(op["operation"] == "remove" and op["target_clip_id"] == "clip_sel_0001"
                   for op in ops.values()), "无基线的候选新增 clip → remove"
        restores = [op for op in ops.values() if op["operation"] == "restore"]
        assert len(restores) == 1 and restores[0]["target_clip_id"] == "clip_0002"
        assert restores[0]["extra"]["restore_clip"]["clip_id"] == "clip_0002"

    def test_track_desync_restores_all_linked_clips(self, base_plan: dict) -> None:
        """配音组失配 → 恢复组内全部基线 clip。"""
        candidate = json.loads(json.dumps(base_plan))
        issues = [{
            "severity": "error", "code": "track_desync", "check": "关联轨道同步",
            "message": "vo mismatch", "clip_ids": ["clip_0001", "clip_0002"],
        }]
        patch, manual, _ = build_repair_patch(candidate, issues, baseline_plan=base_plan)
        assert not manual
        targets = {op["target_clip_id"] for op in patch["operations"] if op["operation"] == "restore"}
        assert targets == {"clip_0001", "clip_0002"}

    def test_invalid_range_clamps_to_media_duration(self, base_plan: dict) -> None:
        """源区间溢出 → trim 夹回 media_0001 的 30000ms;不可夹回 → restore。"""
        candidate = json.loads(json.dumps(base_plan))
        candidate["clips"][0]["source_out_ms"] = 99999
        issues = [{
            "severity": "error", "code": "invalid_range", "check": "候选草稿一致性",
            "message": "overflow", "clip_id": "clip_0001",
        }]
        patch, manual, _ = build_repair_patch(candidate, issues, baseline_plan=base_plan)
        assert not manual
        op = patch["operations"][0]
        assert op["operation"] == "trim"
        assert op["source_out_ms"] == 30000

    def test_unrepairable_issue_goes_manual(self, base_plan: dict) -> None:
        """未知 code / 无定位 → manual,不伪造 patch。"""
        candidate = json.loads(json.dumps(base_plan))
        issues = [{
            "severity": "error", "code": "mystery_code", "check": "?",
            "message": "unknown",
        }]
        patch, manual, _ = build_repair_patch(candidate, issues, baseline_plan=base_plan)
        assert patch["operations"] == []
        assert manual and manual[0]["manual_reason"]

    def test_warnings_are_not_repaired(self, base_plan: dict) -> None:
        candidate = json.loads(json.dumps(base_plan))
        issues = [{
            "severity": "warning", "code": "black_frame",
            "check": "候选预览 QC", "message": "minor", "range": [0, 1000],
        }]
        patch, manual, _ = build_repair_patch(candidate, issues, baseline_plan=base_plan)
        assert patch["operations"] == [] and not manual

    def test_repair_changes_plan_sha_and_is_explainable(self, base_plan: dict) -> None:
        """修复前后 plan sha 不同;diff 可追溯到 issue 证据。"""
        from assembly_capabilities.draft_patch import apply_patch, diff_plans

        candidate = json.loads(json.dumps(base_plan))
        before_sha = plan_sha256(candidate)
        issues = [{
            "severity": "error", "code": "black_frame",
            "check": "候选预览 QC", "message": "black",
            "range": [1000, 2500], "evidence_refs": ["blackdetect:1.0-2.0"],
        }]
        patch, _, _ = build_repair_patch(candidate, issues, baseline_plan=base_plan)
        repaired_raw, _report = apply_patch(
            candidate, patch, expect_base_sha256=plan_sha256(candidate)
        )
        after_sha = plan_sha256(strip_internal_marks(repaired_raw))
        assert after_sha != before_sha, "真实修复必须改变候选计划"
        # 未 strip 的原始候选带 _patch_evidence → diff 可追溯 issue 证据
        diffs = diff_plans(candidate, repaired_raw)
        assert diffs and diffs[0]["evidence_refs"] == ["blackdetect:1.0-2.0"]

    def test_no_change_patch_is_noop(self, base_plan: dict) -> None:
        """修复后的候选与原候选一致 → patch 无 operation(repair_noop 判据)。"""
        candidate = json.loads(json.dumps(base_plan))
        patch, _, _ = build_repair_patch(candidate, [], baseline_plan=base_plan)
        assert patch_fingerprint(patch) == patch_fingerprint({"operations": []})


class TestSpeedAwareRepair:
    """P0 修复(20261117):坏窗→源区间换算必须乘隐含速度(非 1.0 倍速)。

    场景形状(审查文档 §7.2):source 0-4000ms 压进 timeline 0-2000ms
    (speed=2.0);坏窗在时间轴坐标,keep 区间映射回源坐标须 ×2。
    """

    @staticmethod
    def _speedy_plan(base_plan: dict, *, s_in: int, s_out: int, t_in: int, t_out: int) -> dict:
        plan = json.loads(json.dumps(base_plan))
        clip = plan["clips"][0]
        clip["source_in_ms"] = s_in
        clip["source_out_ms"] = s_out
        clip["timeline_in_ms"] = t_in
        clip["timeline_out_ms"] = t_out
        return plan

    @staticmethod
    def _issue(start: int, end: int) -> dict:
        return {
            "severity": "error", "code": "black_frame",
            "check": "候选预览 QC", "message": "black frames detected",
            "range": [start, end],
        }

    def test_keep_tail_maps_timeline_offset_by_speed(self, base_plan: dict) -> None:
        """speed=2.0,坏窗 [300,700]:保尾 keep_in = 0 + 700×2 = 1400(不是 700)。"""
        plan = self._speedy_plan(base_plan, s_in=0, s_out=4000, t_in=0, t_out=2000)
        patch, manual, _ = build_repair_patch(plan, [self._issue(300, 700)], baseline_plan=base_plan)
        assert not manual
        trims = [op for op in patch["operations"] if op["operation"] == "trim"]
        assert len(trims) == 1
        assert (trims[0]["source_in_ms"], trims[0]["source_out_ms"]) == (1400, 4000)

    def test_keep_head_maps_head_span_by_speed(self, base_plan: dict) -> None:
        """speed=2.0,坏窗 [1200,1600]:保头 keep_out = 0 + 1200×2 = 2400。"""
        plan = self._speedy_plan(base_plan, s_in=0, s_out=4000, t_in=0, t_out=2000)
        patch, manual, _ = build_repair_patch(plan, [self._issue(1200, 1600)], baseline_plan=base_plan)
        assert not manual
        trims = [op for op in patch["operations"] if op["operation"] == "trim"]
        assert (trims[0]["source_in_ms"], trims[0]["source_out_ms"]) == (0, 2400)

    def test_speed_3x_rounding_and_tail_keep(self, base_plan: dict) -> None:
        """speed=3.0,坏窗尾界 1001ms → keep_in = round(1001×3) = 3003。"""
        plan = self._speedy_plan(base_plan, s_in=0, s_out=12000, t_in=0, t_out=4000)
        patch, manual, _ = build_repair_patch(plan, [self._issue(500, 1001)], baseline_plan=base_plan)
        assert not manual
        trims = [op for op in patch["operations"] if op["operation"] == "trim"]
        assert (trims[0]["source_in_ms"], trims[0]["source_out_ms"]) == (3003, 12000)

    def test_clean_part_too_short_on_timeline_removes_clip(self, base_plan: dict) -> None:
        """干净区按时间轴口径 <500ms → remove(源坐标 800ms 在 speed=2 下仅 400ms)。

        坏窗 [200,1600]:覆盖率 70%(<0.8 不走覆盖剔除),保尾 clean=400ms
        (时间轴)< 500ms → remove。
        """
        plan = self._speedy_plan(base_plan, s_in=0, s_out=4000, t_in=0, t_out=2000)
        patch, manual, _ = build_repair_patch(plan, [self._issue(200, 1600)], baseline_plan=base_plan)
        assert not manual
        assert [op["operation"] for op in patch["operations"]] == ["remove"]

    def test_speed_1x_keeps_legacy_mapping(self, base_plan: dict) -> None:
        """speed=1.0 行为不变:保尾 keep_in = 时间轴偏移(既有契约用例同型)。"""
        plan = self._speedy_plan(base_plan, s_in=0, s_out=4000, t_in=0, t_out=4000)
        patch, manual, _ = build_repair_patch(plan, [self._issue(1000, 2500)], baseline_plan=base_plan)
        assert not manual
        trims = [op for op in patch["operations"] if op["operation"] == "trim"]
        assert (trims[0]["source_in_ms"], trims[0]["source_out_ms"]) == (2500, 4000)

    def test_window_outside_any_clip_goes_manual(self, base_plan: dict) -> None:
        """坏窗超出全部候选时间轴(sample 总时长 12000ms)→ manual。"""
        plan = self._speedy_plan(base_plan, s_in=0, s_out=4000, t_in=0, t_out=2000)
        patch, manual, _ = build_repair_patch(plan, [self._issue(20000, 21000)], baseline_plan=base_plan)
        assert patch["operations"] == []
        assert manual and "不落在任何候选 clip" in manual[0]["manual_reason"]


# ---------------------------------------------------------------------------
# 结构性错误与 fail-closed 路由
# ---------------------------------------------------------------------------
class TestStructuralAndRouting:
    def test_has_structural_error_by_code(self) -> None:
        assert has_structural_error([{"severity": "error", "code": "track_desync"}])
        assert not has_structural_error([{"severity": "error", "code": "black_frame"}])
        assert not has_structural_error([{"severity": "warning", "code": "track_desync"}])
        assert STRUCTURAL_CODES

    def test_route_fail_closed_on_structural_at_max_retry(self, monkeypatch) -> None:
        import config as config_mod

        monkeypatch.setattr(config_mod, "ASSEMBLY_QC_MAX_RETRY", 2)
        state = {
            "assembly_qc_status": "escalated",
            "assembly_qc_retry_count": 2,
            "assembly_structural_error": True,
        }
        assert route_after_assembly_qc(state) == "assembly_failed"

    def test_route_soft_degrade_on_preview_only_at_max_retry(self, monkeypatch) -> None:
        import config as config_mod

        monkeypatch.setattr(config_mod, "ASSEMBLY_QC_MAX_RETRY", 2)
        state = {
            "assembly_qc_status": "escalated",
            "assembly_qc_retry_count": 2,
            "assembly_structural_error": False,
        }
        assert route_after_assembly_qc(state) == "assembly_write_report"

    def test_route_repair_below_max_retry(self, monkeypatch) -> None:
        import config as config_mod

        monkeypatch.setattr(config_mod, "ASSEMBLY_QC_MAX_RETRY", 2)
        state = {"assembly_qc_status": "escalated", "assembly_qc_retry_count": 0}
        assert route_after_assembly_qc(state) == "assembly_repair_loop"

    def test_route_pass_goes_to_report(self) -> None:
        assert route_after_assembly_qc({"assembly_qc_status": "pass"}) == "assembly_write_report"
        assert route_after_assembly_qc(
            {"assembly_qc_status": "pass_with_warnings"}
        ) == "assembly_write_report"


# ---------------------------------------------------------------------------
# 节点级:修复闭环 + 指纹去重 + 修复后复检数据流
# ---------------------------------------------------------------------------
def _candidate_state(tmp_path: Path, candidate: dict, issues: list) -> dict:
    out_dir = tmp_path / "outputs" / "repair-job" / "assembly"
    out_dir.mkdir(parents=True, exist_ok=True)
    qc_report = out_dir / "candidate_qc_report.json"
    qc_report.write_text(json.dumps({"issues": issues}), encoding="utf-8")
    return {
        "session_id": "repair-job",
        "storyline_outputs_root": str(tmp_path / "outputs" / "repair-job"),
        "error_log": [],
        "status_log": [],
        "storyline_plan": load_canonical_sample(),
        "assembly_candidate_plan": candidate,
        "assembly_candidate_plan_sha256": plan_sha256(candidate),
        "assembly_candidate_qc_report_path": str(qc_report),
    }


class TestNodeCandidateRepairPath:
    def test_repair_writes_candidate_and_history(self, monkeypatch, tmp_path: Path) -> None:
        """黑帧 issue → 修复后候选真实变化 + repair_history 记录前后 sha。"""
        monkeypatch.setattr(config, "ASSEMBLY_REPAIR_VISUAL_EVIDENCE", False)
        from nodes.assembly.node_repair_loop import assembly_repair_loop_node

        candidate = load_canonical_sample()
        state = _candidate_state(tmp_path, candidate, [{
            "severity": "error", "code": "black_frame",
            "check": "候选预览 QC", "message": "black frames detected",
            "range": [1000, 2500],
        }])
        out = assembly_repair_loop_node(state)

        assert "assembly_repair_loop_done" in out["status_log"]
        assert out["assembly_candidate_plan_sha256"] != plan_sha256(candidate)
        repaired = out["assembly_candidate_plan"]
        clip = next(c for c in repaired["clips"] if c["clip_id"] == "clip_0001")
        assert clip["source_in_ms"] == 2500  # 保留尾部干净区(速度 1.0 直接映射)
        # 历史记录:前后 sha + 指纹 + issue codes
        history = json.loads(Path(out["assembly_repair_history_path"]).read_text(encoding="utf-8"))
        assert history[0]["before_plan_sha256"] == plan_sha256(candidate)
        assert history[0]["after_plan_sha256"] == out["assembly_candidate_plan_sha256"]
        assert history[0]["issue_codes"] == ["black_frame"]
        assert history[0]["patch_fingerprint"]

    def test_duplicate_patch_fingerprint_not_replayed(self, monkeypatch, tmp_path: Path) -> None:
        """同 issue 第二轮 → 同 patch 指纹 → repair_noop,候选不再变化。"""
        monkeypatch.setattr(config, "ASSEMBLY_REPAIR_VISUAL_EVIDENCE", False)
        from nodes.assembly.node_repair_loop import assembly_repair_loop_node

        candidate = load_canonical_sample()
        issues = [{
            "severity": "error", "code": "invalid_range", "check": "候选草稿一致性",
            "message": "overflow", "clip_id": "clip_0001",
        }]
        candidate["clips"][0]["source_out_ms"] = 99999
        state = _candidate_state(tmp_path, candidate, issues)
        first = assembly_repair_loop_node(state)
        assert first["assembly_candidate_plan_sha256"] != plan_sha256(candidate)

        # 第二轮:候选已修复,同一 issue 报告再次出现(模拟 QC 未消除)→
        # 新候选上 invalid_range 已不成立(源区间合法)→ 无修复 op → noop
        state2 = dict(state)
        state2["assembly_candidate_plan"] = first["assembly_candidate_plan"]
        state2["assembly_candidate_plan_sha256"] = first["assembly_candidate_plan_sha256"]
        second = assembly_repair_loop_node(state2)
        assert "assembly_repair_noop" in second["status_log"]

        # 第三轮:强行让同一指纹再出现(报告仍指向旧候选的溢出问题,但候选
        # 已合法)→ 策略层不再产 op → 依旧 noop,不无限重放
        state3 = dict(state2)
        third = assembly_repair_loop_node(state3)
        assert third["assembly_candidate_plan_sha256"] == state2["assembly_candidate_plan_sha256"]

    def test_repair_noops_when_only_manual_issues(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(config, "ASSEMBLY_REPAIR_VISUAL_EVIDENCE", False)
        from nodes.assembly.node_repair_loop import assembly_repair_loop_node

        candidate = load_canonical_sample()
        state = _candidate_state(tmp_path, candidate, [{
            "severity": "error", "code": "mystery_code", "check": "?", "message": "unknown",
        }])
        out = assembly_repair_loop_node(state)
        assert "assembly_repair_noop" in out["status_log"]
        assert out.get("assembly_candidate_plan_sha256") == plan_sha256(candidate)
