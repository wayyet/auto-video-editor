"""花字选取器(2026-11-22 SDK 链转正:六段 legacy 链退役)。

SDK 链上花字经 ``TextDecorationSpec.text_effect``(ResourceRef)进入
DraftBuildSpec,node_11 编译时按三重门禁 ``TextSegment.add_effect(effect_id)``
落草稿(SDK 双 ID 合一;materials.effects 6 键精简形态)。本模块只剩**选取**
职责:池化确定性选 1 个 ready 条目,不触碰草稿;node_10(中文)与 node_10b
(英文)经 ``apply_text_fx`` 共用。

核心约束(审查修订版 §4.3/§4.5/R-05/R-09/R-10;资源身份来源不变 ——
真机 rp.db 审计 + GUI 黄金样本,非 SDK metadata 扫描):

- **缓存可用性 = 三条件**:verified(GUI 实证)+ 缓存目录存在
  (``Cache/artistEffect/<effect_id>/<md5>``,独立于字幕动画的 ``Cache/effect``)
  + ``verification.cache_manifest_sha256`` 与当前缓存内容一致(缺失 =
  ``cache_unverified``、不一致 = ``verification_stale``,均不可用 —— 目录存在
  ≠ 资源包完整,「螺旋上升」坏包实证)。
- **降级语义**:选取失败/开关关闭 = 不进 spec(compile 重建时自然无花字);
  资源级降级,不阻断其余装饰。

摘要与 status_log 标签由调用方(node_10/10b)拼接;**本机缓存绝对路径不进日志**
(审查 §4.7),审计只落资源名/effect_id/池口径数字。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from config import (
    JIANYING_ARTIST_EFFECT_CACHE_ROOT,
    TEXT_FLOWER_NAME,
    TEXT_FLOWER_POOL_MODE,
)
from jy_common.cache_manifest import build_cache_manifest
from jy_common.native_fx import flower_effect_cache_available
from jy_common.template_library import TemplateLibrary


def flower_entry_status(flower_meta: dict[str, Any], cache_root: str | Path | None) -> str:
    """花字池条目可用性状态(审查 R-05/TF-03~06;「VIP 元数据存在」「缓存存在」
    「验证未过期」三个状态不压缩)。

    Returns:
        ``disabled`` / ``unverified`` / ``cache_missing`` / ``cache_unverified`` /
        ``verification_stale`` / ``ready`` —— 只有 ``ready`` 进入可用候选。
    """
    if not flower_meta.get("enabled", True):
        return "disabled"
    if not flower_meta.get("verified"):
        return "unverified"
    effect_id = str(flower_meta.get("effect_id") or "")
    md5 = str(flower_meta.get("md5") or "")
    if not effect_id or not md5 or not cache_root:
        return "cache_missing"
    if not flower_effect_cache_available(flower_meta, cache_root):
        return "cache_missing"
    expected_sha = str((flower_meta.get("verification") or {}).get("cache_manifest_sha256") or "")
    if not expected_sha:
        # 有 GUI 实证但验证记录未固化 manifest 摘要 → 不得静默当作完整可用
        return "cache_unverified"
    manifest = build_cache_manifest(cache_root, effect_id, md5)
    if manifest is None:
        return "cache_missing"
    if manifest["manifest_sha256"] != expected_sha:
        return "verification_stale"
    return "ready"


def _pick_flower(
    flower_lib: TemplateLibrary, job_id: str, cache_root: str | Path | None
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """花字选取(池化,与 node_10 ``_pick_intro_animation`` 同款机制)。

    - auto(缺省):全池按 ``sha256(job_id|"text_flower_pool")`` 定起点,按池序
      轮转取第一个 ``ready`` 条目 —— 同任务同结果、zh/en 一致、跨任务多样化。
    - fixed:config 名(须 ``ready``)→ 回落可用子集池序第一个。
    - 无可用条目 → ``(None, audit)``,调用方降级(不进 spec)。
    """
    cache_root_str = str(cache_root) if cache_root else ""
    audit: dict[str, Any] = {
        "mode": "fixed" if TEXT_FLOWER_POOL_MODE == "fixed" else "auto",
        "cache_root": cache_root_str,
        "pool_size": 0,
        "status_counts": {},
        "seed_start_index": None,
        "selected_index": None,
        "fallback_offset": None,
    }
    if not flower_lib.has_flower_lib:
        return None, audit
    rows = flower_lib.list_flower_rows()
    audit["pool_size"] = len(rows)
    if not rows:
        return None, audit
    statuses: dict[str, int] = {}
    for row in rows:
        state = flower_entry_status(row, cache_root)
        statuses[state] = statuses.get(state, 0) + 1
    audit["status_counts"] = statuses
    usable_ids = {
        id(row) for row in rows if flower_entry_status(row, cache_root) == "ready"
    }
    if not usable_ids:
        return None, audit
    if TEXT_FLOWER_POOL_MODE != "fixed":
        seed_input = f"{job_id}|text_flower_pool"
        seed = int(hashlib.sha256(seed_input.encode("utf-8")).hexdigest()[:8], 16)
        start = seed % len(rows)
        audit["seed_start_index"] = start
        for offset in range(len(rows)):
            candidate = rows[(start + offset) % len(rows)]
            if id(candidate) in usable_ids:
                audit["selected_index"] = (start + offset) % len(rows)
                audit["fallback_offset"] = offset
                return candidate, audit
        return None, audit
    fixed = flower_lib.pick_flower_by_name(TEXT_FLOWER_NAME)
    if fixed is not None and id(fixed) in usable_ids:
        audit["selected_index"] = next(
            (i for i, r in enumerate(rows) if r.get("name") == fixed.get("name")), None
        )
        return fixed, audit
    for idx, row in enumerate(rows):
        if id(row) in usable_ids:
            audit["selected_index"] = idx
            return row, audit
    return None, audit


def select_text_flower(
    flower_lib: TemplateLibrary,
    *,
    job_id: str,
    enabled: bool,
    tag: str,
    cache_root: str | Path | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """花字选取(**纯选取,不改草稿**;SDK 链上装饰经 TextDecorationSpec 进 spec)。

    Returns:
        (告警列表, 摘要 dict)。摘要 ``status`` ∈ ``disabled`` /
        ``degraded_no_resource`` / ``selected``;``selected`` 键携带
        name/effect_id/resource_id(库条目双 ID;``effect_id == resource_id``
        由编译器三重门禁复检)。
    """
    if cache_root is None:
        cache_root = JIANYING_ARTIST_EFFECT_CACHE_ROOT
    summary: dict[str, Any] = {
        "status": "disabled",
        "selection_audit": {},
        "selected": None,
    }
    warnings: list[str] = []
    # R-10:开关关闭 = 本轮无花字进 spec;不发告警
    if not enabled:
        return warnings, summary

    flower_meta, selection_audit = _pick_flower(flower_lib, job_id, cache_root)
    summary["selection_audit"] = selection_audit
    if flower_meta is None:
        summary["status"] = "degraded_no_resource"
        warnings.append(f"[{tag}] 无可用花字资源,花字降级(不进 spec)")
        return warnings, summary

    summary["selected"] = {
        "name": str(flower_meta.get("name") or ""),
        "effect_id": str(flower_meta.get("effect_id") or ""),
        "resource_id": str(flower_meta.get("resource_id") or flower_meta.get("effect_id") or ""),
    }
    summary["status"] = "selected"
    return warnings, summary
