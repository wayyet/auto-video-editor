"""Phase 4/5 C 类旁支节点:storyline_generate_ai_transition(plan_v4 §5 阶段 5 / ADR-005)。

条件触发:``STORYLINE_ENABLE_AI_TRANSITION=1`` 才走真实路径,否则 graph 边直接
跳过(plan §2.3 / ADR-005)。本节点壳子直接读 ADR-005 flag;flag 关闭时 noop
+ append status_log,不开销;flag 开启时调 ``ai_transition_client`` 跑(目前
仍是 stub,后续 Real 客户端接 torch 模型权重)。

输入(flag 开启时):
- ``storyline_shots_artifact`` (回切镜头参考)
- ``storyline_groups_artifact`` (group 边界)
- ``storyline_transition_plan`` (上游推荐转场,仅参考)

输出(flag 开启时):
- ``storyline_ai_transition_artifact`` (JSON 路径)
- ``status_log`` append ``storyline_generate_ai_transition_done``
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.ai_transition_client import (
    is_ai_transition_enabled,
    get_default_ai_transition_client,
)
from nodes.storyline._common import append_status_tag, _resolve_outputs_root

logger = logging.getLogger(__name__)


def storyline_generate_ai_transition_node(state: WorkflowState) -> dict:
    if not is_ai_transition_enabled():
        # 默认 noop — plan §5 阶段 5 决策 "默认不实现"
        return {
            "status_log": append_status_tag(
                state, "storyline_generate_ai_transition_skipped"
            ),
        }

    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 启用时:基于 transition_plan.items 逐对生成 AI 转场;stub 模式写空 JSON
    transition_plan = state.get("storyline_transition_plan") or {}
    items = (
        list(transition_plan.get("items") or [])
        if isinstance(transition_plan, dict)
        else []
    )

    client = get_default_ai_transition_client()
    results: list[dict] = []
    for i, it in enumerate(items):
        try:
            r = client.generate(
                from_clip={"clip_id": it.get("group_id") or f"from_{i}"},
                to_clip={"clip_id": it.get("group_id") or f"to_{i}"},
                output_dir=out_dir,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ai_transition generate exception: {e!r}")
            continue
        results.append(
            {
                "from_clip_id": r.from_clip_id,
                "to_clip_id": r.to_clip_id,
                "duration_ms": r.duration_ms,
                "provider": r.provider,
                "error": r.error,
                "artifact": str(r.artifact_path),
            }
        )

    out_path = out_dir / "ai_transition.json"
    out_path.write_text(
        json.dumps(
            {"items": results, "provider": "stub"},
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    return {
        "storyline_ai_transition_artifact": str(out_path),
        "status_log": append_status_tag(
            state,
            f"storyline_generate_ai_transition_done:items={len(results)}",
        ),
    }