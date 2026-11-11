"""join_before_delivery — 中文主线 + 英文分支汇合节点(Week 4 新增,对照计划 §4.6)。

职责:交付 QA 闸门。2026-11-09 阶段 6(效果缺失修复)起升级为
**required-feature gate**:按 9 类必需功能逐类给出 ``passed/failed/degraded/
not_applicable`` 判定,required 类别处于 failed/degraded 时写入
``join_qa_report.gate_failed=True`` —— 阻断路由由 graph.py 的条件边
(:func:`graph.route_after_join_qa`)完成:可自动重试的类别进
``delivery_qa_repair`` 重跑幂等修复节点,无法修复或复检仍失败则
``delivery_qa_failed`` 终止为 run.failed,**不允许静默 END**。

本节点自身保持"记录不抛":直接调用本函数的存量测试与旧 checkpoint 行为
稳定(阻断只发生在图路由层)。

位置(2026-10 调序后):尾链 node_09 → 11 → 12 → 13 的**最后一站**,单前驱。
两条分支的汇合屏障已前移到 ``node_09_inject_fx``(``add_edge([node_15, node_17], node_09)``);
本节点只检查"交付物是否齐全",此时尾链(含 BGM/音量)已全部完成。

K2=B 后新增英文草稿检查(计划 §7 U1,本实现定下的检查项):

============================  ==========================================
检查项                        判定
============================  ==========================================
英文草稿存在性                ``draft_dir_en_branch`` 有值但文件不存在 → issue
转场/特效条数一致              中英两份草稿 ``transitions`` / ``video_effects``
                              条数必须相等且 >0(尾链 09 对两份都生效)
贴纸条数一致                  两份草稿 ``stickers`` 条数必须相等且 >0(尾链 11)
BGM 轨存在                    英文草稿有 audio 轨时,``materials.audios`` 必须
                              引用得到(尾链 12 镜像)
音量已写入                    阶段 5 起改判 BGM 段的 ``audio_fades`` 绑定
                              (段级 volume 天然 1.0 不可判"未写入")
============================  ==========================================

英文草稿从未生成(``draft_dir_en_branch`` 为空,例如 fork 早退)时**整段跳过**,
不当成问题 —— 那是上游降级,不是交付缺陷。

2026-11-07 阶段 4(人工关卡与剪映草稿导入):尾链四项检查扩到中英两份;
深度 QA 委托 :mod:`draft_ops.draft_integrity`;delivery.qa_* 事件供创建台展示。

2026-11-09 阶段 6(效果缺失修复):尾链/深度/交付物检查全部结构化(带
``category``),经 :func:`draft_ops.draft_integrity.build_qa_categories` 聚合为
逐类判定并新增"有旁白但字幕未写入"等类别专属判据;报告顶层增加
``sha256``(回读哈希)/ ``required_features`` 回显 / ``gate_failed``;
``error_texts`` 字符串视图与阶段 4 的 ``join_qa_issues`` 契约逐字一致
(2026-11-12 阶段5批3:顶层 state 字段 ``join_qa_issues`` 因零消费者移除,
字符串清单并入结构化报告)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from draft_ops.draft_integrity import (
    CATEGORY_KEYS,
    DEEP_CHECK_CATEGORY,
    SEV_ERROR,
    build_join_qa_report,
    build_qa_categories,
    issue_text,
    resolve_required_features,
)
from nodes._dual_draft import dual_draft_targets, en_draft_file_missing_path
from nodes.node_13_adjust_volume import is_voiceover_track
from state import WorkflowState

#: delivery.qa 事件里 issue 明细的条数上限(事件载荷不无限膨胀;全量在 state 里)
_EVENT_ISSUE_CAP = 20


def _count(materials: dict, key: str) -> int:
    return len(materials.get(key, []) or [])


def _tail_chain_structured(draft: dict, draft_tag: str, label: str) -> list[dict[str, Any]]:
    """对一份草稿做尾链四项检查,返回结构化 issue(带 category)。

    ``label`` 取 ``"英文草稿"`` / ``"中文主草稿"``;英文侧的 issue 文本与
    K2=B 原实现逐字一致(单测契约),中文侧同构生成。``detail`` 即原文本,
    ``error_texts`` 字符串视图由调用方用 ``detail`` 原文保持。
    """
    structured: list[dict[str, Any]] = []

    def _add(check: str, detail: str, category: str) -> None:
        structured.append(
            {"check": check, "severity": SEV_ERROR, "draft": draft_tag, "detail": detail, "category": category}
        )

    materials = draft.get("materials", {}) or {}

    transitions = _count(materials, "transitions")
    video_effects = _count(materials, "video_effects")
    stickers = _count(materials, "stickers")
    if transitions == 0:
        _add("转场", f"{label}转场缺失(尾链 node_09 未对{label}生效?)", "transitions")
    if video_effects == 0:
        _add("视频特效", f"{label}视频特效缺失(尾链 node_09 未对{label}生效?)", "video_effects")
    if stickers == 0:
        _add("贴纸", f"{label}贴纸缺失(尾链 node_11 未对{label}生效?)", "stickers")

    # 阶段 4:贴纸必须真实挂到视频段(material 在、引用悬空 = 挂载失败;
    # 根因分析 §D——不能只数 materials.stickers 数量)。
    if stickers:
        bound_refs = {
            str(r)
            for track in (draft.get("tracks", []) or []) if track.get("type") == "video"
            for seg in (track.get("segments", []) or [])
            for r in (seg.get("extra_material_refs") or [])
        }
        unbound = [
            str(m.get("id"))
            for m in (materials.get("stickers") or [])
            if str(m.get("id")) not in bound_refs
        ]
        if unbound:
            _add(
                "贴纸绑定",
                f"{label}贴纸未挂到任何视频段({len(unbound)} 个:node_11 挂载失败?)",
                "stickers",
            )

    # 阶段 4:我方字幕(text-zh-/text-en-)应有原生入场动画绑定
    # (material_animations 素材 + text 轨段 extra_material_refs)。
    our_text_ids = [
        str(m.get("id"))
        for m in (materials.get("texts") or [])
        if str(m.get("id")).startswith(("text-zh-", "text-en-"))
    ]
    if our_text_ids:
        text_segments = {
            str(seg.get("material_id")): seg
            for track in (draft.get("tracks", []) or []) if track.get("type") == "text"
            for seg in (track.get("segments", []) or [])
        }
        anim_bucket_ids = {
            str(a.get("id")) for a in (materials.get("material_animations") or [])
        }
        missing_anim = []
        for tid in our_text_ids:
            seg = text_segments.get(tid)
            refs = {str(r) for r in ((seg or {}).get("extra_material_refs") or [])}
            if not (refs & anim_bucket_ids):
                missing_anim.append(tid)
        if missing_anim:
            _add(
                "字幕动画",
                f"{label}我方字幕缺入场动画绑定({len(missing_anim)} 条:node_10 降级或未生效?)",
                "text_animation",
            )

    # BGM 轨 + 淡入淡出绑定:草稿**可能**没有音轨(关卡② 未加 / 镜像失败),那时不报;
    # 有 BGM 轨(非配音轨)却没有 fade 绑定,才是 node_13 该写没写
    # (阶段 5:音量在段级 volume 上,人工/原生段天然带 1.0,不可判"未写入";
    # fade 只由 node_13 写,绑定与否是可判据)。
    audio_tracks = [t for t in (draft.get("tracks", []) or []) if t.get("type") == "audio"]
    if audio_tracks:
        # 匹配键双口径:原生草稿用 segment.material_id 引用素材;旧约定
        # segment.id == material.id(同 node_12/node_13,兼容两者)。
        seg_ids = {
            seg.get(key)
            for track in audio_tracks
            for seg in (track.get("segments", []) or [])
            for key in ("material_id", "id")
            if seg.get(key)
        }
        audios = [m for m in (materials.get("audios", []) or []) if m.get("id") in seg_ids]
        if seg_ids and not audios:
            _add("BGM音轨", f"{label}音轨引用的 audio material 缺失(BGM 镜像不完整?)", "bgm")
        else:
            bgm_segments = [
                seg
                for track in audio_tracks
                if not is_voiceover_track(track)
                for seg in (track.get("segments", []) or [])
            ]
            fade_ids = {
                str(m.get("id")) for m in (materials.get("audio_fades") or [])
            }
            fade_bound = any(
                str(r) in fade_ids
                for seg in bgm_segments
                for r in (seg.get("extra_material_refs") or [])
            )
            if bgm_segments and not fade_bound:
                _add(
                    "BGM淡入淡出",
                    f"{label}BGM 淡入淡出未绑定(尾链 node_13 未对{label}生效?)",
                    "volume_fades",
                )

    return structured


def _load_draft_dict(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """读草稿 JSON;返回 ``(dict, None)`` 或 ``(None, 错误文本)``。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return None, str(exc)


def _categorize_deep_issue(issue: dict[str, Any]) -> str:
    """深度 QA issue → category 键(draft=zh/en 细分字幕类)。"""
    cat = DEEP_CHECK_CATEGORY.get(str(issue.get("check") or ""), "draft_integrity")
    if cat == "subtitles":
        return "subtitles_en" if issue.get("draft") == "en" else "subtitles_zh"
    return cat


def _emit_qa_event(event: str, *, detail: dict) -> None:
    """经 LangGraph custom stream 上报 delivery.qa_* 观测事件(与 node_05 同款)。

    图运行时外调用 ``get_stream_writer()`` 会 RuntimeError —— 观测事件绝不影响
    主流程,任何异常吞掉即可。
    """
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if callable(writer):
            writer(
                {
                    "monitor_event": "progress",
                    "node_id": "join_before_delivery",
                    "detail": {"event": event, **detail},
                }
            )
    except Exception:  # noqa: BLE001 — 观测通道失败不阻断工作流
        pass


def _categories_digest(categories: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """categories → 事件载荷摘要(状态+计数;issue 全量走 state/明细列)。"""
    digest: dict[str, dict[str, Any]] = {}
    for key in CATEGORY_KEYS:
        cat = categories.get(key) or {}
        entry: dict[str, Any] = {"status": cat.get("status")}
        if cat.get("required"):
            entry["required"] = True
        if cat.get("counts"):
            entry["counts"] = cat["counts"]
        if cat.get("note"):
            entry["note"] = cat["note"]
        digest[key] = entry
    return digest


def join_before_delivery(state: WorkflowState) -> dict:
    """LangGraph 节点(交付前 QA 闸门):检查中英两份草稿的交付物是否齐全。

    只返回变更字段(``join_qa_report`` + ``error_log`` + ``status_log``),
    避免覆盖上游未变字段。

    K2=B(2026-10):英文草稿存在时额外做尾链四项检查(转场/特效/贴纸/BGM 音量)。
    英文草稿从未生成时整段跳过,不产生 issues。
    阶段 4(2026-11-07):尾链检查扩到中英两份;深度 QA 结构化进
    ``join_qa_report``;delivery.qa_* 事件供创建台展示。
    阶段 6(2026-11-09):逐类判定(categories)+ required-feature gate
    (``gate_failed`` 供条件边阻断路由,本节点不抛)。
    """
    issues: list[str] = []
    structured: list[dict[str, Any]] = []  # 结构化明细(事件 + 分类聚合用)

    def _add(check: str, detail: str, *, category: str, draft: str = "-") -> None:
        structured.append(
            {"check": check, "severity": SEV_ERROR, "draft": draft, "detail": detail, "category": category}
        )
        issues.append(detail)

    covers = state.get("covers") or []
    if len(covers) < 3:
        _add("封面", f"封面缺失:需3种比例,实际 {len(covers)} 种", category="covers")
    else:
        for c in covers:
            if c.get("en_path") is None:
                _add("封面", f"封面英文版缺失:ratio={c.get('ratio', 'unknown')}", category="covers")

    if not state.get("subtitle_srt_path"):
        _add("英文字幕文件", "英文字幕文件缺失", category="srt")

    # ---- 阶段 4:双语草稿深度 QA(draft_integrity)----
    # 口径与 K2=B 一致:英文分支从未生成 → 整段跳过(上游降级,不是交付缺陷)。
    qa_report = build_join_qa_report(state)
    zh_checked = False
    zh_dict: dict[str, Any] | None = None
    en_dict: dict[str, Any] | None = None
    if not qa_report.get("skipped"):
        targets = dict(dual_draft_targets(state))
        if "zh" not in targets:
            _add("目录", f"中文主草稿缺失: {state.get('draft_path')}", category="draft_integrity")
        else:
            zh_dict, zh_err = _load_draft_dict(Path(targets["zh"]))
            if zh_err is not None:
                _add("目录", f"中文主草稿不可读: {zh_err}", category="draft_integrity")
            elif zh_dict is not None:
                zh_checked = True
                for it in _tail_chain_structured(zh_dict, "zh", "中文主草稿"):
                    structured.append(it)
                    issues.append(it["detail"])

        en_file = targets.get("en")
        if en_file is None:
            _add("目录", f"英文分支草稿缺失: {en_draft_file_missing_path(state)}", category="draft_integrity")
        else:
            en_dict, en_err = _load_draft_dict(Path(en_file))
            if en_err is not None:
                _add("目录", f"英文分支草稿不可读: {en_err}", category="draft_integrity")
            elif en_dict is not None:
                for it in _tail_chain_structured(en_dict, "en", "英文草稿"):
                    structured.append(it)
                    issues.append(it["detail"])

    # 深度 QA 的 error 级 issue 并入 error_texts(报告内字符串视图);warning 只进报告。
    # 阶段 6:深度 issue 打 category 后交给 build_qa_categories 归桶。
    deep_structured: list[dict[str, Any]] = []
    for it in qa_report.get("issues", []):
        it = dict(it)
        it["category"] = _categorize_deep_issue(it)
        deep_structured.append(it)
        if it.get("severity") == SEV_ERROR:
            issues.append(issue_text(it))
            structured.append(it)

    # ---- 阶段 6:逐类判定 + required-feature gate ----
    categories, new_issues, gate_failed = build_qa_categories(state, zh_dict, en_dict, structured)
    for it in new_issues:
        if it.get("severity") == SEV_ERROR:
            issues.append(it["detail"])

    # 报告汇总:passed = 无任何 error(交付物 + 深度 QA + 类别判定);
    # gate_failed 只看 required 类别(未 required 的类别 failed 仅展示不阻断)。
    report_out: dict[str, Any] = {
        **qa_report,
        "passed": not issues,
        "error_texts": list(issues),
        "node_issues": [s["detail"] for s in structured if s["check"] in ("交付物", "封面", "英文字幕文件", "目录")],
        "zh_checked": zh_checked,
        "categories": categories,
        "required_features": resolve_required_features(state),
        "gate_failed": gate_failed,
    }

    event = "delivery.qa_passed" if report_out["passed"] else "delivery.qa_issues"
    _emit_qa_event(
        event,
        detail={
            "passed": report_out["passed"],
            "skipped": bool(qa_report.get("skipped")),
            "gate_failed": gate_failed,
            "issue_count": len(issues),
            "issues": structured[:_EVENT_ISSUE_CAP],
            "categories": _categories_digest(categories),
            "sha256": qa_report.get("sha256") or {},
            "zh_draft_dir": str(Path(state["draft_path"]).parent) if state.get("draft_path") else None,
            "en_draft_dir": state.get("draft_dir_en_branch"),
        },
    )

    new_errors: list[str] = [f"[join] {it}" for it in issues]
    status_tags = [
        "join_before_delivery_done",
        "delivery_qa_ok" if report_out["passed"] else f"delivery_qa_issues:{len(issues)}",
    ]
    if gate_failed:
        status_tags.append("delivery_qa_gate_failed")
    return {
        "join_qa_report": report_out,
        "error_log": new_errors,
        "status_log": status_tags,
    }


__all__ = ["join_before_delivery"]
