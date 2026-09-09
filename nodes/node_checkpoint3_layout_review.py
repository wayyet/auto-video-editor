"""node_checkpoint3_layout_review — 关卡③(Week 5 拆分后)。

对齐第 5 周计划 §2.1 / §4.3:
- **只**做 ``interrupt({"checkpoint": "③", ...})``,不含任何副作用
  (翻译 / 写 marker 等)。天然幂等,resume 重放时不重复执行。
- resume 后:重读 en_branch 草稿(可能已被人工修正),写 SRT(让阶段五
  验收脚本拿到正确的英文音轨时间码)。
- **不**写 ``subtitle_segments_en`` — 因为 16a 已经写过,且 LangGraph 的
  LastValue 通道不允许同一 superstep 两个节点并发写同一 key。本节点只追加
  ``checkpoint3_triggered=True`` + SRT 路径 + status_log,语义上仍能保证
  ``subtitle_segments_en`` 最终值是正确的(16a 写一次,本节点不覆盖)。
- 调用方由 graph.py 的条件边 ``route_after_translate`` 控制:只有
  ``state["layout_issues_detected"]`` 为真时才走到本节点。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from langgraph.types import interrupt

from nodes.node_16_translate_subtitles import (
    _build_interrupt_payload,
    write_srt_from_segments,
)
from state import WorkflowState


def node_checkpoint3_layout_review(state: WorkflowState) -> dict:
    """LangGraph 节点(Week 5 新增):关卡③ — 仅做 interrupt + resume 后写 SRT。

    Returns:
        dict,只含变更字段:
        - ``subtitle_srt_path``: 重写后的 SRT 路径(基于人工修正后的草稿)
        - ``checkpoint3_triggered``: True(便于状态审计)
        - ``status_log``: delta
        注:不写 ``subtitle_segments_en``,避免与 ``node_16a_translate_and_check``
        在同一 superstep 触发 LastValue 并发写冲突。
    """
    issues = list(state.get("layout_issues") or [])
    interrupt(_build_interrupt_payload(state, issues))

    # resume 后:基于"草稿当前内容"重写 SRT(可能已被人工修正)
    # 注意:这里不调用 _read_segments_from_draft,因为写 SRT 是 atomic 操作,
    # write_srt_from_segments 内部已经从 draft_content.json 读 materials.texts
    # 重新序列化(不依赖 state 字段)。
    draft_dir_raw = state.get("draft_dir_en_branch")
    draft_dir = Path(draft_dir_raw) if draft_dir_raw else None

    srt_path: Optional[str] = None
    if draft_dir and draft_dir.exists():
        # write_srt_from_segments 内部从 draft 读 segments 后序列化,
        # 等价于"人工修正后的内容"。
        from nodes.node_16_translate_subtitles import _read_segments_from_draft
        segments_en = _read_segments_from_draft(draft_dir)
        srt_path = write_srt_from_segments(draft_dir, segments_en)

    return {
        "subtitle_srt_path": srt_path,
        "checkpoint3_triggered": True,
        "status_log": ["checkpoint3_resumed"],
    }
