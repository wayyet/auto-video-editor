"""storyline 子图公共 helper(plan_v4 §5 阶段 7)。

阶段 7 抽离 ``_mcp_passthrough.py`` 的 helper 函数(``append_status_tag`` /
``_resolve_outputs_root``)到本模块;删除 vendored 透传壳子。

历史:plan §5 阶段 0 / 1 / 4 / 5 各阶段都在 ``_mcp_passthrough`` 里挂工具
函数,导致每阶段都要重新审视 import 路径。阶段 7 起:
- **本模块** = 纯函数 helper,无 vendored 依赖,可被 19 节点 + qa_gate +
  join_storyline 安全 import。
- **passthrough / vendored_execute / stub_execute** 等透传壳子随
  ``_mcp_passthrough.py`` 一起删除(阶段 4-5 已把所有节点改成本地能力,
  透传不再需要)。

设计纪律(plan §4.4):
1. 失败时按 ``StorylineErrorCode`` 写结构化错误到 ``error_log`` — helper
   仅提供日志追加,不参与决策。
2. ``_resolve_outputs_root`` 默认 ``<repo>/outputs/<job_id>/``,与
   ``storyline.output_isolation`` 兼容。
"""
from __future__ import annotations

from pathlib import Path

from state import WorkflowState


# ---------------------------------------------------------------------------
# outputs_root 解析(原 _mcp_passthrough._resolve_outputs_root)
# ---------------------------------------------------------------------------
def _resolve_outputs_root(state: WorkflowState) -> Path:
    """统一 ``outputs/{job_id}/`` 解析。

    优先级:
    1. ``state.storyline_outputs_root``(显式注入,优先)
    2. ``state.session_id`` → ``outputs/<session_id>``
    3. ``unknown`` 占位(测试场景,实际工作流必有 session_id)
    """
    explicit = state.get("storyline_outputs_root")
    if explicit:
        return Path(str(explicit))
    job_id = str(state.get("session_id") or "unknown")
    return Path(__file__).resolve().parent.parent.parent / "outputs" / job_id


# ---------------------------------------------------------------------------
# status_log 追加(原 _mcp_passthrough.append_status_tag)
# ---------------------------------------------------------------------------
def append_status_tag(state: WorkflowState, *tags: str) -> list[str]:
    """追加 ``tags`` 到 ``state.status_log`` 并返回合并后的 list。

    与 Week 4 的 ``_append_unique`` reducer 兼容:重复 tag 会去重(由 reducer
    处理);本函数仅返回"如果直接 merge 进 state 会是什么样"的 list,调用
    方应 ``{"status_log": append_status_tag(state, "x_done")}``。

    Args:
        state: 当前 state(只读,不修改)。
        *tags: 要追加的 status tag;空标签会被忽略。
    """
    out = list(state.get("status_log", []) or [])
    out.extend(t for t in tags if t)
    return out


# ---------------------------------------------------------------------------
# error_log 追加(原 _mcp_passthrough.append_error)
# ---------------------------------------------------------------------------
def append_error(
    state: WorkflowState,
    *,
    node_kind: str,
    error_code: str,
    message: str,
) -> list[str]:
    """按 ADR-007 规则把结构化错误写到 ``state.error_log`` 并返回合并后 list。

    格式:``[storyline:<node_kind>] <error_code>: <message>``。走 ``_append_unique``
    reducer 时重复会去重。
    """
    msg = f"[storyline:{node_kind}] {error_code}: {message}"
    out = list(state.get("error_log", []) or [])
    out.append(msg)
    return out


__all__ = [
    "_resolve_outputs_root",
    "append_status_tag",
    "append_error",
]