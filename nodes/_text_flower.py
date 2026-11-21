"""原生花字绑定器(2026-11-21 第九期,审查修订版执行计划阶段 3)。

剪映原生花字 = ``materials.effects`` 中 ``type=text_effect`` 的 artistEffect 素材
再被 text 轨段 ``extra_material_refs`` 引用(Golden Sample:console-52373c6f9e11
中文稿 UI 应用「互动立体」实证)。本模块实现「选取 → 构造 → 绑定 → 清理」,
node_10(中文)与 node_10b(英文)经 ``apply_text_fx`` 共用。

核心约束(审查修订版 §4.3/§4.5/R-03/R-05/R-09/R-10):

- **ID 策略 = 固定命名空间 UUIDv5**(由字幕 material id 确定性派生,重放稳定;
  Golden Sample 的 material id 是 UUID,``text-flower-*`` 前缀无 GUI 兼容证据,
  故不用前缀 id)。我方素材以私有标记键 ``autovideo_role=text-flower`` 识别
  (对齐 ``materials.texts`` 私有字段先例)。
- **缓存可用性 = 三条件**:verified(GUI 实证)+ 缓存目录存在
  (``Cache/artistEffect/<effect_id>/<md5>``,独立于字幕动画的 ``Cache/effect``)
  + ``verification.cache_manifest_sha256`` 与当前缓存内容一致(缺失 = 
  ``cache_unverified``、不一致 = ``verification_stale``,均不可用 —— 目录存在
  ≠ 资源包完整,「螺旋上升」坏包实证)。
- **segment 先行**:先按字幕 material id 找 text 轨段,找不到记
  ``no_text_segment`` 且**不写** effect material(不产生孤儿,R-09)。
- **upsert + refs 去重**:重放字节级稳定;人工 UUID 花字(无我方标记键)与
  人工文本一律不动(TF-15)。
- **非破坏降级**:选取失败/开关关闭都不清理旧数据(R-10:disabled ≠ cleanup);
  清理只在本轮已成功写入后执行,且只删带我方标记键的条目。

摘要与 status_log 标签由调用方(node_10/10b)拼接;**本机缓存绝对路径不进日志**
(审查 §4.7),审计只落资源名/effect_id/池口径数字。
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from config import (
    JIANYING_ARTIST_EFFECT_CACHE_ROOT,
    TEXT_FLOWER_NAME,
    TEXT_FLOWER_POOL_MODE,
)
from jy_common.cache_manifest import build_cache_manifest
from jy_common.native_fx import (
    OUR_TEXT_FLOWER_PRIVATE_KEY,
    OUR_TEXT_FLOWER_ROLE,
    build_native_text_effect_material,
    flower_effect_cache_available,
)
from jy_common.template_library import TemplateLibrary
from nodes._draft_io import find_text_segment_by_material_id

#: 我方正文字幕 id 前缀(node_08 / node_08b 产出;开场标题 hook-title-* 不在列)
_OUR_TEXT_PREFIXES = ("text-zh-", "text-en-")

#: UUIDv5 固定命名空间(项目级、任务无关;由稳定字符串一次性派生)。
#: 派生输入改动 = 全部历史我方花字 id 失配 → 只影响清理识别,不影响渲染;
#: 因此该字符串一经发布不得再改。
_FLOWER_ID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "auto-video-editor/text-flower/20261121"
)


def _is_our_text(text_obj: dict) -> bool:
    text_id = str(text_obj.get("id") or "")
    return any(text_id.startswith(p) for p in _OUR_TEXT_PREFIXES)


def our_flower_material_id(text_id: str) -> str:
    """字幕 material id → 我方花字 material id(UUIDv5,大写,Golden Sample 同形)。

    同一字幕重放/恢复必得同一 id(upsert 幂等);不同字幕必不同(无 seed 碰撞面)。
    """
    return str(uuid.uuid5(_FLOWER_ID_NAMESPACE, text_id)).upper()


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
    - 无可用条目 → ``(None, audit)``,调用方降级不写(保留已有花字,R-10)。
    """
    cache_root_str = str(cache_root) if cache_root else ""
    audit: dict[str, Any] = {
        "mode": "fixed" if TEXT_FLOWER_POOL_MODE == "fixed" else "auto",
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


def _cleanup_stale_flowers(draft: dict, kept_ids: set[str]) -> int:
    """清理我方旧花字(带标记键且未续用)与其 text 轨段 refs,返回清理数。

    只动带 ``autovideo_role=text-flower`` 私有标记的条目;人工 UUID 花字
    (无标记)永远不删(TF-15)。仅在本轮已成功写入后调用(R-10)。
    """
    materials = draft.setdefault("materials", {})
    effects_bucket = materials.setdefault("effects", [])
    removed_ids: set[str] = set()
    kept: list[dict[str, Any]] = []
    for item in effects_bucket:
        item_id = str(item.get("id") or "")
        is_ours = item.get(OUR_TEXT_FLOWER_PRIVATE_KEY) == OUR_TEXT_FLOWER_ROLE
        if is_ours and item_id not in kept_ids:
            removed_ids.add(item_id)
            continue
        kept.append(item)
    if not removed_ids:
        return 0
    effects_bucket[:] = kept
    for track in draft.get("tracks", []) or []:
        if track.get("type") != "text":
            continue
        for seg in track.get("segments", []) or []:
            refs = seg.get("extra_material_refs")
            if isinstance(refs, list) and removed_ids:
                seg["extra_material_refs"] = [r for r in refs if str(r) not in removed_ids]
    return len(removed_ids)


def apply_text_flower_effects(
    draft: dict,
    flower_lib: TemplateLibrary,
    *,
    job_id: str,
    enabled: bool,
    tag: str,
    cache_root: str | Path | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """为我方正文字幕写原生花字(就地改 ``draft``),返回 (告警, 摘要)。

    处理对象:仅 ``text-zh-`` / ``text-en-`` 我方正文字幕(开场标题
    ``hook-title-*`` 与人工 UUID 文本不处理,TF-08/09/15)。

    Returns:
        (告警列表, 摘要 dict)。摘要含 ``status`` ∈ ``disabled`` /
        ``degraded_no_resource`` / ``written`` / ``no_text_segment`` /
        ``not_applicable``(审查 §P5 状态区分;不把四个不同状态压成一个标志)。
    """
    if cache_root is None:
        cache_root = JIANYING_ARTIST_EFFECT_CACHE_ROOT
    summary: dict[str, Any] = {
        "texts_scanned": 0,
        "flowers_written": 0,
        "no_text_segment": 0,
        "cleaned_stale": 0,
        "status": "disabled",
        "selection_audit": {},
    }
    warnings: list[str] = []
    # R-10:开关关闭 = 只停止本轮新写入,保留草稿已有花字;不做清理、不发告警
    if not enabled:
        return warnings, summary

    flower_meta, selection_audit = _pick_flower(flower_lib, job_id, cache_root)
    summary["selection_audit"] = selection_audit
    if flower_meta is None:
        summary["status"] = "degraded_no_resource"
        warnings.append(
            f"[{tag}] 无可用花字资源,花字降级不写入(已有花字保留不清理)"
        )
        return warnings, summary

    summary["selected"] = {
        "name": str(flower_meta.get("name") or ""),
        "effect_id": str(flower_meta.get("effect_id") or ""),
        # 2026-11-21 第10期阶段4:补 resource_id(spec 发射消费;花字库条目双 ID)
        "resource_id": str(flower_meta.get("resource_id") or flower_meta.get("effect_id") or ""),
    }
    materials = draft.setdefault("materials", {})
    effects_bucket = materials.setdefault("effects", [])
    kept_ids: set[str] = set()

    for text_obj in materials.get("texts", []) or []:
        if not _is_our_text(text_obj):
            continue
        summary["texts_scanned"] += 1
        text_id = str(text_obj.get("id") or "")
        # R-09:segment 先行 —— 找不到轨段不写 effect material(不产生孤儿)
        seg = find_text_segment_by_material_id(draft, text_id)
        if seg is None:
            summary["no_text_segment"] += 1
            continue
        material = build_native_text_effect_material(
            material_id=our_flower_material_id(text_id),
            flower_meta=flower_meta,
            artist_effect_cache_root=cache_root,
        )
        # upsert 语义:同 id 旧条目先移除再追加(重放幂等,TF-12)
        effects_bucket[:] = [e for e in effects_bucket if e.get("id") != material["id"]]
        effects_bucket.append(material)
        kept_ids.add(material["id"])
        refs = seg.setdefault("extra_material_refs", [])
        if material["id"] not in refs:
            refs.append(material["id"])
        summary["flowers_written"] += 1

    if summary["flowers_written"]:
        # R-10/TF-16:清理只在本轮已写入后执行;降级路径(未写入)绝不清理
        summary["cleaned_stale"] = _cleanup_stale_flowers(draft, kept_ids)
        summary["status"] = "written"
    elif summary["no_text_segment"]:
        summary["status"] = "no_text_segment"
    else:
        summary["status"] = "not_applicable"
    return warnings, summary
