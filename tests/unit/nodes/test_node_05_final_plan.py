"""P1-7:``generate_draft`` 消费 ``assembly_final_plan`` 单测。

验收对照(计划文档 P1-7):
- final plan 被明确读取;改变 selected clip 的源区间 → 最终草稿视频轨对应变化;
- final plan 文件缺失 / sha 漂移 / 基线漂移 → ContractInvalid fail-closed(用例 E);
- 未提交 final plan → 回退 storyline_plan(status tag 可观测);
- 消费后影响链回写 mapped_draft_sha256 + generate_draft contribution。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from assembly_capabilities.influence_trace import canonical_json_sha256
from nodes.node_05_generate_draft import generate_initial_jianying_draft
from storyline.contract import ContractInvalid
from tests.support.draft_influence import (
    draft_video_summary,
    load_canonical_sample,
)


def _trim_clip3(plan: dict) -> dict:
    """在共享 fixture 上做一次可观察的 trim:clip_0003 源区间 8000-12000 → 8500-11500。"""
    out = copy.deepcopy(plan)
    for c in out["clips"]:
        if c["clip_id"] == "clip_0003":
            c["source_in_ms"] = 8500
            c["source_out_ms"] = 11500
            c["timeline_out_ms"] = c["timeline_in_ms"] + (
                c["source_out_ms"] - c["source_in_ms"]
            )
    return out


def _make_state(
    tmp_path: Path,
    *,
    final_plan: dict | None,
    base_sha: str | None = None,
    final_sha: str | None = None,
    with_storyline: bool = True,
) -> tuple[dict, Path, Path]:
    """构造带 assembly 字段的最小 state + final_plan.json + trace 文件路径。"""
    sample = load_canonical_sample()
    final_path = tmp_path / "final_plan.json"
    trace_path = tmp_path / "influence_trace.json"
    if final_plan is not None:
        final_path.write_text(
            json.dumps(final_plan, ensure_ascii=False), encoding="utf-8"
        )
        trace_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "base_plan_sha256": base_sha or canonical_json_sha256(sample),
                    "node_contributions": [],
                    "final_plan_sha256": final_sha or canonical_json_sha256(final_plan),
                    "mapped_draft_sha256": None,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    state: dict = {
        "video_input_path": str(tmp_path / "input.mp4"),
        "error_log": [],
        "status_log": [],
        # 不给 session_id:manifest 回填跳过,避免污染仓库 outputs 目录
    }
    if with_storyline:
        state["storyline_plan"] = sample
    if final_plan is not None:
        state["assembly_final_plan_path"] = str(final_path)
        state["assembly_final_plan_sha256"] = (
            final_sha if final_sha is not None else canonical_json_sha256(final_plan)
        )
        state["assembly_base_plan_sha256"] = (
            base_sha if base_sha is not None else canonical_json_sha256(sample)
        )
        state["assembly_influence_trace_path"] = str(trace_path)
    return state, final_path, trace_path


def _fake_encrypt(_draft_dir):
    from draft_ops.encryption_detector import DraftStatus

    return DraftStatus.PLAINTEXT


def test_final_plan_consumed_changes_video_track(tmp_path: Path) -> None:
    """最终计划(trim 过的 clip_0003)→ 草稿视频轨源区间跟随变化。"""
    sample = load_canonical_sample()
    final_plan = _trim_clip3(sample)
    state, _final_path, trace_path = _make_state(tmp_path, final_plan=final_plan)

    out = generate_initial_jianying_draft(state, tmp_path / "draft", encrypt_detector=_fake_encrypt)
    assert out["draft_path"]
    assert any("draft_from_assembly_final_plan" in t for t in out["status_log"])

    loaded = json.loads(Path(out["draft_path"]).read_text(encoding="utf-8"))
    summary = draft_video_summary(loaded)
    assert len(summary) == 3
    # mapper 把 ms → µs:clip_0003 的源起点 8500ms = 8_500_000µs(原始为 8_000_000)
    assert summary[2]["source_start_us"] == 8_500_000
    assert summary[2]["source_end_us"] == 11_500_000

    # 影响链回写:mapped_draft_sha256 + generate_draft contribution
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["mapped_draft_sha256"]
    assert len(trace["mapped_draft_sha256"]) == 64
    node_ids = {c["node_id"] for c in trace["node_contributions"]}
    assert "generate_draft" in node_ids


def test_storyline_fallback_without_final_plan(tmp_path: Path) -> None:
    """未提交 final plan(QC 关/阻断)→ 回退 storyline_plan,tag 可观测。"""
    sample = load_canonical_sample()
    state, _fp, _tp = _make_state(tmp_path, final_plan=None)
    out = generate_initial_jianying_draft(state, tmp_path / "draft", encrypt_detector=_fake_encrypt)
    assert out["draft_path"]
    assert any("draft_from_storyline_plan" in t for t in out["status_log"])
    loaded = json.loads(Path(out["draft_path"]).read_text(encoding="utf-8"))
    summary = draft_video_summary(loaded)
    assert summary[2]["source_start_us"] == 8_000_000


def test_final_sha_mismatch_fail_closed(tmp_path: Path) -> None:
    """final plan sha 与 state 记录漂移(用例 E)→ ContractInvalid,不写草稿。"""
    sample = load_canonical_sample()
    state, _fp, _tp = _make_state(
        tmp_path, final_plan=sample, final_sha="0" * 64
    )
    with pytest.raises(ContractInvalid):
        generate_initial_jianying_draft(state, tmp_path / "draft", encrypt_detector=_fake_encrypt)
    assert not (tmp_path / "draft" / "draft_content.json").exists()


def test_final_file_deleted_fail_closed(tmp_path: Path) -> None:
    """final plan 文件被删(用例 E)→ ContractInvalid,不静默回退旧计划。"""
    sample = load_canonical_sample()
    state, final_path, _tp = _make_state(tmp_path, final_plan=sample)
    final_path.unlink()  # 模拟删除
    with pytest.raises(ContractInvalid):
        generate_initial_jianying_draft(state, tmp_path / "draft", encrypt_detector=_fake_encrypt)


def test_base_plan_drift_fail_closed(tmp_path: Path) -> None:
    """storyline_plan 与 final plan 绑定的 base sha 漂移 → 拒绝套用旧候选。"""
    sample = load_canonical_sample()
    state, _fp, _tp = _make_state(
        tmp_path, final_plan=sample, base_sha="1" * 64
    )
    with pytest.raises(ContractInvalid):
        generate_initial_jianying_draft(state, tmp_path / "draft", encrypt_detector=_fake_encrypt)
