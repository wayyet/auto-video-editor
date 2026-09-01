"""节点 4:import_video_and_plan_shots — 复用节点 2 的 OpenStoryline MCP 连接(附件 3.1 节)。

OpenStoryline 内置 VLM 规划;本节点只负责调用 MCP 工具并把结果写入 state。
ready=False 时早退(只追加 error_log,不修改其他字段)。
"""

from __future__ import annotations

from state import WorkflowState

# 默认客户端工厂 — 阶段 E / 真实接入时替换
_default_client_factory = None


def set_default_client_factory(factory: callable | None) -> None:
    """允许运行期注入客户端工厂(便于生产环境用 HTTPOpenStorylineMCPClient)。"""
    global _default_client_factory
    _default_client_factory = factory


def import_video_and_plan_shots(
    state: WorkflowState,
    *,
    client_factory: callable | None = None,
) -> dict:
    """导入视频并获取分镜规划,写入 state["shot_plan"]。

    Args:
        state: 当前工作流状态(需含 openstoryline_ready /
            openstoryline_mcp_endpoint / video_input_path)。
        client_factory: 可选工厂,接受 endpoint 返回实现了
            import_video_and_get_shot_plan 方法的对象;None 时使用 Mock。
    """
    errors = list(state.get("error_log", []) or [])

    if not state.get("openstoryline_ready"):
        errors.append("[node_04] OpenStoryline 服务未就绪,跳过分镜规划")
        return {**state, "error_log": errors}

    video_path = state.get("video_input_path")
    if not video_path:
        errors.append("[node_04] video_input_path 为空,无法调用 MCP")
        return {**state, "error_log": errors}

    factory = client_factory or _default_client_factory or _mock_factory
    endpoint = state.get("openstoryline_mcp_endpoint") or ""
    client = factory(endpoint)

    try:
        shot_plan = client.import_video_and_get_shot_plan(video_path=video_path)
    except Exception as e:  # noqa: BLE001 - 附件 6 节 TC-06 要求捕获写入 error_log
        errors.append(f"[node_04] MCP 调用失败: {e}")
        return {**state, "error_log": errors}

    return {**state, "shot_plan": shot_plan, "error_log": errors}


def _mock_factory(endpoint: str):
    from mcp_clients.openstoryline_client import MockOpenStorylineMCPClient

    return MockOpenStorylineMCPClient(endpoint=endpoint)
