"""关卡①/② interrupt/resume 联调测试(Week 3 Day5,对应附件 5 节;阶段3 auto-only 版)。

LangGraph 1.2.9 行为:``invoke()`` **不抛** GraphInterrupt;中断信息通过
``g.get_state(config).interrupts`` / ``snap.next`` 暴露。resume 用
``Command(resume=True)`` 触发。

阶段3(auto-only)后流程:第一次 invoke 从 START 直入 storyline 链(stub 模式
确定性跑完)→ generate_draft(已有草稿则跳过)→ 关卡① interrupt;resume →
关卡②;resume → END。旧关卡⓪(checkpoint0_storyline_plan)已随 human 模式
删除,相关覆盖一并移除。

覆盖:
1. 基础 interrupt — interrupt() 挂起 + checkpointer 持久化 + Command(resume=True) 续跑
2. 多日挂起模拟 — 全新 graph 实例 + 同一个 checkpointer 跨实例恢复
   (2026-10 阶段 C:SQLite 后端已删除;真正的"新数据库连接"恢复覆盖移到
   ``tests/integration/test_postgres_resume.py``,用真实 Postgres)
3. 副作用幂等 — _send_notification 只发 1 次
4. thread_id 隔离 — 两个 thread 并发互不干扰
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from graph import build_graph
from state import WorkflowState
from draft_ops.draft_integrity import REQUIRED_FEATURE_KEYS


REQUIRED_FEATURES_NONE = {key: False for key in REQUIRED_FEATURE_KEYS}


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
        "required_features": REQUIRED_FEATURES_NONE,
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
    """35s 草稿 + state(节点 7 一次通过);storyline 产物隔离到 tmp。"""
    draft_dir = tmp_path / "drafts" / "demo"
    draft_file = draft_dir / "draft_content.json"
    _seed_draft(draft_file, duration_us=35_000_000)
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(draft_dir))
    state = _seed_state(str(draft_file))
    state["storyline_outputs_root"] = str(tmp_path / "os_outputs")
    return state, draft_file


def _build_inmemory_graph(thread_id: str, saver: InMemorySaver | None = None):
    """编译一张图。

    ``saver`` 显式传入时,多张图共享同一个 checkpointer —— 用来模拟
    "进程重启后用同一个存档恢复"(阶段 C 之后 SQLite 后端已删除,
    跨数据库连接的恢复覆盖见 ``tests/integration/test_postgres_resume.py``)。
    """
    return build_graph(
        checkpointer=saver if saver is not None else InMemorySaver(),
        thread_id=thread_id,
        start_heartbeat_thread=False,
    )


def _is_interrupted(g, config: dict) -> bool:
    """检查 graph 是否在给定 config 中停在 interrupt 节点。"""
    snap = g.get_state(config)
    return bool(snap.interrupts) or snap.next != ()


# ---------------------------------------------------------------------------
# 测试 1:基础 interrupt
# ---------------------------------------------------------------------------
def test_checkpoint1_interrupt_and_resume_basic(seeded) -> None:
    """interrupt() 挂起 → 状态在 checkpointer 中 → Command(resume=True) 续跑。

    阶段3 后:第一次 invoke 跑完 storyline 链停在关卡①,再 resume → 关卡②,
    再 resume → END(关卡⓪ 已删除)。
    """
    state, _ = seeded
    config = {"configurable": {"thread_id": "int-basic"}}

    g = _build_inmemory_graph("int-basic")

    # 第一次 invoke:storyline 链 → generate_draft(已有草稿跳过)→ 关卡① interrupt
    g.invoke(state, config=config)
    assert _is_interrupted(g, config), "应停在节点 6 interrupt(关卡①)"

    # resume 关卡① → 节点 7 → 节点 12 interrupt
    g.invoke(Command(resume=True), config=config)
    assert _is_interrupted(g, config), "应停在节点 12 interrupt"

    # resume 关卡② → 节点 13 → END
    final = g.invoke(Command(resume=True), config=config)
    assert not _is_interrupted(g, config), "流程应已结束"

    # 节点 5 不应被重跑
    assert final["status_log"].count("node_05_generate_draft_done") == 1
    # 关卡①/② 各 resumed 一次(关卡⓪ 已删除,不再有 checkpoint0_resumed)
    assert final["status_log"].count("checkpoint1_resumed") == 1
    assert final["status_log"].count("checkpoint2_resumed") == 1
    # 节点 13 通过(Week 3 补全后真实实现:有 audio track → node_13_adjust_volume_done;
    # 无 audio track → node_13_adjust_volume_no_audio_track 降级路径)
    node_13_tags = [
        t for t in final["status_log"]
        if t in ("node_13_adjust_volume_done", "node_13_adjust_volume_no_audio_track")
    ]
    assert len(node_13_tags) == 1, f"节点 13 应执行 1 次,实际 {len(node_13_tags)} 次"


# ---------------------------------------------------------------------------
# 测试 2:多日挂起模拟(同一个 checkpointer + 全新 graph 实例)
# ---------------------------------------------------------------------------
def test_multi_day_resume_via_shared_checkpointer(seeded) -> None:
    """模拟"全新 graph 实例 + 同一个存档 = 跨实例恢复"。

    阶段3 后:第一次 invoke 停在关卡①,resume 后关卡②,resume 后 END。

    2026-10(阶段 C):SQLite 后端已删除,这里用共享的 ``InMemorySaver``
    保住"重建图后仍能从存档 resume"这条行为覆盖;
    真正的新数据库连接(Postgres)恢复覆盖见
    ``tests/integration/test_postgres_resume.py``。
    """
    state, _ = seeded
    thread_id = "multi-day"
    config = {"configurable": {"thread_id": thread_id}}

    saver = InMemorySaver()

    g1 = _build_inmemory_graph(thread_id, saver=saver)
    g1.invoke(state, config=config)
    assert _is_interrupted(g1, config), "g1 应停在关卡① interrupt"

    # 模拟"数天后":丢弃 g1,新建 graph2(共享同一个 checkpointer)
    g2 = _build_inmemory_graph(thread_id, saver=saver)
    g2.invoke(Command(resume=True), config=config)
    assert _is_interrupted(g2, config), "g2 resume 关卡① 后应在节点 12 interrupt"

    final = g2.invoke(Command(resume=True), config=config)
    assert not _is_interrupted(g2, config), "g2 应已结束"

    # 节点 1-5 不应被重跑
    assert final["status_log"].count("node_05_generate_draft_done") == 1
    # 完整路径跑通(Week 3 补全后节点 13 是真实实现,无 audio track 时走降级)
    node_13_tags = [
        t for t in final["status_log"]
        if t in ("node_13_adjust_volume_done", "node_13_adjust_volume_no_audio_track")
    ]
    assert len(node_13_tags) == 1, f"节点 13 应执行 1 次,实际 {len(node_13_tags)} 次"


# ---------------------------------------------------------------------------
# 测试 3:副作用幂等 — 通知只发 1 次
# ---------------------------------------------------------------------------
def test_notification_only_sent_once_on_resume(seeded) -> None:
    """interrupt() 之前不调用通知,resume 后才发,且仅发 1 次。

    阶段3 后:第一次 invoke 停在关卡① interrupt(不计入 node_06 mock),
    resume 关卡① → _post_resume(被 mock)→ 关卡② → resume 后 END。
    """
    from unittest.mock import patch

    state, _ = seeded
    config = {"configurable": {"thread_id": "int-notify"}}

    g = _build_inmemory_graph("int-notify")

    with patch(
        "nodes.node_06_human_reorder._post_resume",
        side_effect=lambda s: {
            **s,
            "reorder_notified": True,
            "status_log": list(s.get("status_log") or []) + ["checkpoint1_resumed"],
        },
    ) as mock_post:
        # 第一次 invoke:应停在关卡① interrupt(不计入 node_06 mock)
        g.invoke(state, config=config)
        assert mock_post.call_count == 0, f"关卡① interrupt 前不应调用 node_06 mock,实际 {mock_post.call_count} 次"

        # resume 关卡① → _post_resume 应被调 1 次
        g.invoke(Command(resume=True), config=config)
        assert mock_post.call_count == 1, f"resume 关卡① 后 _post_resume 应为 1 次,实际 {mock_post.call_count} 次"

        # resume 关卡②:node_06 不应再被调用(走 node_12)
        g.invoke(Command(resume=True), config=config)
        assert mock_post.call_count == 1, f"关卡② 应不计入 node_06 mock,实际 {mock_post.call_count} 次"


# ---------------------------------------------------------------------------
# 测试 4:thread_id 隔离 — 两个 thread 并发互不干扰
# ---------------------------------------------------------------------------
def test_thread_id_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded) -> None:
    """两个 thread 并发触发关卡①,乱序 resume,各自 draft_path 不互相覆盖。

    阶段3 后:两个 thread 第一次 invoke 都停在关卡①,
    再乱序 resume 关卡① → 关卡② → END。
    """
    draft_a = tmp_path / "drafts" / "thread_a" / "draft_content.json"
    draft_b = tmp_path / "drafts" / "thread_b" / "draft_content.json"
    _seed_draft(draft_a, 35_000_000)
    _seed_draft(draft_b, 35_000_000)

    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts"))

    state_a = _seed_state(str(draft_a))
    state_b = _seed_state(str(draft_b))
    state_a["session_id"] = "thread_a"
    state_b["session_id"] = "thread_b"
    state_a["storyline_outputs_root"] = str(tmp_path / "os_outputs_a")
    state_b["storyline_outputs_root"] = str(tmp_path / "os_outputs_b")

    config_a = {"configurable": {"thread_id": "thread_a"}}
    config_b = {"configurable": {"thread_id": "thread_b"}}

    g_a = _build_inmemory_graph("thread_a")
    g_b = _build_inmemory_graph("thread_b")

    # 两个 thread 都跑到关卡① interrupt
    g_a.invoke(state_a, config=config_a)
    g_b.invoke(state_b, config=config_b)
    assert _is_interrupted(g_a, config_a)
    assert _is_interrupted(g_b, config_b)

    # 乱序 resume:b 先到结束,a 后到结束
    g_b.invoke(Command(resume=True), config=config_b)
    g_b.invoke(Command(resume=True), config=config_b)
    final_b = g_b.invoke(Command(resume=True), config=config_b)
    assert not _is_interrupted(g_b, config_b)

    g_a.invoke(Command(resume=True), config=config_a)
    g_a.invoke(Command(resume=True), config=config_a)
    final_a = g_a.invoke(Command(resume=True), config=config_a)
    assert not _is_interrupted(g_a, config_a)

    # A 不应有 B 的 thread_id 痕迹
    assert all("thread_b" not in str(item) for item in final_a["status_log"])
    # A 应跑完整路径(Week 3 补全后节点 13 是真实实现)
    node_13_a = [
        t for t in final_a["status_log"]
        if t in ("node_13_adjust_volume_done", "node_13_adjust_volume_no_audio_track")
    ]
    assert len(node_13_a) == 1, f"A 节点 13 应执行 1 次,实际 {len(node_13_a)} 次"
    # B 同样
    node_13_b = [
        t for t in final_b["status_log"]
        if t in ("node_13_adjust_volume_done", "node_13_adjust_volume_no_audio_track")
    ]
    assert len(node_13_b) == 1, f"B 节点 13 应执行 1 次,实际 {len(node_13_b)} 次"

    # 草稿文件应保持分离
    assert draft_a.exists()
    assert draft_b.exists()
