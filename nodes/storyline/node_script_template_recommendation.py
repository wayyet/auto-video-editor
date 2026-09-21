"""Phase 3/7 旁路节点:storyline_script_template_recommendation(plan_v4 §2.2)。

阶段 0 期间该节点是 vendored 透传壳子;阶段 7 彻底删除 ``_mcp_passthrough`` 后,
本地化为 noop 节点 — 仅 append status_log,不写任何字段(plan §3.3 注释:
"``script_template_rec`` **不** 写 ``storyline_script_artifact``(避免与
``generate_script`` 并行写同一字段触发 LangGraph ``InvalidUpdateError``)")。

后续如需真实 script template 渲染能力(范文风格库注入),新建
``storyline_capabilities.script_template_rec`` 实现并接到本节点。

输入:
- ``storyline_groups_artifact`` (与 generate_script 并行,不修改)
- ``storyline_script_artifact`` (上游,不被覆盖)

输出:
- ``status_log`` append ``storyline_script_template_recommendation_done``
"""
from __future__ import annotations

from state import WorkflowState
from nodes.storyline._common import append_status_tag


def storyline_script_template_recommendation_node(
    state: WorkflowState,
) -> dict:
    # 阶段 7 起:noop(plan §3.3 第 3 行);留 hook 给未来接入 script_template_rec
    return {
        **state,
        "status_log": append_status_tag(
            state, "storyline_script_template_recommendation_done"
        ),
    }