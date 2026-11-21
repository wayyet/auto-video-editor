"""节点 11:inject_sticker — 贴纸计划发射 + SDK 编译汇合入口(对应原文档 4.6 节)。

2026-11-22 SDK 链转正(六段 legacy 链退役):

- **计划发射**:字幕来源 = spec sidecar ``subtitles`` 段(legacy 的
  ``materials.texts`` 扫描随原生文本轨写入退役);关键词命中经
  ``jy_common.sticker_resolver.resolve_sticker_resource_id``;贴纸 id 确定性
  (``sticker-{字幕 id}``)→ 重放幂等。时间范围取该字幕的 spec 起止
  (SDK 编译落**独立 sticker 轨**,不需要视频段绑定)。
- **fail-closed 不变**:``sticker_template.json`` 仍为占位
  (``_reverse_engineering_pending: true``,资源值 ``PLACEHOLDER_*``)。
  占位/非 19 位 resource_id 一律不进 spec,计 ``degraded_placeholder`` 并给出
  回填指引 —— 真实 ID 由操作员在剪映 5.9.0.11632 手工创建贴纸样本 diff 回填
  (步骤见模板 ``_reverse_engineering_steps``);回填后本节点自动转为正常发射。
- **SDK 编译汇合**:本节点是唯一编译写入入口 —— 发射 spec 后以当前草稿+全量
  spec 调 ``compile_and_publish_draft`` SDK 重建草稿。编译失败/被人工高级特征
  阻断 → fail-closed 记录,草稿保留编译前快照(safe_write_draft 快照回退兜底);
  **禁止静默回退 legacy**(设计文档 §13.2;legacy 写入链已退役)。
- **显式取文本**:关键词匹配用 spec 字幕的渲染文本(wrap_content 优先,纯文本
  兜底),与编译器字幕文本口径一致。

摘要 ``sticker_summary`` 进 state(交付 QA stickers 类别消费 degraded 计数)。
"""

from __future__ import annotations

import json
from pathlib import Path

from jy_common.native_fx import is_19_digit_resource_id
from jy_common.sticker_resolver import resolve_sticker_resource_id
from nodes._dual_draft import ZH, dual_draft_targets, missing_en_note
from state import WorkflowState


def _sticker_id_for(subtitle_id: str, index: int) -> str:
    """贴纸 id 由字幕 id 派生(确定性),重放得到同一个 id → spec 幂等。

    字幕没有 id 时退回序号兜底(spec 字幕一定带 id,兜底只防异常数据)。
    """
    return f"sticker-{subtitle_id}" if subtitle_id else f"sticker-idx-{index}"


def _plan_stickers_for_spec(
    spec_subtitles: list,
    tag: str,
) -> tuple[list[dict], list[str], dict]:
    """对 spec 字幕列表做关键词匹配与 fail-closed 校验(**纯计算**)。

    Returns:
        (stickers_plan, 告警, 摘要)。stickers_plan 条目含 StickerSpec 全字段。
    """
    from jianying_sdk.spec import StickerSpec

    warnings: list[str] = []
    summary: dict[str, int] = {
        "texts_scanned": 0,
        "matched": 0,
        "written": 0,
        "degraded_placeholder": 0,
        "no_segment": 0,
    }

    plan: list[StickerSpec] = []
    for index, sub in enumerate(spec_subtitles):
        # 开场大字标题(hook-title-*)不被贴贴纸(第四期计划 §3.2 对位表 #8)。
        # spec.subtitles 正常只含 node_08/08b 字幕;此过滤为防御性(规格来源异常时也不贴)。
        if str(getattr(sub, "subtitle_id", "") or "").startswith("hook-title-"):
            continue
        summary["texts_scanned"] += 1
        display_text = str(getattr(sub, "wrap_content", None) or sub.text or "")
        resource_id = resolve_sticker_resource_id(display_text)
        if not resource_id:
            continue
        summary["matched"] += 1

        if not is_19_digit_resource_id(resource_id):
            # fail-closed:占位/无效资源不进 spec(回填指引见模块 docstring)
            summary["degraded_placeholder"] += 1
            warnings.append(
                f"[{tag}] 贴纸资源未回填真实 ID({resource_id!r}),"
                "degraded 不写入 —— 请按 templates/sticker_template.json"
                " 的 _reverse_engineering_steps 实机回填"
            )
            continue

        subtitle_id = str(getattr(sub, "subtitle_id", "") or "")
        sticker_id = _sticker_id_for(subtitle_id, index)
        plan.append(StickerSpec(
            sticker_id=sticker_id,
            resource_id=str(resource_id),
            start_us=int(getattr(sub, "start_us", 0) or 0),
            duration_us=int(getattr(sub, "end_us", 0) or 0) - int(getattr(sub, "start_us", 0) or 0),
            source_subtitle_id=subtitle_id or None,
        ))
        summary["written"] += 1
    return plan, warnings, summary


def _emit_sticker_spec(draft_dir: Path, tag: str) -> tuple[dict, list[str]]:
    """贴纸计划 → DraftBuildSpec.stickers(SDK 编译数据源;失败 fail-closed)。

    Returns:
        (摘要, 告警)。摘要含 texts_scanned/matched/written/degraded_placeholder
        等计数,供 ``sticker_summary``/交付 QA 消费。
    """
    from nodes._draft_build_spec import load_build_spec, replace_spec_sections

    spec = load_build_spec(draft_dir)
    spec_subtitles = list(spec.subtitles or []) if spec else []
    plan, warnings, summary = _plan_stickers_for_spec(spec_subtitles, f"node_11_{tag}")
    replace_spec_sections(draft_dir, stickers=plan)
    return summary, warnings


def inject_sticker(state: WorkflowState) -> dict:
    """发射贴纸计划 → node_11 唯一编译入口以 SDK 重建中英两份草稿。

    K2=B(2026-10):对**中文主草稿 + 英文分支草稿**各发射/编译一次
    (见 ``nodes/_dual_draft.py``)。两份草稿在同一节点内顺序处理,不跨超步,
    返回值契约不变(整份 state)。
    """
    status_log = list(state.get("status_log", []) or [])
    error_log = list(state.get("error_log", []) or [])

    targets = dual_draft_targets(state)
    if not targets:
        raise FileNotFoundError(f"[node_11] state.draft_path 缺失,无法注入贴纸")

    has_en_dir = bool(state.get("draft_dir_en_branch"))
    sticker_summary: dict[str, dict] = {}
    canonical_plan = state.get("storyline_plan")
    for tag, draft_file in targets:
        draft_dir = Path(draft_file).parent
        # 贴纸计划发射(关键词匹配 + fail-closed 校验;summary 供 QA/审计)
        summary, warnings = _emit_sticker_spec(draft_dir, tag)
        status_log.append(f"[node_11] 贴纸计划已入 spec:{summary.get('written', 0)} 条")
        error_log.extend(warnings)
        sticker_summary[tag] = summary

        # SDK 编译汇合(唯一写入入口):以当前草稿 + 全量 spec 重建草稿
        compile_note = f"node_11_compile_{tag}"
        try:
            from jianying_sdk.pipeline import compile_and_publish_draft

            record = compile_and_publish_draft(
                draft_dir,
                Path(draft_file),
                job_id=str(state.get("session_id") or ""),
                canonical_plan=canonical_plan if isinstance(canonical_plan, dict) else None,
                audit_node=compile_note,
            )
        except Exception as exc:  # noqa: BLE001 - 管线异常兜底(不覆盖有效草稿)
            record = {"status": "failed", "note": repr(exc)}
        status_log.append(f"{compile_note}:{record.get('status')}")
        if record.get("status") == "failed":
            error_log.append(
                f"[node_11] SDK 编译失败({tag}),草稿保留编译前快照: {record.get('note')}"
            )
        elif record.get("status") == "failed_blocked_features":
            error_log.append(
                f"[node_11] SDK 编译被人工高级特征阻断({tag}),"
                "草稿保留编译前快照: 见 spec.timeline.blocked_features"
            )
        elif record.get("status") == "skipped_no_spec":
            status_log.append(f"{compile_note}_no_spec")
        elif record.get("status") == "compiled":
            status_log.append(
                f"{compile_note}_counts:{json.dumps(record.get('counts') or {}, ensure_ascii=False)}"
            )
        status_log.append(
            "node_11_inject_sticker_done" if tag == ZH else "node_11_inject_sticker_en_done"
        )

    if len(targets) == 1 and has_en_dir:
        error_log.append(missing_en_note(state))

    return {
        **state,
        "status_log": status_log,
        "error_log": error_log,
        "sticker_summary": sticker_summary,
    }
