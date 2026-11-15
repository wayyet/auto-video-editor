"""P1-0 契约:``assembly_influence_trace`` 影响链追踪(第六期)。

六个 assembly 节点的写入方(P1-2~P1-6 逐阶段接入)与 P1-7 的固化方
(``assembly_write_report``)共用本契约;这里固定:
- 契约校验通过 / 失败的边界;
- ``canonical_json_sha256`` 规范化 hash 的稳定性(键序无关)。
"""

from __future__ import annotations

from assembly_capabilities.influence_trace import (
    TRACE_SCHEMA_VERSION,
    add_contribution,
    canonical_json_sha256,
    is_sha256_hex,
    new_influence_trace,
    validate_influence_trace,
)

_SHA = "a" * 64


# ---------------------------------------------------------------------------
# canonical_json_sha256
# ---------------------------------------------------------------------------
def test_canonical_json_sha256_key_order_independent() -> None:
    a = {"b": 1, "a": {"d": 2, "c": 3}}
    b = {"a": {"c": 3, "d": 2}, "b": 1}
    assert canonical_json_sha256(a) == canonical_json_sha256(b)
    assert is_sha256_hex(canonical_json_sha256(a))


def test_canonical_json_sha256_changes_with_content() -> None:
    assert canonical_json_sha256({"a": 1}) != canonical_json_sha256({"a": 2})


# ---------------------------------------------------------------------------
# trace 契约
# ---------------------------------------------------------------------------
def test_new_trace_passes_validation() -> None:
    trace = new_influence_trace(_SHA)
    assert trace["schema_version"] == TRACE_SCHEMA_VERSION
    assert trace["node_contributions"] == []
    assert validate_influence_trace(trace) == []


def test_add_contribution_accumulates() -> None:
    trace = new_influence_trace(_SHA)
    add_contribution(
        trace,
        node_id="assembly_discover_and_probe",
        artifact="assembly/media.json",
        consumed_by=["assembly_build_timeline"],
        affected_operations=["op_001"],
        final_clip_ids=["clip_0001"],
    )
    add_contribution(
        trace,
        node_id="assembly_visual_observe",
        artifact="assembly/visual_observation.json",
        consumed_by=["assembly_build_timeline"],
    )
    assert len(trace["node_contributions"]) == 2
    assert validate_influence_trace(trace) == []


def test_validation_rejects_missing_consumer() -> None:
    trace = new_influence_trace(_SHA)
    add_contribution(
        trace,
        node_id="assembly_build_timeline",
        artifact="assembly/candidate_plan.json",
        consumed_by=[],  # 空消费者 = 死产物,契约不允许
    )
    issues = validate_influence_trace(trace)
    assert any("consumed_by" in i for i in issues)


def test_validation_rejects_duplicate_contribution() -> None:
    trace = new_influence_trace(_SHA)
    for _ in range(2):
        add_contribution(
            trace,
            node_id="assembly_discover_and_probe",
            artifact="assembly/media.json",
            consumed_by=["assembly_visual_observe"],
        )
    issues = validate_influence_trace(trace)
    assert any("重复贡献" in i for i in issues)


def test_validation_rejects_bad_base_hash() -> None:
    trace = new_influence_trace("not-a-hash")
    issues = validate_influence_trace(trace)
    assert any("base_plan_sha256" in i for i in issues)


def test_validation_rejects_non_dict() -> None:
    assert validate_influence_trace(None)  # 非法输入必须产生 issue 而非抛异常


def test_validation_rejects_bad_final_hash() -> None:
    trace = new_influence_trace(_SHA)
    trace["final_plan_sha256"] = "xyz"
    issues = validate_influence_trace(trace)
    assert any("final_plan_sha256" in i for i in issues)
