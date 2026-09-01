"""节点 13:adjust_volume — 占位节点(Week 3 范围)。

Week 3 仅占位:写 ``status_log`` 后直接通过,不动 ``draft_content.json``。
真实音量/淡入淡出逻辑顺延 Week 4,届时先做字段逆向工程:
1. 取测试草稿备份
2. 在剪映客户端内手动给音频片段加淡入 2s/淡出 3s,音量 -6dB
3. diff draft_content.json 定位 materials.audio_fades 字段结构
4. 按逆向结果实现 jianying_adjust_volume()
"""

from __future__ import annotations

from state import WorkflowState


def adjust_volume(state: WorkflowState) -> dict:
    """占位实现:仅写 status_log + 标记 volume_adjusted=False,不动草稿。"""
    log = list(state.get("status_log", []) or []) + ["node_13_adjust_volume_placeholder_pass"]
    return {**state, "status_log": log, "volume_adjusted": False}