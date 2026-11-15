"""``assembly_influence_trace`` 影响链追踪契约(第六期 P1-0)。

第六期设计文档 §2.3-E:为六个 assembly 节点建立可追踪因果链——每个节点
追加一条 contribution(谁产出了什么 artifact、被谁消费、影响了哪些
operation、最终落到哪些 clip),P1-7 由 ``assembly_write_report`` 固化为
``assembly_influence_trace.json``。

本模块先固定契约与校验规则,让 P1-2~P1-6 的写入方与测试共用同一把尺子:

- ``canonical_json_sha256``:全仓统一的"规范化 JSON sha256"(sort_keys +
  紧凑分隔符 + UTF-8),plan/patch/trace/hash 全部走这一个实现;
- ``new_influence_trace``:建一条空 trace;
- ``add_contribution``:节点写入一条 contribution(幂等键 = node_id+artifact);
- ``validate_influence_trace``:契约校验,返回 issue 列表(空 = 通过)。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# trace 结构版本;字段变更时 bump 并同步 validate 规则与测试
TRACE_SCHEMA_VERSION = 1

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json_sha256(obj: Any) -> str:
    """规范化 JSON sha256:sort_keys + 紧凑分隔符 + UTF-8。

    同一份数据无论键序如何,hash 恒定;这是 ``base_plan_sha256`` /
    ``final_plan_sha256`` / patch hash 的唯一计算口径。
    """
    payload = json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def is_sha256_hex(value: Any) -> bool:
    """是否为 64 位小写十六进制 sha256。"""
    return isinstance(value, str) and bool(_SHA256_HEX_RE.match(value))


def new_influence_trace(base_plan_sha256: str) -> dict[str, Any]:
    """建一条空 trace。``base_plan_sha256`` = 原始 storyline_plan 的规范化 hash。"""
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "base_plan_sha256": base_plan_sha256,
        "node_contributions": [],
        "final_plan_sha256": None,
        "mapped_draft_sha256": None,
    }


def add_contribution(
    trace: dict[str, Any],
    *,
    node_id: str,
    artifact: str,
    consumed_by: list[str],
    affected_operations: list[str] | None = None,
    final_clip_ids: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """向 trace 追加一条节点贡献记录,并返回该记录(便于节点连同日志落盘)。

    同一 (node_id, artifact) 重复追加视为契约错误(由
    :func:`validate_influence_trace` 拦截),这里不静默去重——写两遍说明
    调用方语义有问题,宁可让测试红。
    """
    contribution: dict[str, Any] = {
        "node_id": node_id,
        "artifact": artifact,
        "consumed_by": list(consumed_by),
        "affected_operations": list(affected_operations or []),
        "final_clip_ids": list(final_clip_ids or []),
    }
    if extra:
        contribution["extra"] = extra
    trace.setdefault("node_contributions", []).append(contribution)
    return contribution


def load_or_create_trace(path, base_plan_sha256: str) -> dict[str, Any]:
    """读 ``influence_trace.json``;缺失/损坏/契约不过 → 建新 trace(重建不静默带病续写)。"""
    import json
    from pathlib import Path as _Path

    p = _Path(path)
    if p.is_file():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and not validate_influence_trace(existing):
            return existing
    return new_influence_trace(base_plan_sha256)


def save_trace(path, trace: dict[str, Any]) -> None:
    """trace 落盘(原子性不做要求——trace 是审计产物,坏行由下次 load 兜底重建)。"""
    import json
    from pathlib import Path as _Path

    p = _Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(trace, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def validate_influence_trace(trace: Any) -> list[str]:
    """契约校验,返回 issue 文本列表;空列表 = 通过。"""
    issues: list[str] = []
    if not isinstance(trace, dict):
        return [f"trace 必须是 dict,得到 {type(trace).__name__}"]

    if trace.get("schema_version") != TRACE_SCHEMA_VERSION:
        issues.append(
            f"schema_version 必须是 {TRACE_SCHEMA_VERSION},得到 {trace.get('schema_version')!r}"
        )
    if not is_sha256_hex(trace.get("base_plan_sha256")):
        issues.append(
            f"base_plan_sha256 必须是 64 位小写 sha256,得到 {trace.get('base_plan_sha256')!r}"
        )

    contributions = trace.get("node_contributions")
    if not isinstance(contributions, list):
        issues.append(
            f"node_contributions 必须是 list,得到 {type(contributions).__name__}"
        )
        contributions = []

    seen: set[tuple[str, str]] = set()
    for idx, item in enumerate(contributions):
        where = f"node_contributions[{idx}]"
        if not isinstance(item, dict):
            issues.append(f"{where} 必须是 dict,得到 {type(item).__name__}")
            continue
        node_id = item.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            issues.append(f"{where}.node_id 必须是非空字符串,得到 {node_id!r}")
        artifact = item.get("artifact")
        if not isinstance(artifact, str) or not artifact.strip():
            issues.append(f"{where}.artifact 必须是非空字符串,得到 {artifact!r}")
        consumed_by = item.get("consumed_by")
        if (
            not isinstance(consumed_by, list)
            or not consumed_by
            or not all(isinstance(c, str) and c.strip() for c in consumed_by)
        ):
            issues.append(
                f"{where}.consumed_by 必须是非空字符串列表(必须有真实消费者),得到 {consumed_by!r}"
            )
        for key in ("affected_operations", "final_clip_ids"):
            value = item.get(key)
            if value is not None and not (
                isinstance(value, list) and all(isinstance(v, str) and v for v in value)
            ):
                issues.append(f"{where}.{key} 必须是字符串列表,得到 {value!r}")
        if isinstance(node_id, str) and isinstance(artifact, str):
            key = (node_id, artifact)
            if key in seen:
                issues.append(f"{where} (node_id={node_id!r}, artifact={artifact!r}) 重复贡献")
            seen.add(key)

    for key in ("final_plan_sha256", "mapped_draft_sha256"):
        value = trace.get(key)
        if value is not None and not is_sha256_hex(value):
            issues.append(f"{key} 为 None 或 64 位小写 sha256,得到 {value!r}")

    return issues


__all__ = [
    "TRACE_SCHEMA_VERSION",
    "canonical_json_sha256",
    "is_sha256_hex",
    "new_influence_trace",
    "add_contribution",
    "load_or_create_trace",
    "save_trace",
    "validate_influence_trace",
]
