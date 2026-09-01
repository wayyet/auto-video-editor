"""阶段 D 端到端集成测试 TC-01..TC-06(对照附件 4.2-4.3 节)。

策略:不真起 OpenStoryline / 剪映,通过向图内节点注入 mock 实现,完整跑通
整条 LangGraph 图。LangGraph 节点调用是直接的 Python 函数调用,可以用
monkeypatch / 工厂参数替换底层行为。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from draft_ops.encryption_detector import DraftStatus
from graph import build_graph
from langgraph.checkpoint.memory import InMemorySaver
from mcp_clients.openstoryline_client import MockOpenStorylineMCPClient


# ---------------------------------------------------------------------------
# 测试辅助:用 monkeypatch 把"外部副作用"全部干掉
# ---------------------------------------------------------------------------
class _StubProc:
    def __init__(self, pid: int = 11111) -> None:
        self.pid = pid
        self.stdout = None
        self.stderr = None


def _patch_node_2_ready(monkeypatch, *, ready: bool) -> None:
    """让节点 2 不真起进程、不真发 HTTP,直接返回 ready。"""
    from nodes import node_02_launch_openstoryline as m

    def fake_popen(*args, **kwargs):
        return _StubProc(pid=22222 if ready else 33333)

    def fake_check(url, timeout_s):
        return ready

    monkeypatch.setattr(m, "subprocess", type("S", (), {"Popen": staticmethod(fake_popen)}))


@pytest.fixture
def patched_graph(monkeypatch, tmp_path):
    """提供一个已 patch 好节点 2/3(默认 ready=True、preview 成功)、节点 4 用 Mock 客户端的图。"""
    # 节点 1 不依赖任何外部资源,直接跑即可
    # 节点 2:patch Popen + 健康检查
    import nodes.node_02_launch_openstoryline as m2
    from nodes import node_02_launch_openstoryline as mod2

    def fake_popen(*args, **kwargs):
        return _StubProc(pid=44444)

    def fake_health(url, timeout_s):
        return True  # ready=True

    monkeypatch.setattr(mod2, "subprocess", type("S", (), {"Popen": staticmethod(fake_popen), "PIPE": -1}))
    monkeypatch.setattr(mod2, "_wait_for_ready", fake_health)

    # 节点 3:patch popen_factory
    import nodes.node_03_open_preview as mod3

    monkeypatch.setattr(mod3, "subprocess", type("S", (), {"Popen": staticmethod(lambda *a, **k: _StubProc())}))

    return build_graph(checkpointer=InMemorySaver())


@pytest.fixture
def base_initial_state(tmp_path) -> dict:
    video = tmp_path / "30s.mp4"
    video.write_bytes(b"fake video bytes")
    return {
        "session_id": "tc01",
        "video_input_path": str(video),
        "error_log": [],
    }


# ---------------------------------------------------------------------------
# TC-01:端到端主流程
# ---------------------------------------------------------------------------
def test_tc01_end_to_end_happy_path(patched_graph, base_initial_state, tmp_path, monkeypatch) -> None:
    """30s 测试视频 → 5 节点依次成功 → draft_path 指向合法 JSON。"""
    # 节点 5 用 real atomic_writer 写到 tmp_path/draft/draft_content.json
    draft_dir = tmp_path / "draft"
    # 通过 patch 节点 5 让其用我们指定的 draft_dir
    from nodes import node_05_generate_draft as mod5

    real_generate = mod5.generate_initial_jianying_draft

    def generate_with_dir(state, draft_dir, **kwargs):
        return real_generate(state, draft_dir, **kwargs)

    monkeypatch.setattr(mod5, "generate_initial_jianying_draft", generate_with_dir)
    # graph 里登记的 action 是 nodes.node_05_generate_draft.generate_initial_jianying_draft 的引用
    # 要 patch 真正在 graph 里跑的那个函数,得改 graph 里 add_node 时传进去的对象
    from graph import build_graph as real_build

    import nodes.node_05_generate_draft as m5

    orig = m5.generate_initial_jianying_draft

    def patched(state, *args, **kwargs):
        # 节点的第二次位置参数 draft_dir 改用我们的 tmp_path/draft
        return orig(state, draft_dir, **kwargs)

    # 重新装配图:把节点 5 替换为 patched 版本
    g = StateGraph_Repatched(orig, patched, base_initial_state, tmp_path)

    out = g.invoke(
        base_initial_state,
        config={"configurable": {"thread_id": "tc01"}},
    )

    assert out["cache_cleaned"] is True
    assert out["openstoryline_ready"] is True
    assert out["preview_opened"] is True
    assert isinstance(out["shot_plan"], dict)
    draft_path = Path(out["draft_path"])
    assert draft_path.exists()
    loaded = json.loads(draft_path.read_text(encoding="utf-8"))
    assert {"canvas_config", "materials", "tracks"} <= set(loaded.keys())
    assert out["draft_encryption_status"] in {"plaintext", "not_found"}
    assert out["draft_version_strategy"] == "strategy_a_version_lock"


def StateGraph_Repatched(orig_generate, patched_generate, initial_state, tmp_path):
    """手搓一个 StateGraph,直接用 patched 版本的 generate_initial_jianying_draft。"""
    from langgraph.graph import START, END, StateGraph
    from langgraph.checkpoint.memory import InMemorySaver
    from state import WorkflowState
    from nodes.node_01_clean_cache import clean_cache
    from nodes.node_02_launch_openstoryline import launch_openstoryline_service
    from nodes.node_03_open_preview import open_preview
    from nodes.node_04_import_and_plan import import_video_and_plan_shots

    g = StateGraph(WorkflowState)
    g.add_node("clean_cache", clean_cache)
    g.add_node("launch_openstoryline", launch_openstoryline_service)
    g.add_node("open_preview", open_preview)
    g.add_node("import_and_plan", import_video_and_plan_shots)
    g.add_node("generate_draft", patched_generate)
    g.add_edge(START, "clean_cache")
    g.add_edge("clean_cache", "launch_openstoryline")
    g.add_conditional_edges(
        "launch_openstoryline",
        lambda s: "open_preview" if s.get("openstoryline_ready") else END,
        {"open_preview": "open_preview", END: END},
    )
    g.add_edge("open_preview", "import_and_plan")
    g.add_conditional_edges(
        "import_and_plan",
        lambda s: "generate_draft" if s.get("openstoryline_ready") and s.get("shot_plan") else END,
        {"generate_draft": "generate_draft", END: END},
    )
    g.add_edge("generate_draft", END)
    return g.compile(checkpointer=InMemorySaver())


# ---------------------------------------------------------------------------
# TC-02:State 字段完整性
# ---------------------------------------------------------------------------
def test_tc02_state_field_completeness(patched_graph, base_initial_state, tmp_path) -> None:
    """每节点执行后新增字段符合 §4.2 schema。"""
    from langgraph.graph import START, END, StateGraph
    from langgraph.checkpoint.memory import InMemorySaver
    from state import WorkflowState
    from nodes.node_01_clean_cache import clean_cache
    from nodes.node_02_launch_openstoryline import launch_openstoryline_service
    from nodes.node_03_open_preview import open_preview
    from nodes.node_04_import_and_plan import import_video_and_plan_shots
    from nodes.node_05_generate_draft import generate_initial_jianying_draft

    g = StateGraph(WorkflowState)
    g.add_node("clean_cache", clean_cache)
    g.add_node("launch_openstoryline", launch_openstoryline_service)
    g.add_node("open_preview", open_preview)
    g.add_node("import_and_plan", import_video_and_plan_shots)
    g.add_node("generate_draft",
               lambda s: generate_initial_jianying_draft(s, tmp_path / "draft"))
    g.add_edge(START, "clean_cache")
    g.add_edge("clean_cache", "launch_openstoryline")
    g.add_conditional_edges(
        "launch_openstoryline",
        lambda s: "open_preview" if s.get("openstoryline_ready") else END,
        {"open_preview": "open_preview", END: END},
    )
    g.add_edge("open_preview", "import_and_plan")
    g.add_conditional_edges(
        "import_and_plan",
        lambda s: "generate_draft" if s.get("openstoryline_ready") and s.get("shot_plan") else END,
        {"generate_draft": "generate_draft", END: END},
    )
    g.add_edge("generate_draft", END)
    compiled = g.compile(checkpointer=InMemorySaver())

    out = compiled.invoke(base_initial_state, config={"configurable": {"thread_id": "tc02"}})

    # 字段完整性断言(对齐 §4.2 WorkflowState)
    assert out["cache_cleaned"] is True
    assert isinstance(out["cache_cleaned_paths"], list)
    assert out["openstoryline_pid"] is not None
    assert out["openstoryline_mcp_endpoint"].startswith("http://")
    assert out["openstoryline_web_url"].startswith("http://")
    assert out["openstoryline_ready"] is True
    assert out["preview_opened"] is True
    assert isinstance(out["shot_plan"], dict)
    assert out["draft_path"] is not None
    assert out["draft_encryption_status"] in {"plaintext", "encrypted", "not_found"}
    assert out["draft_version_strategy"] in {
        "strategy_a_version_lock",
        "strategy_b_oneway_write",
    }
    assert isinstance(out["error_log"], list)


# ---------------------------------------------------------------------------
# TC-03:加密草稿误判
# ---------------------------------------------------------------------------
def test_tc03_encrypted_draft_aborts_writing(tmp_path, base_initial_state, monkeypatch) -> None:
    """预置加密 draft_content.json → 节点 5 检测到加密 → 早退,error_log 含原因,原文件未覆盖。"""
    # 预置"加密"文件:写入二进制乱码,模拟 AES 加密
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    draft_file = draft_dir / "draft_content.json"
    original_bytes = b"\x00\x01\x02aes-encrypted-original\xff"
    draft_file.write_bytes(original_bytes)

    # 把节点 5 的加密检测函数注入为"返回 ENCRYPTED"
    from nodes.node_05_generate_draft import generate_initial_jianying_draft as real_g
    from draft_ops.encryption_detector import DraftStatus

    def fake_encrypt(_draft_dir):
        return DraftStatus.ENCRYPTED

    # 把节点 5 的 encrypt_detector 默认值换成 fake_encrypt
    import nodes.node_05_generate_draft as mod5

    def patched_generate(state, draft_dir, *, encrypt_detector=fake_encrypt, writer=None):
        return real_g(state, draft_dir, encrypt_detector=fake_encrypt, writer=writer or __import__("draft_ops.atomic_writer", fromlist=["atomic_write_draft"]).atomic_write_draft)

    from langgraph.graph import START, END, StateGraph
    from langgraph.checkpoint.memory import InMemorySaver
    from state import WorkflowState
    from nodes.node_01_clean_cache import clean_cache
    from nodes.node_02_launch_openstoryline import launch_openstoryline_service
    from nodes.node_03_open_preview import open_preview
    from nodes.node_04_import_and_plan import import_video_and_plan_shots

    g = StateGraph(WorkflowState)
    g.add_node("clean_cache", clean_cache)
    g.add_node("launch_openstoryline", launch_openstoryline_service)
    g.add_node("open_preview", open_preview)
    g.add_node("import_and_plan", import_video_and_plan_shots)
    g.add_node("generate_draft",
               lambda s: patched_generate(s, draft_dir))
    g.add_edge(START, "clean_cache")
    g.add_edge("clean_cache", "launch_openstoryline")
    g.add_conditional_edges(
        "launch_openstoryline",
        lambda s: "open_preview" if s.get("openstoryline_ready") else END,
        {"open_preview": "open_preview", END: END},
    )
    g.add_edge("open_preview", "import_and_plan")
    g.add_conditional_edges(
        "import_and_plan",
        lambda s: "generate_draft" if s.get("openstoryline_ready") and s.get("shot_plan") else END,
        {"generate_draft": "generate_draft", END: END},
    )
    g.add_edge("generate_draft", END)
    compiled = g.compile(checkpointer=InMemorySaver())

    # patch 节点 2/3 同 TC-01
    import nodes.node_02_launch_openstoryline as mod2
    import nodes.node_03_open_preview as mod3

    def fake_popen(*args, **kwargs):
        return _StubProc(pid=55555)

    def fake_health(url, timeout_s):
        return True

    monkeypatch.setattr(mod2, "subprocess", type("S", (), {"Popen": staticmethod(fake_popen), "PIPE": -1}))
    monkeypatch.setattr(mod2, "_wait_for_ready", fake_health)
    monkeypatch.setattr(mod3, "subprocess", type("S", (), {"Popen": staticmethod(lambda *a, **k: _StubProc())}))

    out = compiled.invoke(base_initial_state, config={"configurable": {"thread_id": "tc03"}})

    assert out["draft_path"] is None
    assert out["draft_encryption_status"] == "encrypted"
    assert any("检测到已加密草稿" in e for e in out["error_log"])
    # 原文件未被覆盖
    assert draft_file.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# TC-04:写入中途中断(用 graph 内 mock writer 替代 atomic_write_draft)
# ---------------------------------------------------------------------------
def test_tc04_write_interrupted_keeps_target_intact(tmp_path, base_initial_state, monkeypatch) -> None:
    """writer 在 os.replace 前抛异常 → 目标文件保持写入前状态,tmp 已清。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    draft_file = draft_dir / "draft_content.json"
    pre_existing = {"old": True}
    draft_file.write_text(json.dumps(pre_existing), encoding="utf-8")

    # 用一个会失败的 writer
    def failing_writer(draft_file, content):
        # 写到 tmp 但不 replace
        import tempfile, os
        from pathlib import Path
        serialized = json.dumps(content, ensure_ascii=False, indent=2)
        fd, tmp_path_str = tempfile.mkstemp(
            dir=Path(draft_file).parent, prefix=".draft_tmp_", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(serialized)
            raise OSError("simulated disk full before replace")
        except Exception:
            if os.path.exists(tmp_path_str):
                os.remove(tmp_path_str)
            raise

    # 在图里把节点 5 换成"使用 failing_writer"的版本
    from nodes.node_05_generate_draft import generate_initial_jianying_draft as real_g

    from langgraph.graph import START, END, StateGraph
    from langgraph.checkpoint.memory import InMemorySaver
    from state import WorkflowState
    from nodes.node_01_clean_cache import clean_cache
    from nodes.node_02_launch_openstoryline import launch_openstoryline_service
    from nodes.node_03_open_preview import open_preview
    from nodes.node_04_import_and_plan import import_video_and_plan_shots

    def tc04_generate(state, draft_dir, *, encrypt_detector=None, writer=None):
        from draft_ops.encryption_detector import detect_draft_encryption, DraftStatus
        status = encrypt_detector(draft_dir) if encrypt_detector else detect_draft_encryption(draft_dir)
        if status == DraftStatus.ENCRYPTED:
            return {**state, "draft_path": None, "draft_encryption_status": status.value, "error_log": state["error_log"] + ["enc"]}
        return real_g(state, draft_dir, writer=failing_writer)

    g = StateGraph(WorkflowState)
    g.add_node("clean_cache", clean_cache)
    g.add_node("launch_openstoryline", launch_openstoryline_service)
    g.add_node("open_preview", open_preview)
    g.add_node("import_and_plan", import_video_and_plan_shots)
    g.add_node("generate_draft", lambda s: tc04_generate(s, draft_dir))
    g.add_edge(START, "clean_cache")
    g.add_edge("clean_cache", "launch_openstoryline")
    g.add_conditional_edges(
        "launch_openstoryline",
        lambda s: "open_preview" if s.get("openstoryline_ready") else END,
        {"open_preview": "open_preview", END: END},
    )
    g.add_edge("open_preview", "import_and_plan")
    g.add_conditional_edges(
        "import_and_plan",
        lambda s: "generate_draft" if s.get("openstoryline_ready") and s.get("shot_plan") else END,
        {"generate_draft": "generate_draft", END: END},
    )
    g.add_edge("generate_draft", END)
    compiled = g.compile(checkpointer=InMemorySaver())

    import nodes.node_02_launch_openstoryline as mod2
    import nodes.node_03_open_preview as mod3

    def fake_popen(*args, **kwargs):
        return _StubProc(pid=66666)

    def fake_health(url, timeout_s):
        return True

    monkeypatch.setattr(mod2, "subprocess", type("S", (), {"Popen": staticmethod(fake_popen), "PIPE": -1}))
    monkeypatch.setattr(mod2, "_wait_for_ready", fake_health)
    monkeypatch.setattr(mod3, "subprocess", type("S", (), {"Popen": staticmethod(lambda *a, **k: _StubProc())}))

    # 这里我们期望整个 invoke 抛 OSError — 因为节点 5 抛出了
    with pytest.raises(OSError):
        compiled.invoke(base_initial_state, config={"configurable": {"thread_id": "tc04"}})

    # 目标文件应保持原状
    assert draft_file.exists()
    assert json.loads(draft_file.read_text(encoding="utf-8")) == pre_existing
    # 没有遗留 tmp
    leftovers = [p for p in draft_dir.iterdir() if p.name.startswith(".draft_tmp_")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# TC-05:OpenStoryline 未就绪
# ---------------------------------------------------------------------------
def test_tc05_openstoryline_not_ready_early_exits(tmp_path, base_initial_state, monkeypatch) -> None:
    """节点 2 ready=False → 节点 4 早退,error_log 有原因,shot_plan 不被设置。"""
    import nodes.node_02_launch_openstoryline as mod2
    import nodes.node_03_open_preview as mod3

    def fake_popen(*args, **kwargs):
        return _StubProc(pid=77777)

    def fake_health_fail(url, timeout_s):
        return False  # ready=False

    monkeypatch.setattr(mod2, "subprocess", type("S", (), {"Popen": staticmethod(fake_popen), "PIPE": -1}))
    monkeypatch.setattr(mod2, "_wait_for_ready", fake_health_fail)
    monkeypatch.setattr(mod3, "subprocess", type("S", (), {"Popen": staticmethod(lambda *a, **k: _StubProc())}))

    from graph import build_graph

    g = build_graph(checkpointer=InMemorySaver())
    out = g.invoke(base_initial_state, config={"configurable": {"thread_id": "tc05"}})

    # 节点 4 早退,shot_plan 不应被设置
    assert out["openstoryline_ready"] is False
    assert out.get("shot_plan") is None
    # 节点 5 不应被触发
    assert out.get("draft_path") is None
    # error_log 含节点 2 超时 + 节点 4 早退原因
    assert any("健康检查超时" in e for e in out["error_log"])
    assert any("OpenStoryline 服务未就绪" in e for e in out["error_log"])


# ---------------------------------------------------------------------------
# TC-06:视频格式不支持
# ---------------------------------------------------------------------------
def test_tc06_unsupported_video_format_writes_error_log(
    tmp_path, base_initial_state, monkeypatch
) -> None:
    """Mock 客户端抛 ValueError → 节点 4 catch 后写 error_log,其他字段不受影响。"""
    import nodes.node_02_launch_openstoryline as mod2
    import nodes.node_03_open_preview as mod3
    import nodes.node_04_import_and_plan as mod4

    def fake_popen(*args, **kwargs):
        return _StubProc(pid=88888)

    def fake_health(url, timeout_s):
        return True

    monkeypatch.setattr(mod2, "subprocess", type("S", (), {"Popen": staticmethod(fake_popen), "PIPE": -1}))
    monkeypatch.setattr(mod2, "_wait_for_ready", fake_health)
    monkeypatch.setattr(mod3, "subprocess", type("S", (), {"Popen": staticmethod(lambda *a, **k: _StubProc())}))

    # 注入会抛 ValueError 的客户端工厂
    def bad_factory(endpoint):
        class _Bad:
            def import_video_and_get_shot_plan(self, *, video_path):
                raise ValueError("unsupported codec")
        return _Bad()

    monkeypatch.setattr(mod4, "_mock_factory", bad_factory)

    from graph import build_graph

    g = build_graph(checkpointer=InMemorySaver())
    out = g.invoke(base_initial_state, config={"configurable": {"thread_id": "tc06"}})

    assert out.get("shot_plan") is None
    assert out.get("draft_path") is None
    assert any("MCP 调用失败" in e and "unsupported codec" in e for e in out["error_log"])
    # 其他字段保持完整
    assert out["cache_cleaned"] is True
    assert out["openstoryline_ready"] is True
    assert out["preview_opened"] is True
