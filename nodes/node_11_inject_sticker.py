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

import json
from pathlib import Path

import config

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


def _emit_sticker_spec(draft_dir: Path, draft: dict, log: list[str]) -> None:
    """贴纸计划 → DraftBuildSpec.stickers(第10期阶段6,SDK 编译数据源)。

    从**本轮注入后的草稿**自洽导出:我方 ``sticker-{text_id}`` 条目反解来源
    字幕,时间范围取该字幕的 target_timerange(与写入同源)。SDK 编译时落
    独立 sticker 轨(旧链路挂 video segment refs 是两种合法表达,编译器统一
    用 SDK 轨)。失败降级不阻断。
    """
    try:
        from jianying_sdk.spec import StickerSpec
        from nodes._draft_build_spec import replace_spec_sections

        materials = draft.get("materials") or {}
        texts_by_id = {
            str(t.get("id")): t
            for t in materials.get("texts") or []
            if isinstance(t, dict) and t.get("id")
        }
        stickers: list[StickerSpec] = []
        for entry in materials.get("stickers") or []:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("id") or "")
            if not entry_id.startswith(OUR_STICKER_PREFIX):
                continue  # 人工贴纸(id 是 uuid)不由 spec 管理,原样保留在草稿
            text_id = entry_id.removeprefix(OUR_STICKER_PREFIX)
            target_tr = _text_timerange(draft, texts_by_id.get(text_id) or {})
            stickers.append(StickerSpec(
                sticker_id=entry_id,
                resource_id=str(entry.get("resource_id") or ""),
                start_us=int(target_tr.get("start") or 0),
                duration_us=int(entry.get("duration") or 0),
                source_subtitle_id=text_id or None,
            ))
        replace_spec_sections(draft_dir, stickers=stickers)
        log.append(f"[node_11] 贴纸计划已入 spec:{len(stickers)} 条")
    except Exception as exc:  # noqa: BLE001 — spec 是增强路径,绝不阻断 node_11
        log.append(f"[node_11] 贴纸 spec 写入失败(不阻断): {exc!r}")


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
    # 第10期阶段7:SDK 编译后端(pyjianyingdraft)时,node_11 是唯一编译写入
    # 入口 —— legacy 注入先落盘 + spec 发射,再以当前草稿+全量 spec SDK 重建
    # (失败降级保留 legacy 产物,§11.1)。
    sdk_backend = str(getattr(config, "DRAFT_BUILDER_BACKEND", "legacy")) == "pyjianyingdraft"
    canonical_plan = state.get("storyline_plan")
    for tag, draft_file in targets:
        draft = load_draft(draft_file)
        warnings, summary = _inject_sticker_into(draft, f"node_11_{tag}")
        # 第10期阶段6:贴纸计划同步入 spec(双稿各自独立,SDK 编译数据源)
        _emit_sticker_spec(Path(draft_file).parent, draft, status_log)
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
        if sdk_backend:
            compile_note = f"node_11_compile_{tag}"
            try:
                from jianying_sdk.pipeline import compile_and_publish_draft

                record = compile_and_publish_draft(
                    Path(draft_file).parent,
                    Path(draft_file),
                    job_id=str(state.get("session_id") or ""),
                    canonical_plan=canonical_plan if isinstance(canonical_plan, dict) else None,
                    audit_node=compile_note,
                )
            except Exception as exc:  # noqa: BLE001 - 管线异常兜底(§11.1 不覆盖有效草稿)
                record = {"status": "failed", "note": repr(exc)}
            status_log.append(f"{compile_note}:{record.get('status')}")
            if record.get("status") == "failed":
                error_log.append(
                    f"[node_11] SDK 编译失败({tag}),已保留 legacy 草稿: {record.get('note')}"
                )
            elif record.get("status") == "failed_blocked_features":
                error_log.append(
                    f"[node_11] SDK 编译被人工高级特征阻断({tag}),"
                    "保留 legacy 草稿: 见 spec.timeline.blocked_features"
                )
            elif record.get("status") == "skipped_no_spec":
                status_log.append(f"{compile_note}_no_spec")
            elif record.get("status") == "compiled":
                status_log.append(
                    f"{compile_note}_counts:{json.dumps(record.get('counts') or {}, ensure_ascii=False)}"
                )

    if len(targets) == 1 and has_en_dir:
        error_log.append(missing_en_note(state))

    return {
        **state,
        "status_log": status_log,
        "error_log": error_log,
        "sticker_summary": sticker_summary,
    }
