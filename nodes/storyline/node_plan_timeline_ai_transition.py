"""Phase 4/5 C 类旁支节点:storyline_plan_timeline_ai_transition(plan_v4 §5 阶段 5 / ADR-005)。

2026-10 接入主图:紧跟 ``storyline_plan_timeline_pro`` 与 ``storyline_generate_ai_transition``,
之后进 ``storyline_qa_gate``(设计文档 §3.2:PTP / GAT → PTAT → qa 闸门)::

    storyline_plan_timeline_pro ─────────────┐
    storyline_generate_ai_transition ────────┴─▶ [屏障] ─▶ plan_timeline_ai_transition ─▶ [屏障] ─▶ qa_gate

``qa_gate`` 的屏障里用本节点**替换** ``plan_timeline_pro``,所以 qa 校验的是含 AI 转场的最终时间线。
开关 ``STORYLINE_ENABLE_AI_TRANSITION`` **建图时读取**(默认开),关闭时图里不接这两个节点。

与旧版的区别(旧版从没被执行过):
- **不再重算时间线**。旧版会再调一遍 ``plan_timeline_pro``,而且漏传 ``group_scripts``,
  会把中文字幕文本(``subtitles.zh``)和已回填的 ``transcript`` 丢掉。
  现在改为:读取本轮 ``plan_timeline_pro`` 写出的基础时间线(``storyline_timeline_plan``),
  只挂上 ``ai_transitions`` 清单,另存为 ``timeline_plan_ai.json``。
- 挂上之后用 ``CanonicalTimeline`` 严格校验;**任何失败都保留基础时间线**(只写 error_log),
  不会让主线因为旁支出错而断掉。
- ``options.enable_ai_transition`` 只在清单里有**真实**(非 stub)转场时才置 True。
- 不改 clips、不改时长。(⚠️ 真实客户端若会往时间线里插 clip,必须在这里重算时长,
  由 qa_gate 兜底校验。)

⚠️ 本节点在 qa 重试回路里,**每轮重跑一次**。状态标签必须固定(成功一律 ``_done``,
包括"没有可挂的转场";跳过一律 ``_skipped``),不能每轮都变 —— 原因见 ``graph.py`` 文档字符串
(qa 路由看 ``status_log[-1]``)。细节写 logger。

输入:
- ``storyline_timeline_plan``(基础时间线 JSON 路径)
- ``storyline_ai_transition_artifact``(``storyline_generate_ai_transition`` 的产物)

输出:
- ``storyline_timeline_plan``:成功挂上清单时指向 ``timeline_plan_ai.json``;其余情况保持不变
- ``status_log`` append ``storyline_plan_timeline_ai_transition_done`` /
  ``_skipped`` / ``_failed``
"""
from __future__ import annotations

import copy
import json
import logging

from state import WorkflowState
from storyline.contract import CanonicalTimeline, StorylineErrorCode
from storyline_capabilities.ai_transition_client import (
    is_ai_transition_enabled,
)
from nodes.storyline._common import (
    _read_json_artifact,
    append_status_tag,
    _resolve_outputs_root,
)

logger = logging.getLogger(__name__)


def _items_for_timeline(doc: object, clip_ids: set[str]) -> list[dict]:
    """取出清单里两端 clip 都真实存在于时间线的条目(引用闭合)。"""
    if not isinstance(doc, dict):
        return []
    out: list[dict] = []
    for it in doc.get("items") or []:
        if not isinstance(it, dict):
            continue
        if str(it.get("from_clip_id") or "") in clip_ids and str(
            it.get("to_clip_id") or ""
        ) in clip_ids:
            out.append(it)
    return out


def storyline_plan_timeline_ai_transition_node(state: WorkflowState) -> dict:
    if not is_ai_transition_enabled():
        return {
            "status_log": append_status_tag(
                state, "storyline_plan_timeline_ai_transition_skipped"
            ),
        }

    try:
        base = _read_json_artifact(state, "storyline_timeline_plan")
        if not isinstance(base, dict):
            # 没有基础时间线可挂 → 什么都不动,让下游按原有逻辑兜底
            logger.info("plan_timeline_ai_transition: 没有基础时间线,跳过")
            return {
                "status_log": append_status_tag(
                    state, "storyline_plan_timeline_ai_transition_skipped"
                ),
            }

        clip_ids = {
            str(c.get("clip_id") or "")
            for c in (base.get("clips") or [])
            if isinstance(c, dict)
        }
        items = _items_for_timeline(
            _read_json_artifact(state, "storyline_ai_transition_artifact"), clip_ids
        )
        if not items:
            # 没有可挂的转场(只有一个 group / 上游失败 / 全被过滤)→ 保留基础时间线
            logger.info("plan_timeline_ai_transition: 没有可挂的转场,保留基础时间线")
            return {
                "status_log": append_status_tag(
                    state, "storyline_plan_timeline_ai_transition_done"
                ),
            }

        merged = copy.deepcopy(base)
        merged["ai_transitions"] = items
        options = merged.get("options")
        if not isinstance(options, dict):
            options = {}
            merged["options"] = options
        options["enable_ai_transition"] = any(
            not bool(it.get("is_stub", True)) for it in items
        )
        # 严格校验:通不过就保留基础时间线(下面 except 分支)
        CanonicalTimeline.model_validate(merged)

        out_dir = _resolve_outputs_root(state) / "storyline"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "timeline_plan_ai.json"
        out_path.write_text(
            json.dumps(merged, ensure_ascii=False, default=str), encoding="utf-8"
        )
        logger.info("plan_timeline_ai_transition: 挂上 %d 条 AI 转场", len(items))
    except Exception as e:  # noqa: BLE001 — 旁支节点,绝不拖垮主线
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                "[storyline:plan_timeline_ai_transition] "
                f"{StorylineErrorCode.TOOL_EXECUTION_FAILED}: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_plan_timeline_ai_transition_failed"
            ),
        }

    return {
        "storyline_timeline_plan": str(out_path),
        "status_log": append_status_tag(
            state, "storyline_plan_timeline_ai_transition_done"
        ),
    }
