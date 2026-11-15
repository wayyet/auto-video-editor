"""P1-7 统一计划取用 helper(``effective_canonical_plan``)契约单测。

三个 storyline_plan 消费点(node_05/08/09)共用同一取用口径:
- final plan 文件是权威(路径 + sha 双重校验),state 内联 dict 仅为镜像;
- 文件缺失/损坏/非 dict/sha 漂移 → ``assembly_final_plan_invalid``(主消费方
  必须 fail-closed,辅助消费方回落 storyline_plan);
- 未提交 final plan → ``storyline_plan`` 回落(QC 关/阻断/旧任务)。
"""

from __future__ import annotations

import json
from pathlib import Path

from assembly_capabilities.influence_trace import canonical_json_sha256
from nodes.storyline._common import (
    PLAN_SOURCE_FINAL,
    PLAN_SOURCE_FINAL_INVALID,
    PLAN_SOURCE_STORYLINE,
    effective_canonical_plan,
)
from tests.support.draft_influence import load_canonical_sample


def _write_final(tmp_path: Path, plan: dict) -> str:
    p = tmp_path / "final_plan.json"
    p.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_storyline_only_falls_back() -> None:
    """未提交 final plan(QC 关/旧任务)→ 原始 storyline_plan。"""
    plan = load_canonical_sample()
    state = {"storyline_plan": plan}
    out, source, notes = effective_canonical_plan(state)
    assert out == plan
    assert source == PLAN_SOURCE_STORYLINE
    assert notes == []


def test_final_plan_file_consumed(tmp_path: Path) -> None:
    """final plan 文件 + sha 一致 → 读到 final plan。"""
    plan = load_canonical_sample()
    final = dict(plan)
    state = {
        "storyline_plan": plan,
        "assembly_final_plan_path": _write_final(tmp_path, final),
        "assembly_final_plan_sha256": canonical_json_sha256(final),
    }
    out, source, notes = effective_canonical_plan(state)
    assert out == final
    assert source == PLAN_SOURCE_FINAL
    assert notes == []


def test_final_sha_drift_invalid(tmp_path: Path) -> None:
    """sha 漂移 → invalid(final plan 不被采用,调用方 fail-closed)。"""
    plan = load_canonical_sample()
    state = {
        "storyline_plan": plan,
        "assembly_final_plan_path": _write_final(tmp_path, plan),
        "assembly_final_plan_sha256": "0" * 64,
    }
    out, source, notes = effective_canonical_plan(state)
    assert out is None
    assert source == PLAN_SOURCE_FINAL_INVALID
    assert any("sha 漂移" in n for n in notes)


def test_final_file_missing_invalid(tmp_path: Path) -> None:
    """文件被删(用例 E)→ invalid,绝不静默回退 storyline_plan。"""
    plan = load_canonical_sample()
    missing = tmp_path / "gone" / "final_plan.json"
    state = {
        "storyline_plan": plan,
        "assembly_final_plan_path": str(missing),
        "assembly_final_plan_sha256": canonical_json_sha256(plan),
    }
    out, source, notes = effective_canonical_plan(state)
    assert out is None
    assert source == PLAN_SOURCE_FINAL_INVALID
    assert any("不存在" in n for n in notes)


def test_final_file_corrupt_invalid(tmp_path: Path) -> None:
    """损坏 JSON → invalid。"""
    plan = load_canonical_sample()
    p = tmp_path / "final_plan.json"
    p.write_text("not json", encoding="utf-8")
    state = {
        "storyline_plan": plan,
        "assembly_final_plan_path": str(p),
        "assembly_final_plan_sha256": canonical_json_sha256(plan),
    }
    out, source, _ = effective_canonical_plan(state)
    assert out is None
    assert source == PLAN_SOURCE_FINAL_INVALID


def test_file_authority_over_state_mirror(tmp_path: Path) -> None:
    """文件是权威:state 内联镜像存在但文件缺失 → invalid(不读镜像)。"""
    plan = load_canonical_sample()
    state = {
        "storyline_plan": plan,
        "assembly_final_plan": dict(plan),  # 内联镜像
        "assembly_final_plan_path": str(tmp_path / "missing.json"),
        "assembly_final_plan_sha256": canonical_json_sha256(plan),
    }
    out, source, _ = effective_canonical_plan(state)
    assert out is None
    assert source == PLAN_SOURCE_FINAL_INVALID


def test_nothing_available() -> None:
    """final plan 与 storyline_plan 皆缺(旧 stub 任务)→ (None, "")。"""
    out, source, notes = effective_canonical_plan({})
    assert out is None
    assert source == ""
    assert notes == []
