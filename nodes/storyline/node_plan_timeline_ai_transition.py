"""Phase 4/5 C 类旁支节点:storyline_plan_timeline_ai_transition(plan_v4 §5 阶段 5 / ADR-005)。

2026-10 接入主图:紧跟 ``storyline_plan_timeline_pro`` 与 ``storyline_generate_ai_transition``,
之后进 ``storyline_qa_gate``(设计文档 §3.2:PTP / GAT → PTAT → qa 闸门)::

    storyline_plan_timeline_pro ─────────────┐
    storyline_generate_ai_transition ────────┴─▶ [屏障] ─▶ plan_timeline_ai_transition ─▶ [屏障] ─▶ qa_gate

``qa_gate`` 的屏障里用本节点**替换** ``plan_timeline_pro``,所以 qa 校验的是含 AI 转场的最终时间线。
开关 ``STORYLINE_ENABLE_AI_TRANSITION`` **建图时读取**(默认开),关闭时图里不接这两个节点。

2026-11-12 noop 治理阶段2b 物化(设计文档 §2.4)——本节点现在做三件事:

1. **物化**:对每个真实生成成功(``video_path`` 就位且 ``status=generated``)且两端
   在时间线上相邻的条目,把生成的 MP4 注册为 ``source_media`` 新条目,并在相邻两段
   之间插入一个合成转场 Clip(``source_in=0 / source_out=duration``、速度 1.0、
   ``ai_transition_pair`` 溯源)。``mapper`` 无需理解"抽象 AI 转场"——转场就是普通
   视频 clip,照常产出 ``materials.videos[]`` 与 ``tracks.video.segments[]``。
2. **时长预算吸收(不静默拉长成片)**:插入的总时长优先按比例从原片段目标跨度里
   压出(逐段下限 200ms,压缩只会提高速度,不会触碰 ``STORYLINE_MIN_CLIP_SPEED``
   下限);预算仍不够时从**最靠后**的转场开始放弃并留 warning,保证
   ``options.max_duration_ms`` 不被突破。
3. **审计**:全部条目(含降级/放弃的)照旧写进 ``ai_transitions`` 清单;
   ``options.enable_ai_transition`` 只在**实际物化**了至少一条转场时为 True。

任何一步失败都**保留基础时间线**(只写 error_log),主线不因旁支出错而断掉。

⚠️ 本节点在 qa 重试回路里,**每轮重跑一次**。状态标签必须固定(成功一律 ``_done``,
包括"没有可挂的转场";跳过一律 ``_skipped``),不能每轮都变 —— ``status_log`` 走
``_append_unique`` 去重,固定标签保持通道整洁(qa 路由已改用
``storyline_qa_route_signal`` 信号字段,2026-11-12 阶段1)。细节写 logger。

输入:
- ``storyline_timeline_plan``(基础时间线 JSON 路径)
- ``storyline_ai_transition_artifact``(``storyline_generate_ai_transition`` 的产物)

输出:
- ``storyline_timeline_plan``:成功物化/挂清单时指向 ``timeline_plan_ai.json``;其余情况保持不变
- ``status_log`` append ``storyline_plan_timeline_ai_transition_done`` / ``_skipped`` / ``_failed``
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from pathlib import Path

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

#: 转场片段语义标签
_TRANSITION_TAG = "ai_transition"
#: 预算吸收时单个原片段的最小目标跨度(毫秒);压缩不会把一段压得低于此值
_ABSORB_FLOOR_MS = 200


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


def _is_generated(it: dict) -> bool:
    """真实生成成功且产物在盘上的条目才可物化。"""
    return (
        (not bool(it.get("is_stub", True)))
        and str(it.get("status") or "") == "generated"
        and bool(it.get("video_path"))
        and Path(str(it["video_path"])).is_file()
        and int(it.get("duration_ms") or 0) > 0
    )


def _shrink(spans: list[int], need: int, floor_ms: int) -> tuple[list[int], int]:
    """从各段按可压缩余量比例共抽出 ``need`` 毫秒(逐段不低于 ``floor_ms``)。

    返回 ``(新跨度列表, 实际抽出量)``;余量不足时抽出能抽的部分。
    压缩只会让 speed=源/目标 变大,不会触碰最小速度下限。
    """
    new = list(spans)
    remaining = int(need)
    for _ in range(len(new) + 1):
        pool = sum(s - floor_ms for s in new if s > floor_ms)
        if pool <= 0 or remaining <= 0:
            break
        taken = 0
        for i, s in enumerate(new):
            room = s - floor_ms
            if room <= 0:
                continue
            cut = min(room, (room * remaining) // pool)
            new[i] -= cut
            taken += cut
        remaining -= taken
        if taken == 0:
            break
    return new, (int(need) - remaining)


def _materialize(
    base: dict, items: list[dict]
) -> tuple[dict | None, list[str]]:
    """把可物化条目插进时间线;返回 ``(新时间线|None, 警告)``。

    新时间线为 ``None`` 表示没有可物化条目(全部 generated 条目缺失产物/不相邻),
    调用方走审计-only 路径;警告无论是否物化都返回(节点层并进产物 warnings)。
    预算:``options.max_duration_ms``;无预算限制时不做吸收,直接顺序重排。
    """
    clips = [dict(c) for c in (base.get("clips") or []) if isinstance(c, dict)]
    if not clips:
        return None
    source_media = [
        dict(s) for s in (base.get("source_media") or []) if isinstance(s, dict)
    ]
    options = dict(base.get("options") or {})
    budget_ms = int(options.get("max_duration_ms") or 0) or None

    index = {str(c.get("clip_id") or ""): i for i, c in enumerate(clips)}
    warnings: list[str] = []

    # (插入位置 = from 片段下标, 条目);两端必须相邻,否则放弃该条
    candidates: list[tuple[int, dict]] = []
    for it in items:
        if not _is_generated(it):
            continue
        fi = index.get(str(it.get("from_clip_id") or ""))
        ti = index.get(str(it.get("to_clip_id") or ""))
        if fi is None or ti is None or ti != fi + 1:
            warnings.append(
                f"转场 {it.get('from_clip_id')}->{it.get('to_clip_id')} "
                "两端在最终时间线上不相邻,不物化(保留为审计条目)"
            )
            continue
        candidates.append((fi, it))
    candidates.sort(key=lambda pair: pair[0])
    if not candidates:
        return None, warnings

    spans = [int(c["timeline_out_ms"]) - int(c["timeline_in_ms"]) for c in clips]
    base_total = sum(spans)

    def _relay(k: int) -> tuple[int, list[int], int]:
        """取前 k 个转场 + 预算吸收;返回(插入总时长, 新原片段跨度, 实际吸收量)。"""
        kept_d = sum(int(it["duration_ms"]) for _, it in candidates[:k])
        if kept_d <= 0:
            return 0, list(spans), 0
        if budget_ms is None:
            return kept_d, list(spans), 0
        removable = max(0, base_total - _ABSORB_FLOOR_MS * len(spans))
        need = min(kept_d, max(0, budget_ms - base_total) + kept_d)
        # 目标:base_total - absorbed + kept_d <= budget ⇔ absorbed >= base_total + kept_d - budget
        need = max(0, base_total + kept_d - budget_ms)
        need = min(need, removable)
        new_spans, absorbed = _shrink(spans, need, _ABSORB_FLOOR_MS)
        return kept_d, new_spans, absorbed

    # 预算装不下时从最靠后的转场开始放弃
    k = len(candidates)
    while k > 0:
        kept_d, new_spans, absorbed = _relay(k)
        total = base_total - absorbed + kept_d
        if budget_ms is None or total <= budget_ms:
            break
        dropped = candidates[k - 1][1]
        warnings.append(
            f"转场 {dropped.get('from_clip_id')}->{dropped.get('to_clip_id')} "
            f"因时长预算(max_duration_ms={budget_ms})被放弃,该衔接点退回硬切"
        )
        k -= 1
    kept_d, new_spans, absorbed = _relay(k)
    kept = [it for _, it in candidates[:k]]
    if not kept:
        return None, warnings

    # 顺序重排时间轴:原片段 + 插入的转场片段
    # 插入位置 = from 片段**之后**(即 to 片段之前),所以下标取 fi + 1
    insert_at: dict[int, list[dict]] = {}
    for pos, it in candidates[:k]:
        insert_at.setdefault(pos + 1, []).append(it)

    new_clips: list[dict] = []
    new_media: list[dict] = []
    cursor = 0
    seq = 0
    for i, clip in enumerate(clips):
        for it in insert_at.get(i, []):
            seq += 1
            duration = int(it["duration_ms"])
            media_src = (
                f"{it.get('video_path')}|{it.get('from_clip_id')}|{it.get('to_clip_id')}"
            )
            media_id = (
                "transition_media_"
                + hashlib.sha256(media_src.encode("utf-8")).hexdigest()[:8]
            )
            new_media.append(
                {
                    "media_id": media_id,
                    "file_uri": Path(str(it["video_path"])).resolve().as_uri(),
                    "duration_ms": duration,
                    "media_type": "video",
                    "width": it.get("width"),
                    "height": it.get("height"),
                    "fps": it.get("fps"),
                    "sha256": it.get("sha256"),
                }
            )
            new_clips.append(
                {
                    "clip_id": f"transition_{seq:04d}",
                    "source_media_id": media_id,
                    "source_in_ms": 0,
                    "source_out_ms": duration,
                    "timeline_in_ms": cursor,
                    "timeline_out_ms": cursor + duration,
                    "semantic_tags": [_TRANSITION_TAG],
                    "ai_transition_pair": (
                        f"{it.get('from_clip_id')}->{it.get('to_clip_id')}"
                    ),
                }
            )
            cursor += duration
        span = new_spans[i]
        clip = dict(clip)
        clip["timeline_in_ms"] = cursor
        clip["timeline_out_ms"] = cursor + span
        cursor += span
        new_clips.append(clip)

    merged = copy.deepcopy(base)
    merged["clips"] = new_clips
    merged["source_media"] = source_media + new_media
    # 审计清单保留全部条目(含未物化的);物化信息由 clips[].ai_transition_pair 承载
    merged["ai_transitions"] = items
    merged_options = dict(options)
    merged_options["enable_ai_transition"] = True
    merged["options"] = merged_options
    base_warnings = list(merged.get("warnings") or [])
    merged["warnings"] = base_warnings + warnings
    return merged, warnings


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

        # 阶段2b:真实生成条目物化为 SourceMedia + Clip;失败/放弃的条目只作审计
        merged, materialize_warnings = _materialize(base, items)
        if merged is not None:
            logger.info(
                "plan_timeline_ai_transition: 物化 %d 条 AI 转场(清单 %d 条,%d 条警告)",
                sum(
                    1
                    for c in merged.get("clips", [])
                    if isinstance(c, dict) and c.get("ai_transition_pair")
                ),
                len(items),
                len(materialize_warnings),
            )
        else:
            merged = copy.deepcopy(base)
            merged["ai_transitions"] = items
            merged_options = dict(merged.get("options") or {})
            # 没有片段真正被物化进时间线 → 不声称已挂真实转场
            # (degraded / skipped / 不相邻 / 产物丢失的条目都只作审计)
            merged_options["enable_ai_transition"] = False
            merged["options"] = merged_options
            if materialize_warnings:
                merged["warnings"] = list(merged.get("warnings") or []) + materialize_warnings
            logger.info(
                "plan_timeline_ai_transition: 无可物化条目,仅挂 %d 条审计清单", len(items)
            )

        # 严格校验:通不过就保留基础时间线(下面 except 分支)
        CanonicalTimeline.model_validate(merged)

        out_dir = _resolve_outputs_root(state) / "storyline"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "timeline_plan_ai.json"
        out_path.write_text(
            json.dumps(merged, ensure_ascii=False, default=str), encoding="utf-8"
        )
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
