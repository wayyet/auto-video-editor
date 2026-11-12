"""节点 11:inject_sticker — 贴纸原生落地 + 占位资源 fail-closed(对应原文档 4.6 节)。

2026-11-09 阶段 4(效果缺失修复)重写:

- **fail-closed**:``sticker_template.json`` 仍是占位(``_reverse_engineering_pending:
  true``,资源值为 ``PLACEHOLDER_*``)。占位/非 19 位 resource_id **一律不落草稿**
  (占位 ID 会让剪映加载到无效素材),计 ``degraded`` 并给出回填指引 —— 真实 ID
  由操作员在剪映 5.9.0.11632 手工创建贴纸样本 diff 回填(步骤见模板
  ``_reverse_engineering_steps``);回填后本节点自动转为正常写入。
- **原生素材**:真实 ID 时经 ``jy_common/native_fx.build_native_sticker_material``
  构造原生 ``materials.stickers`` 条目(type:"sticker"),并按字幕时间范围挂到
  时间重叠最大的 video segment ``extra_material_refs``(Week 3 既有匹配逻辑)。
- **显式取文本**:字幕显示文本经 ``native_text.text_display_text``(原生 content
  JSON 优先,纯文本兜底),不再隐式依赖 content 字符串包含中文。
- **时间范围双口径**:优先字幕 text 轨段的原生 ``target_timerange``,私有键兜底。

幂等与守卫:贴纸 id 确定性(``sticker-{字幕 material id}``)→ 重放按 id 覆盖;
重放前清理未续用的我方贴纸(``sticker-`` 前缀)与 refs(人工贴纸是 uuid,不受影响)。
"""

from __future__ import annotations

from pathlib import Path

from jy_common.native_fx import (
    OUR_STICKER_PREFIX,
    build_native_sticker_material,
    is_19_digit_resource_id,
)
from jy_common.native_text import text_display_text
from jy_common.sticker_resolver import resolve_sticker_resource_id
from nodes._draft_io import (
    apply_and_write,
    find_text_segment_by_material_id,
    find_video_track,
    load_draft,
    upsert_by_id,
)
from nodes._dual_draft import ZH, dual_draft_targets, missing_en_note
from state import WorkflowState


def _segment_range(seg: dict) -> tuple[int, int]:
    """返回 ``[start, start+duration)``(微秒)。字段缺失时退化为 0。"""
    tr = seg.get("target_timerange", {}) or {}
    start = int(tr.get("start", 0))
    duration = int(tr.get("duration", 0))
    return start, start + duration


def _timerange_range(tr: dict) -> tuple[int, int]:
    start = int(tr.get("start", 0))
    duration = int(tr.get("duration", 0))
    return start, start + duration


def _overlap_us(a: tuple[int, int], b: tuple[int, int]) -> int:
    """两闭区间重叠长度(微秒),无重叠返回 0。"""
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return max(0, hi - lo)


def _find_overlapping_segment(
    segments: list[dict],
    timerange: dict,
) -> dict | None:
    """按 timerange 找时间重叠最大的 segment;无重叠返回 None。

    选择规则:
    - 任一像素级重叠(``> 0 us``)即认为匹配
    - 多段重叠时取交集面积最大的段
    - 无任何重叠段 → 返回 None(由调用方决定 fallback 策略)
    """
    if not segments:
        return None
    target_range = _timerange_range(timerange)
    best: tuple[int, dict] | None = None
    for seg in segments:
        ov = _overlap_us(target_range, _segment_range(seg))
        if ov > 0 and (best is None or ov > best[0]):
            best = (ov, seg)
    return best[1] if best else None


def _append_ref_once(refs: list, sticker_id: str) -> None:
    """往 segment 的 ``extra_material_refs``(剪映格式:字符串 id 列表)里补引用。

    重放时不重复追加(幂等)——不改列表类型,只判存在。
    """
    if sticker_id not in refs:
        refs.append(sticker_id)


def _sticker_id_for(text_obj: dict, index: int) -> str:
    """贴纸 id 由字幕 id 派生(确定性),重放得到同一个 id → 可按 id 覆盖,幂等。

    字幕没有 id 时退回序号兜底(``node_08`` / ``node_08b`` 产出的 text 一定带 id,
    兜底只防手工构造的草稿)。
    """
    text_id = text_obj.get("id")
    return f"{OUR_STICKER_PREFIX}{text_id}" if text_id else f"{OUR_STICKER_PREFIX}idx-{index}"


def _text_timerange(draft: dict, text_obj: dict) -> dict:
    """字幕时间范围:原生 text 轨段优先,私有 ``target_timerange`` 兜底。"""
    text_id = str(text_obj.get("id") or "")
    if text_id:
        seg = find_text_segment_by_material_id(draft, text_id)
        if seg is not None and seg.get("target_timerange"):
            return dict(seg["target_timerange"])
    return dict(text_obj.get("target_timerange", {}) or {})


def _cleanup_stale_stickers(draft: dict, kept_ids: set[str]) -> int:
    """清理未续用的我方贴纸(``sticker-`` 前缀素材与 refs),返回清理数。"""
    materials = draft.setdefault("materials", {})
    bucket = materials.setdefault("stickers", [])
    removed = 0
    kept: list[dict] = []
    for item in bucket:
        item_id = str(item.get("id") or "")
        if item_id.startswith(OUR_STICKER_PREFIX) and item_id not in kept_ids:
            removed += 1
            continue
        kept.append(item)
    bucket[:] = kept

    video_track = find_video_track(draft)
    for seg in video_track.get("segments", []) or []:
        refs = seg.get("extra_material_refs")
        if not isinstance(refs, list):
            continue
        seg["extra_material_refs"] = [
            r for r in refs
            if not (str(r).startswith(OUR_STICKER_PREFIX) and str(r) not in kept_ids)
        ]
    return removed


def _inject_sticker_into(draft: dict, tag: str) -> tuple[list[str], dict]:
    """把贴纸注入**一份**草稿(就地改 ``draft``),返回 (告警, 摘要)。

    中英两份草稿共用这一个实现。注意英文草稿的 ``materials.texts`` 是英文文本,
    贴纸关键词命中情况可能与中文不同 —— 属预期,不做跨语言关键词映射。
    """
    warnings: list[str] = []
    summary: dict[str, int] = {
        "texts_scanned": 0,
        "matched": 0,
        "written": 0,
        "degraded_placeholder": 0,
        "no_segment": 0,
        "cleaned_stale": 0,
    }

    materials = draft.setdefault("materials", {})
    stickers_material = materials.setdefault("stickers", [])
    video_track = find_video_track(draft)
    segments = video_track.get("segments", [])

    texts = materials.get("texts", []) or []
    kept_ids: set[str] = set()

    for index, text_obj in enumerate(texts):
        if str(text_obj.get("id") or "").startswith("hook-title-"):
            # 开场大字标题(node_09b 产出)不被贴贴纸(第四期计划 §3.2 对位表 #8)
            continue
        summary["texts_scanned"] += 1
        content = text_display_text(text_obj)
        resource_id = resolve_sticker_resource_id(content)
        if not resource_id:
            continue
        summary["matched"] += 1

        if not is_19_digit_resource_id(resource_id):
            # fail-closed:占位/无效资源不落草稿(回填指引见模块 docstring)
            summary["degraded_placeholder"] += 1
            warnings.append(
                f"[{tag}] 贴纸资源未回填真实 ID({resource_id!r}),"
                "degraded 不写入 —— 请按 templates/sticker_template.json"
                " 的 _reverse_engineering_steps 实机回填"
            )
            continue

        sticker_id = _sticker_id_for(text_obj, index)
        target_tr = _text_timerange(draft, text_obj)
        sticker_obj = build_native_sticker_material(
            material_id=sticker_id,
            resource_id=resource_id,
            duration_us=int(target_tr.get("duration", 0) or 0),
        )
        upsert_by_id(stickers_material, sticker_obj)
        kept_ids.add(sticker_id)

        target_seg = _find_overlapping_segment(segments, target_tr)
        if target_seg is None:
            # Week 3 兼容:无重叠时退回 segment[0] + error_log
            if segments:
                _append_ref_once(segments[0].setdefault("extra_material_refs", []), sticker_id)
                warnings.append(
                    f"[{tag}] 字幕 timerange {target_tr} 无重叠 segment,"
                    f"贴纸 {sticker_id} 退回 segment[0](Week 3 兼容)"
                )
        else:
            _append_ref_once(target_seg.setdefault("extra_material_refs", []), sticker_id)
        summary["written"] += 1

    summary["cleaned_stale"] = _cleanup_stale_stickers(draft, kept_ids)
    video_track["segments"] = segments
    return warnings, summary


def inject_sticker(state: WorkflowState) -> dict:
    """读取草稿 → 为每条字幕解析贴纸 resource_id → 原生注入/占位 fail-closed → 原子写回。

    K2=B(2026-10):对**中文主草稿 + 英文分支草稿**各注入一次(见 ``nodes/_dual_draft.py``)。
    两份草稿在同一节点内顺序处理,不跨超步,返回值契约不变(整份 state)。
    """
    status_log = list(state.get("status_log", []) or [])
    error_log = list(state.get("error_log", []) or [])

    targets = dual_draft_targets(state)
    if not targets:
        raise FileNotFoundError(f"[node_11] state.draft_path 缺失,无法注入贴纸")

    has_en_dir = bool(state.get("draft_dir_en_branch"))
    sticker_summary: dict[str, dict] = {}
    for tag, draft_file in targets:
        draft = load_draft(draft_file)
        warnings, summary = _inject_sticker_into(draft, f"node_11_{tag}")
        error_log.extend(warnings)
        sticker_summary[tag] = summary
        # Week 5:参数从 draft_path 提升为 draft_path.parent,safe_write_draft 双写
        status_log.extend(
            apply_and_write(
                draft_file,
                draft,
                "node_11",
                done_tag=(
                    "node_11_inject_sticker_done" if tag == ZH else "node_11_inject_sticker_en_done"
                ),
            )
        )

    if len(targets) == 1 and has_en_dir:
        error_log.append(missing_en_note(state))

    return {
        **state,
        "status_log": status_log,
        "error_log": error_log,
        "sticker_summary": sticker_summary,
    }
