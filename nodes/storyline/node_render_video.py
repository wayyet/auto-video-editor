"""Phase 4/5/6 旁支节点:storyline_render_video(plan_v4 §5 阶段 6 / ADR-006)。

``STORYLINE_ENABLE_RENDER_SMOKE_TEST=1`` 才跑真实渲染。默认关闭,直接 noop 返回
state,append ``storyline_render_video_skipped`` status_log(plan §5 阶段 6)。

输入(flag 开启时):
- ``storyline_timeline_plan``: 已校验的 CanonicalTimeline
- ``storyline_outputs_root``: 写 mp4 的目录

输出(flag 开启时):
- ``storyline_render_smoke_test_path``: mp4 路径(不接入下游)
- ``status_log`` append ``storyline_render_video_done``(或 `_skipped`)

设计纪律(plan §5 阶段 6):
1. 默认 noop 路径**不**写 ``storyline_render_smoke_test_path`` 字段,避免
   干扰下游(plan §2.2 "render_video 标注 STORYLINE_ENABLE_RENDER_SMOKE_TEST=1
   才走")。
2. flag 开启时:本阶段 6 仍写 stub mp4 占位(主 venv 不能装 ffmpeg/libav) +
   把 ``storyline_render_smoke_test_path`` 字段写进 state,但不接入下游
   任何节点(对照 graph.py 第 627 行注释)。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from config import STORYLINE_ENABLE_RENDER_SMOKE_TEST
from state import WorkflowState
from nodes.storyline._common import (
    _resolve_outputs_root,
    append_status_tag,
)

logger = logging.getLogger(__name__)


def storyline_render_video_node(state: WorkflowState) -> dict:
    if not STORYLINE_ENABLE_RENDER_SMOKE_TEST:
        # 默认 noop:append status_log 即可,plan §5 阶段 6 强调"不接入下游"
        return {
            **state,
            "status_log": append_status_tag(
                state, "storyline_render_video_skipped"
            ),
        }

    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    # flag 开启时:写最小 mp4 占位 JSON(真实 ffmpeg 后续阶段才接)
    ts_ms = int(time.time() * 1000)
    artifact = out_dir / f"render_smoke_{ts_ms}.json"
    timeline_path = state.get("storyline_timeline_plan") or ""
    artifact.write_text(
        json.dumps(
            {
                "timeline_plan": str(timeline_path),
                "rendered_at_ms": ts_ms,
                "provider": "stub",
                "note": "smoke test placeholder; main venv cannot run ffmpeg.",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        **state,
        "storyline_render_smoke_test_path": str(artifact),
        "status_log": append_status_tag(
            state, "storyline_render_video_done"
        ),
    }