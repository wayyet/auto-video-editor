"""节点 4 单测(附件 3.1 节要点)。"""

from __future__ import annotations

from mcp_clients.openstoryline_client import MockOpenStorylineMCPClient
from nodes.node_04_import_and_plan import import_video_and_plan_shots


def _stub_factory(endpoint: str):
    return MockOpenStorylineMCPClient(endpoint=endpoint)


def test_ready_false_early_exits_without_modifying_other_fields() -> None:
    """ready=False → 早退,只追加 error_log,shot_plan 不被设置。"""
    state = {
        "openstoryline_ready": False,
        "openstoryline_mcp_endpoint": "http://127.0.0.1:8006/mcp",
        "video_input_path": "/tmp/30s.mp4",
        "error_log": [],
    }
    out = import_video_and_plan_shots(state, client_factory=_stub_factory)
    assert "shot_plan" not in out
    assert any("OpenStoryline 服务未就绪" in e for e in out["error_log"])


def test_ready_true_with_mock_client_writes_shot_plan() -> None:
    """ready=True + Mock 客户端 → shot_plan 写入,且包含 shots 字段。"""
    state = {
        "openstoryline_ready": True,
        "openstoryline_mcp_endpoint": "http://127.0.0.1:8006/mcp",
        "video_input_path": "/tmp/30s.mp4",
        "error_log": [],
    }
    out = import_video_and_plan_shots(state, client_factory=_stub_factory)
    assert "shot_plan" in out
    plan = out["shot_plan"]
    assert plan["video_path"] == "/tmp/30s.mp4"
    assert isinstance(plan["shots"], list)
    assert len(plan["shots"]) >= 1


def test_mcp_exception_recorded_in_error_log() -> None:
    """客户端抛 ValueError → error_log 记录,其他字段不受影响(TC-06)。"""
    state = {
        "openstoryline_ready": True,
        "openstoryline_mcp_endpoint": "http://127.0.0.1:8006/mcp",
        "video_input_path": "/tmp/bad_codec.mp4",
        "error_log": [],
    }

    def bad_factory(endpoint):
        class _BadClient:
            def import_video_and_get_shot_plan(self, *, video_path):
                raise ValueError(f"unsupported codec: {video_path}")
        return _BadClient()

    out = import_video_and_plan_shots(state, client_factory=bad_factory)
    assert "shot_plan" not in out
    assert any("MCP 调用失败" in e and "unsupported codec" in e for e in out["error_log"])
