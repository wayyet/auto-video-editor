"""关卡①/② interrupt/resume 联调测试(Week 3 Day5,对应附件 5 节)。

LangGraph 1.2.9 行为:``invoke()`` **不抛** GraphInterrupt;中断信息通过
``g.get_state(config).interrupts`` / ``snap.next`` 暴露。resume 用
``Command(resume=True)`` 触发。

覆盖:
1. 基础 interrupt — interrupt() 挂起 + checkpointer 持久化 + Command(resume=True) 续跑
2. 多日挂起模拟 — 全新 graph + 同 thread_id 跨进程恢复(SqliteSaver)
3. 副作用幂等 — _send_notification 只发 1 次
4. thread_id 隔离 — 两个 thread 并发互不干扰
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from config import CHECKPOINTER_DB_DIR, make_checkpointer
from graph import build_graph
from state import WorkflowState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class _StubProc:
    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid


@pytest.fixture(autouse=True)
def _patch_external(monkeypatch: pytest.MonkeyPatch) -> None:
    """屏蔽节点 2/3 的真实进程拉起;_wait_for_ready 永远返回 True。"""
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


def _seed_state(draft_path: str) -> WorkflowState:
    return {
        "session_id": "int-test",
        "video_input_path": "C:/tmp/fake.mp4",
        "draft_path": draft_path,
        "draft_encryption_status": "plaintext",
        "draft_version_strategy": "strategy_a_version_lock",
        "shot_plan": {"video_path": "", "shots": [{"id": "sh1", "start_s": 0.0, "end_s": 5.0}]},
        "error_log": [],
        "status_log": ["node_05_generate_draft_done"],
    }


def _seed_draft(path: Path, duration_us: int = 35_000_000) -> None:
    """恰好 35s 的草稿 — 节点 7 不变速。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": duration_us,
        "materials": {"videos": [{"id": "v1"}]},
        "tracks": [{
            "type": "video",
            "fps": 30,
            "segments": [{"id": "s1", "target_timerange": {"start": 0, "duration": duration_us}}],
        }],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[WorkflowState, Path]:
    """35s 草稿 + state(节点 7 一次通过)。"""
    draft_dir = tmp_path / "drafts" / "demo"
    draft_file = draft_dir / "draft_content.json"
    _seed_draft(draft_file, duration_us=35_000_000)
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(draft_dir))
    return _seed_state(str(draft_file)), draft_file


def _build_inmemory_graph(thread_id: str):
    return build_graph(checkpointer=InMemorySaver(), thread_id=thread_id, start_heartbeat_thread=False)


def _build_sqlite_graph(thread_id: str, sqlite_path: Path | None = None):
    """显式构造 sqlite checkpointer 的图。sqlite_path 用于测试隔离到 tmp 目录。"""
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver

    if sqlite_path is None:
        sqlite_path = CHECKPOINTER_DB_DIR / f"{thread_id}.sqlite"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(sqlite_path), check_same_thread=False)
    saver = SqliteSaver(conn)
    return build_graph(checkpointer=saver, thread_id=thread_id, start_heartbeat_thread=False)


def _is_interrupted(g, config: dict) -> bool:
    """检查 graph 是否在给定 config 中停在 interrupt 节点。"""
    snap = g.get_state(config)
    return bool(snap.interrupts) or snap.next != ()


# ---------------------------------------------------------------------------
# 测试 1:基础 interrupt
# ---------------------------------------------------------------------------
def test_checkpoint1_interrupt_and_resume_basic(seeded) -> None:
    """interrupt() 挂起 → 状态在 checkpointer 中 → Command(resume=True) 续跑。"""
    state, _ = seeded
    config = {"configurable": {"thread_id": "int-basic"}}
    g = _build_inmemory_graph("int-basic")

    # 第一次 invoke:跑到节点 6 触发 interrupt
    g.invoke(state, config=config)
    assert _is_interrupted(g, config), "节点 6 应挂起于 interrupt"

    # resume 后节点 6 继续 → 节点 7 → 节点 12 interrupt
    g.invoke(Command(resume=True), config=config)
    assert _is_interrupted(g, config), "节点 12 应挂起于 interrupt"

    # resume 关卡② → 节点 13 → END
    final = g.invoke(Command(resume=True), config=config)
    assert not _is_interrupted(g, config), "流程应已结束"

    # 节点 5 不应被重跑
    assert final["status_log"].count("node_05_generate_draft_done") == 1
    # checkpoint1/2 各 resumed 一次
    assert final["status_log"].count("checkpoint1_resumed") == 1
    assert final["status_log"].count("checkpoint2_resumed") == 1
    # 节点 13 占位通过
    assert final["status_log"].count("node_13_adjust_volume_placeholder_pass") == 1


# ---------------------------------------------------------------------------
# 测试 2:多日挂起模拟(同 sqlite 文件 + 全新 graph 实例)
# ---------------------------------------------------------------------------
def test_multi_day_resume_via_sqlite_persistence(seeded, tmp_path: Path) -> None:
    """模拟"全新 graph 实例 + 同 sqlite 文件 = 跨进程恢复"。"""
    state, _ = seeded
    thread_id = "multi-day"
    config = {"configurable": {"thread_id": thread_id}}

    sqlite_path = tmp_path / f"{thread_id}.sqlite"

    g1 = _build_sqlite_graph(thread_id, sqlite_path=sqlite_path)
    g1.invoke(state, config=config)
    assert _is_interrupted(g1, config), "g1 应停在节点 6 interrupt"

    # 模拟"数天后":丢弃 g1,新建 graph2
    g2 = _build_sqlite_graph(thread_id, sqlite_path=sqlite_path)
    g2.invoke(Command(resume=True), config=config)
    assert _is_interrupted(g2, config), "g2 resume 后应在节点 12 interrupt"

    final = g2.invoke(Command(resume=True), config=config)
    assert not _is_interrupted(g2, config), "g2 应已结束"

    # 节点 1-5 不应被重跑
    assert final["status_log"].count("node_05_generate_draft_done") == 1
    # 完整路径跑通
    assert final["status_log"].count("node_13_adjust_volume_placeholder_pass") == 1

    # 关闭连接 + 清理 sqlite 文件(tmp_path 隔离,Windows 锁也能 unlink)
    try:
        g1.checkpointer.__exit__(None, None, None)
        g2.checkpointer.__exit__(None, None, None)
    except Exception:
        pass
    # tmp_path 由 pytest 自动清理


# ---------------------------------------------------------------------------
# 测试 3:副作用幂等 — 通知只发 1 次
# ---------------------------------------------------------------------------
def test_notification_only_sent_once_on_resume(seeded) -> None:
    """interrupt() 之前不调用通知,resume 后才发,且仅发 1 次。"""
    state, _ = seeded
    config = {"configurable": {"thread_id": "int-notify"}}
    g = _build_inmemory_graph("int-notify")

    call_count: list[int] = [0]

    def counting_notifier(state: dict, checkpoint: str) -> None:
        call_count[0] += 1

    with patch("nodes.node_06_human_reorder._post_resume", side_effect=lambda s: {**s, "reorder_notified": True, "status_log": list(s.get("status_log") or []) + ["checkpoint1_resumed"]}) as mock_post:
        # 第一次 invoke:interrupt 之前 _post_resume 不应被调用
        g.invoke(state, config=config)
        assert mock_post.call_count == 0, f"interrupt 之前不应调用 _post_resume,实际 {mock_post.call_count} 次"

        # resume 关卡①:_post_resume 应被调 1 次
        g.invoke(Command(resume=True), config=config)
        assert mock_post.call_count == 1, f"resume 关卡① 后 _post_resume 应为 1 次,实际 {mock_post.call_count} 次"

        # resume 关卡②:node_06 不应再被调用(走 node_12)
        g.invoke(Command(resume=True), config=config)
        assert mock_post.call_count == 1, f"关卡② 应不计入 node_06 mock,实际 {mock_post.call_count} 次"


# ---------------------------------------------------------------------------
# 测试 4:thread_id 隔离 — 两个 thread 并发互不干扰
# ---------------------------------------------------------------------------
def test_thread_id_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded) -> None:
    """两个 thread 并发触发关卡①,乱序 resume,各自 draft_path 不互相覆盖。"""
    _, draft_file = seeded

    draft_a = tmp_path / "drafts" / "thread_a" / "draft_content.json"
    draft_b = tmp_path / "drafts" / "thread_b" / "draft_content.json"
    _seed_draft(draft_a, 35_000_000)
    _seed_draft(draft_b, 35_000_000)

    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts"))

    state_a = _seed_state(str(draft_a))
    state_b = _seed_state(str(draft_b))

    config_a = {"configurable": {"thread_id": "thread_a"}}
    config_b = {"configurable": {"thread_id": "thread_b"}}

    g_a = _build_inmemory_graph("thread_a")
    g_b = _build_inmemory_graph("thread_b")

    # 两个 thread 都跑到节点 6 interrupt
    g_a.invoke(state_a, config=config_a)
    g_b.invoke(state_b, config=config_b)
    assert _is_interrupted(g_a, config_a)
    assert _is_interrupted(g_b, config_b)

    # 乱序 resume:b 先到结束, a 后到结束
    g_b.invoke(Command(resume=True), config=config_b)
    final_b = g_b.invoke(Command(resume=True), config=config_b)
    assert not _is_interrupted(g_b, config_b)

    g_a.invoke(Command(resume=True), config=config_a)
    final_a = g_a.invoke(Command(resume=True), config=config_a)
    assert not _is_interrupted(g_a, config_a)

    # A 不应有 B 的 thread_id 痕迹
    assert all("thread_b" not in str(item) for item in final_a["status_log"])
    # A 应跑完整路径
    assert final_a["status_log"].count("node_13_adjust_volume_placeholder_pass") == 1
    # B 同样
    assert final_b["status_log"].count("node_13_adjust_volume_placeholder_pass") == 1

    # 草稿文件应保持分离
    assert draft_a.exists()
    assert draft_b.exists()