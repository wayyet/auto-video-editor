"""delivery_qa_repair / delivery_qa_failed — 交付 QA 阻断路由的两个节点(阶段 6)。

计划 §8 阶段 6 任务 4:「将 QA 的 passed=false 接入 LangGraph 条件边;按错误
类型进入定向修复,无法修复则终止为 failed,不允许静默 END。」

- :func:`delivery_qa_repair` —— **定向修复**:对"可自动重试"的 required 类别
  (转场/字幕动画/贴纸/音量淡入淡出)重跑对应尾链节点函数。阶段 3-5 的幂等
  三件套(确定性 id + 重放剥我方旧 refs + 清孤儿)保证重放安全;修复后回到
  ``join_before_delivery`` 复检(条件边路由)。字幕/配音/BGM/草稿结构性缺失
  属数据性缺陷,重跑无意义 → 不在分派表内,直接走失败终止。
- :func:`delivery_qa_failed` —— **失败终止**:raise RuntimeError,
  ``monitoring/workflow_runner.run_with_events`` 捕获图执行异常后发
  ``run.failed``,运行状态置 failed(对照 ``escalate_guardrail_failure`` 的
  "写 error 后连 END"——那仍会被创建台调和为 completed,不满足
  「不静默 END」的验收标准)。

图接线(graph.py)::

    join_before_delivery → route_after_join_qa
        gate 未失败            → export_final_video
        gate 失败 且未修复过    → delivery_qa_repair → join_before_delivery(回边复检)
        gate 失败 且已修复过    → delivery_qa_failed → END(raise → run.failed)
"""

from __future__ import annotations

from typing import Any, Callable

from draft_ops.draft_integrity import (
    REQUIRED_FEATURE_KEYS,
    CAT_DEGRADED,
    CAT_FAILED,
    resolve_required_features,
)
from state import WorkflowState

#: 修复上限:1 次(repair → join 复检一轮;再失败即终止,不与上游降级缠斗)
DELIVERY_QA_MAX_REPAIRS = 1


def _build_dispatch() -> dict[str, Callable[[WorkflowState], dict]]:
    """required 类别 → 幂等修复动作(重跑尾链节点函数,副作用自落盘)。

    未列出的类别(subtitles_*/voiceover_*/bgm)是数据性缺失,不可自动修复。
    """
    """延迟 import 尾链节点函数(避免 nodes 包内初始化顺序依赖)。"""
    from nodes.node_09_inject_fx import inject_fx
    from nodes.node_10_inject_text_fx import inject_text_fx, inject_text_fx_en
    from nodes.node_11_inject_sticker import inject_sticker
    from nodes.node_13_adjust_volume import adjust_volume

    def _text_animation(state: WorkflowState) -> dict:
        # 动画对两份草稿各自写入;英文分支未生成时 10b 内部自带降级跳过
        inject_text_fx(state)
        inject_text_fx_en(state)
        return {}

    return {
        "transitions": inject_fx,
        "text_animation": _text_animation,
        "stickers": inject_sticker,
        "volume_fades": adjust_volume,
    }


def _blocked_categories(state: WorkflowState) -> list[str]:
    """处于 failed/degraded 的 required 类别(gate 阻断清单)。"""
    report = state.get("join_qa_report") or {}
    categories = report.get("categories") or {}
    required = report.get("required_features") or resolve_required_features(state)
    blocked: list[str] = []
    for key in REQUIRED_FEATURE_KEYS:
        if not required.get(key):
            continue
        status = str((categories.get(key) or {}).get("status") or "")
        if status in (CAT_FAILED, CAT_DEGRADED):
            blocked.append(key)
    return blocked


def _emit_qa_event(event: str, *, node_id: str, detail: dict) -> None:
    """经 LangGraph custom stream 上报 delivery.qa_* 观测事件(与 join 同款)。"""
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if callable(writer):
            writer(
                {
                    "monitor_event": "progress",
                    "node_id": node_id,
                    "detail": {"event": event, **detail},
                }
            )
    except Exception:  # noqa: BLE001 — 观测通道失败不阻断工作流
        pass


def delivery_qa_repair(state: WorkflowState) -> dict:
    """LangGraph 节点:按阻断类别分派幂等修复,写 retry 计数后回 join 复检。"""
    blocked = _blocked_categories(state)
    retried: list[str] = []
    unrepairable: list[str] = []
    if blocked:
        dispatch = _build_dispatch()
        for key in blocked:
            action = dispatch.get(key)
            if action is None:
                unrepairable.append(key)
                continue
            action(state)  # 幂等重放;节点自身的 status_log 标签不并入(路由不受影响)
            retried.append(key)

    tags = ["delivery_qa_repair_done"]
    if retried:
        tags.append(f"delivery_qa_repaired:{','.join(retried)}")
    if unrepairable:
        tags.append(f"delivery_qa_unrepairable:{','.join(unrepairable)}")
    _emit_qa_event(
        "delivery.qa_repair",
        node_id="delivery_qa_repair",
        detail={"retried": retried, "unrepairable": unrepairable},
    )
    return {
        "delivery_qa_retry_count": DELIVERY_QA_MAX_REPAIRS,
        "status_log": tags,
    }


def delivery_qa_failed(state: WorkflowState) -> dict:
    """LangGraph 节点:required 特性复检仍失败 → 终止运行(run.failed)。

    raise 前先上报 ``delivery.qa_failed`` 事件(创建台交付卡显示失败与
    阻断类别);异常由 ``run_with_events`` 转成 ``run.failed``。
    """
    report = state.get("join_qa_report") or {}
    categories = report.get("categories") or {}
    required = report.get("required_features") or resolve_required_features(state)
    blocked = _blocked_categories(state)
    summary = {
        key: {
            "status": str((categories.get(key) or {}).get("status") or "?"),
            "issues": [
                str(i.get("detail") or "")
                for i in ((categories.get(key) or {}).get("issues") or [])
                if isinstance(i, dict) and i.get("severity") == "error"
            ][:5],
        }
        for key in blocked
    }
    _emit_qa_event(
        "delivery.qa_failed",
        node_id="delivery_qa_failed",
        detail={
            "blocked": blocked,
            "required_features": {k: bool(required.get(k)) for k in REQUIRED_FEATURE_KEYS},
            "categories": summary,
            "sha256": report.get("sha256") or {},
        },
    )
    detail_text = "; ".join(f"{k}={v['status']}" for k, v in summary.items()) or "未知类别"
    raise RuntimeError(f"[delivery_qa] 必需功能未通过,交付阻断(复检仍失败): {detail_text}")


__all__ = [
    "DELIVERY_QA_MAX_REPAIRS",
    "delivery_qa_failed",
    "delivery_qa_repair",
]
