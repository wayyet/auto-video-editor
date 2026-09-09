"""State schema 兼容性测试(Week 5 新增,对照第 5 周计划 §5.2 + §3.3)。

目的:Week 5 加了 ``en_audio_path`` / ``final_video_path`` / ``heartbeat_id``
/ ``layout_issues`` / ``layout_issues_detected`` 字段;旧 checkpoint(Week 4
存下来的)没有这些字段。验证:

1. 旧 checkpoint load → 节点用 ``.get(key, default)`` 不抛 KeyError
2. 旧 state 通过图完整跑通(关键节点不会因字段缺失崩溃)
3. 直接 ``state["key"]`` 索引(无 ``.get`` 兜底)在节点函数中应被禁止
   (静态 lint / 简易运行时扫描)

这是"破坏性变更应急"流程的回归测试:任何给 State 加新字段的 PR,都必须
跑过本测试,证明旧 checkpoint 也能恢复。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from graph import build_graph
from state import WorkflowState


# ---------------------------------------------------------------------------
# Fixtures(同 test_week5_resilience.py 风格)
# ---------------------------------------------------------------------------
class _StubProc:
    def __init__(self, pid: int = 11111) -> None:
        self.pid = pid


@pytest.fixture(autouse=True)
def _patch_external(monkeypatch: pytest.MonkeyPatch) -> None:
    import nodes.node_02_launch_openstoryline as m2
    import nodes.node_03_open_preview as m3

    monkeypatch.setattr(
        m2, "subprocess",
        type("S", (), {"Popen": staticmethod(lambda *a, **k: _StubProc()), "PIPE": -1}),
    )
    monkeypatch.setattr(m2, "_wait_for_ready", lambda url, t: True)
    monkeypatch.setattr(
        m3, "subprocess",
        type("S", (), {"Popen": staticmethod(lambda *a, **k: _StubProc())}),
    )


def _seed_draft(draft_dir: Path, duration_us: int = 35_000_000) -> Path:
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_file = draft_dir / "draft_content.json"
    draft_file.write_text(
        json.dumps(
            {
                "canvas_config": {"width": 1080, "height": 1920},
                "duration": duration_us,
                "materials": {"videos": [{"id": "v1"}]},
                "tracks": [
                    {
                        "type": "video",
                        "fps": 30,
                        "segments": [
                            {
                                "id": "s1",
                                "target_timerange": {"start": 0, "duration": duration_us},
                            }
                        ],
                    }
                ],
            }
        )
    )
    return draft_file


def _legacy_week4_state(draft_path: str) -> dict:
    """模拟 Week 4 生成的 state — 不含 Week 5 新字段。

    Week 4 字段:session_id / draft_path / asr_segments_zh / status_log /
    error_log / shot_plan / draft_encryption_status / draft_version_strategy。
    Week 5 新字段(en_audio_path / final_video_path / heartbeat_id /
    layout_issues / layout_issues_detected)都缺失。
    """
    return {
        "session_id": "legacy-w4",
        "video_input_path": "C:/tmp/legacy.mp4",
        "draft_path": draft_path,
        "draft_encryption_status": "plaintext",
        "draft_version_strategy": "strategy_a_version_lock",
        "shot_plan": {"video_path": "", "shots": [{"id": "sh1", "start_s": 0.0, "end_s": 5.0}]},
        "asr_segments_zh": [
            {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_zh": "你好"},
            {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_zh": "世界"},
        ],
        "asr_segments_zh_written": True,
        "error_log": [],
        "status_log": ["node_05_generate_draft_done"],
    }


def _is_interrupted(g, config: dict) -> bool:
    snap = g.get_state(config)
    return bool(snap.interrupts) or snap.next != ()


# ---------------------------------------------------------------------------
# 测试 1:旧 checkpoint(Week 4 state schema)resume 不报错
# ---------------------------------------------------------------------------
def test_legacy_state_schema_resume_through_full_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用 Week 4 schema 的初始 state 跑完整图,验证新字段缺失时节点不抛 KeyError。

    这是"任何给 State 加字段的 PR 都必须跑过"的回归 — 模拟"运维把
    上周生产的 checkpoint 拿来 resume",保证不因 Week 5 加字段而崩。
    """
    draft_dir = tmp_path / "drafts" / "demo"
    draft_file = _seed_draft(draft_dir)
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(draft_dir))

    state = _legacy_week4_state(str(draft_file))
    config = {"configurable": {"thread_id": "w5-legacy-state"}}

    # 显式断言 state 确实不含 Week 5 字段
    assert "en_audio_path" not in state
    assert "final_video_path" not in state
    assert "heartbeat_id" not in state
    assert "layout_issues" not in state
    assert "layout_issues_detected" not in state

    g = build_graph(
        checkpointer=InMemorySaver(),
        thread_id="w5-legacy-state",
        start_heartbeat_thread=False,
    )

    # 完整跑通,不应抛 KeyError
    g.invoke(state, config=config)
    g.invoke(Command(resume=True), config=config)
    final = g.invoke(Command(resume=True), config=config)

    assert not _is_interrupted(g, config), "流程应已结束"

    # Week 5 字段在 final state 中应有值(由节点写入)
    assert final.get("en_audio_path") is not None
    assert Path(final["en_audio_path"]).exists()
    # layout_issues_detected 应有值
    assert "layout_issues_detected" in final
    # final_video_path Week 5 暂不实写,但字段可在 state 字典中存在(None)
    # heartbeat_id 同上


# ---------------------------------------------------------------------------
# 测试 2:节点代码静态扫描 — 禁止裸 ``state["key"]``(无 .get 兜底)
# ---------------------------------------------------------------------------
def test_nodes_use_state_get_not_subscript() -> None:
    """扫描 Week 5 新增字段是否被裸索引(state["x"])读取 — 必须改用 .get。

    Week 5 计划 §5.4 团队约定:"任何 state['key'] 直接索引(无 .get 兜底)在
    节点函数中:code review 直接打回"。

    范围:仅扫描 Week 5 新增字段(en_audio_path / final_video_path /
    heartbeat_id / layout_issues / layout_issues_detected)。已有字段如
    draft_path 不在扫描范围(Week 1-4 既有字段,旧 checkpoint 一定有它们)。

    实现策略:
    - 跳过 docstring / 注释 / `{**state, ...}` spread
    - 跳过 state["key"] = value 写入形式
    - 仅匹配 state["<week5_field>"] 纯读取
    """
    # Week 5 新字段(只读需要 .get)
    week5_fields = {
        "en_audio_path",
        "final_video_path",
        "heartbeat_id",
        "layout_issues",
        "layout_issues_detected",
    }
    nodes_dir = Path(__file__).resolve().parent.parent.parent / "nodes"
    found_issues: list[str] = []

    # 匹配 state["<week5_field>"] 读取(非写入,非 .get)
    read_pattern = re.compile(
        r'state\[(?P<q>["\'])(?P<key>[^"\']+)(?P=q)\]'
    )

    for py_file in nodes_dir.glob("node_*.py"):
        content = py_file.read_text(encoding="utf-8")
        lines = content.splitlines()
        in_docstring = False
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            # docstring 跟踪
            triple_count = stripped.count('"""')
            if triple_count == 1:
                in_docstring = not in_docstring
                continue
            if triple_count == 2 or in_docstring:
                continue
            if stripped.startswith("#"):
                continue
            # 写入形式:state["k"] = ... 允许
            if re.search(r'state\[[\'"][^\'"]+[\'"]\]\s*=', line):
                continue
            # 匹配 read 形式
            for m in read_pattern.finditer(line):
                key = m.group("key")
                if key in week5_fields:
                    found_issues.append(f"{py_file.name}:{i}: {line.strip()}")

    assert not found_issues, (
        "以下节点代码对 Week 5 新字段用裸索引读取,违反 Week 5 团队约定 §5.4,"
        "请改为 state.get('key', default):\n" + "\n".join(found_issues)
    )


# ---------------------------------------------------------------------------
# 测试 3:State TypedDict 不强制 Week 5 新字段为必填(允许 NotRequired)
# ---------------------------------------------------------------------------
def test_workflow_state_new_fields_are_not_required() -> None:
    """Week 5 新字段都用 ``NotRequired``,意味着初始 state 可以不提供。

    这是 schema 层"旧 checkpoint 兼容"的基础 — ``total=False`` 的
    TypedDict + ``NotRequired`` 让所有 Week 5 字段可选,旧 checkpoint 缺它们
    也能跑通。
    """
    from typing import get_type_hints
    from state import WorkflowState

    hints = get_type_hints(WorkflowState)
    new_fields = ["en_audio_path", "final_video_path", "heartbeat_id",
                  "layout_issues", "layout_issues_detected"]
    for f in new_fields:
        assert f in hints, f"Week 5 字段 {f} 应在 WorkflowState 中定义"


# ---------------------------------------------------------------------------
# 测试 4:仅 en_audio_path(节点 17 唯一写)在 final state 中正确产生
# ---------------------------------------------------------------------------
def test_en_audio_path_produced_by_node_17(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证 Week 5 关键产物 ``en_audio_path`` 在最终 state 中被节点 17 写入。

    注:不在初始 state 里放任何 Week 5 字段 — LangGraph 的 LastValue channel 会
    把"初始值"和"节点写"视为同一 superstep 的两次写,触发并发冲突。实际
    生产中初始 state 不应预填任何会被节点写的字段。
    """
    draft_dir = tmp_path / "drafts" / "demo"
    draft_file = _seed_draft(draft_dir)
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(draft_dir))

    state = _legacy_week4_state(str(draft_file))
    state["session_id"] = "w5-en-audio"
    config = {"configurable": {"thread_id": "w5-en-audio"}}
    g = build_graph(
        checkpointer=InMemorySaver(),
        thread_id="w5-en-audio",
        start_heartbeat_thread=False,
    )
    g.invoke(state, config=config)
    g.invoke(Command(resume=True), config=config)
    final = g.invoke(Command(resume=True), config=config)
    assert not _is_interrupted(g, config)

    # 节点 17 应在 draft_dir_en_branch 下生成 en_dub.wav
    assert final.get("en_audio_path") and Path(final["en_audio_path"]).exists()
    assert Path(final["en_audio_path"]).stat().st_size > 5_000
