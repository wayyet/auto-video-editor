"""auto-mode 19 节点 LangGraph 图(plan_v4 §2 / §5 阶段 7 已完成本地化)。

本目录是 plan_v4 Phase 4 的新增代码,每个 ``node_<name>.py`` 就是一个
LangGraph 节点函数,签名统一 ``(state: WorkflowState) -> dict``。

- **本目录的代码不进 ``graph.py`` 的常规拓扑**,只在 ``STORYLINE_MODE=auto``
  时通过 ``_route_mode`` 条件边触发。``human`` mode 下完全不动,保证 172 unit
  + 关卡⓪ interrupt/resume 不回归。
- 阶段 0 期间通过 ``_mcp_passthrough`` 透传到 vendored OpenStoryline copy;
  阶段 1~6 逐步把每个节点本地化到 ``storyline_capabilities/``;**阶段 7 已彻底
  删除 ``_mcp_passthrough.py``**,helper 抽到 ``_common.py``。
- ``qa_gate`` / ``join_storyline`` 是 fan-in 收尾节点,只读产物 + 写
  ``status_log`` / ``error_log``。
- ``_common`` 模块提供 ``append_status_tag`` / ``_resolve_outputs_root`` /
  ``append_error`` 三个纯函数 helper,无 vendored 依赖。
"""