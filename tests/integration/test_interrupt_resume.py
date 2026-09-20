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
    """显式构造 sqlite checkpointer 的图。sqlite_path 用于测试隔离到 tmp 目录。

    需要 ``langgraph-checkpoint-sqlite`` 包(``pyproject.toml`` 已声明);
    如果包未安装,本测试被跳过(见 test_multi_day_resume_via_sqlite_persistence 的
    ``pytest.mark.skipif``)。
    """
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver

    if sqlite_path is None:
        sqlite_path = CHECKPOINTER_DB_DIR / f"{thread_id}.sqlite"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(sqlite_path), check_same_thread=False)
    saver = SqliteSaver(conn)
    return build_graph(checkpointer=saver, thread_id=thread_id, start_heartbeat_thread=False)


_SQLITE_AVAILABLE = pytest.mark.skipif(
    __import__("importlib").util.find_spec("langgraph.checkpoint.sqlite") is None,
    reason="langgraph-checkpoint-sqlite 未安装(运行 pip install langgraph-checkpoint-sqlite 启用本测试)",
)


def _is_interrupted(g, config: dict) -> bool:
    """检查 graph 是否在给定 config 中停在 interrupt 节点。"""
    snap = g.get_state(config)
    return bool(snap.interrupts) or snap.next != ()


# ---------------------------------------------------------------------------
# 测试 1:基础 interrupt
# ---------------------------------------------------------------------------
def test_checkpoint1_interrupt_and_resume_basic(seeded, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """interrupt() 挂起 → 状态在 checkpointer 中 → Command(resume=True) 续跑。

    2026-09 迁移后:第一次 invoke 停在关卡⓪(新),再 resume → 关卡①,再 resume → END。
    """
    state, _ = seeded
    config = {"configurable": {"thread_id": "int-basic"}}

    # 预置 openstoryline 产物让 node_04 读到 storyline_plan
    os_root = tmp_path / "os_outputs"
    _seed_openstoryline_outputs(os_root, session_id="sid-basic")
    import nodes.node_04_import_and_plan as _m4
    monkeypatch.setattr(_m4, "OPENSTORYLINE_OUTPUTS_ROOT", os_root)

    g = _build_inmemory_graph("int-basic")

    # 第一次 invoke:跑到 checkpoint0_storyline_plan 触发 interrupt
    g.invoke(state, config=config)
    assert _is_interrupted(g, config), "应停在 checkpoint0_storyline_plan interrupt"

    # resume 关卡⓪ → 节点 4 读产物 → 节点 5(已有 draft_path 跳过) → 节点 6 interrupt
    g.invoke(Command(resume=True), config=config)
    assert _is_interrupted(g, config), "应停在节点 6 interrupt(关卡①)"

    # resume 关卡① → 节点 7 → 节点 12 interrupt
    g.invoke(Command(resume=True), config=config)
    assert _is_interrupted(g, config), "应停在节点 12 interrupt"

    # resume 关卡② → 节点 13 → END
    final = g.invoke(Command(resume=True), config=config)
    assert not _is_interrupted(g, config), "流程应已结束"

    # 节点 5 不应被重跑
    assert final["status_log"].count("node_05_generate_draft_done") == 1
    # checkpoint0/1/2 各 resumed 一次
    assert final["status_log"].count("checkpoint0_resumed") == 1
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
# 测试 2:多日挂起模拟(同 sqlite 文件 + 全新 graph 实例)
# ---------------------------------------------------------------------------
@_SQLITE_AVAILABLE
def test_multi_day_resume_via_sqlite_persistence(seeded, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟"全新 graph 实例 + 同 sqlite 文件 = 跨进程恢复"。

    2026-09 迁移后:第一次 invoke 停在关卡⓪,resume 后关卡①,resume 后 END。
    """
    state, _ = seeded
    thread_id = "multi-day"
    config = {"configurable": {"thread_id": thread_id}}

    # 预置产物
    os_root = tmp_path / "os_outputs"
    _seed_openstoryline_outputs(os_root, session_id="sid-multi")
    import nodes.node_04_import_and_plan as _m4
    monkeypatch.setattr(_m4, "OPENSTORYLINE_OUTPUTS_ROOT", os_root)

    sqlite_path = tmp_path / f"{thread_id}.sqlite"

    g1 = _build_sqlite_graph(thread_id, sqlite_path=sqlite_path)
    g1.invoke(state, config=config)
    assert _is_interrupted(g1, config), "g1 应停在 checkpoint0_storyline_plan interrupt"

    # 模拟"数天后":丢弃 g1,新建 graph2
    g2 = _build_sqlite_graph(thread_id, sqlite_path=sqlite_path)
    g2.invoke(Command(resume=True), config=config)
    assert _is_interrupted(g2, config), "g2 resume 关卡⓪ 后应在节点 6 interrupt"

    g2.invoke(Command(resume=True), config=config)
    assert _is_interrupted(g2, config), "g2 resume 关卡① 后应在节点 12 interrupt"

    final = g2.invoke(Command(resume=True), config=config)
    assert not _is_interrupted(g2, config), "g2 应已结束"

    # 节点 1-5 不应被重跑
    assert final["status_log"].count("node_05_generate_draft_done") == 1
    assert final["status_log"].count("checkpoint0_resumed") == 1
    # 完整路径跑通(Week 3 补全后节点 13 是真实实现,无 audio track 时走降级)
    node_13_tags = [
        t for t in final["status_log"]
        if t in ("node_13_adjust_volume_done", "node_13_adjust_volume_no_audio_track")
    ]
    assert len(node_13_tags) == 1, f"节点 13 应执行 1 次,实际 {len(node_13_tags)} 次"

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
def test_notification_only_sent_once_on_resume(seeded, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """interrupt() 之前不调用通知,resume 后才发,且仅发 1 次。

    2026-09 迁移后:第一次 invoke 停在关卡⓪(不计入 node_06 mock),
    resume 关卡⓪ → 关卡① interrupt → 关卡① _post_resume(被 mock)。
    """
    state, _ = seeded
    config = {"configurable": {"thread_id": "int-notify"}}

    # 预置产物
    os_root = tmp_path / "os_outputs"
    _seed_openstoryline_outputs(os_root, session_id="sid-notify")
    import nodes.node_04_import_and_plan as _m4
    monkeypatch.setattr(_m4, "OPENSTORYLINE_OUTPUTS_ROOT", os_root)

    g = _build_inmemory_graph("int-notify")

    with patch(
        "nodes.node_06_human_reorder._post_resume",
        side_effect=lambda s: {
            **s,
            "reorder_notified": True,
            "status_log": list(s.get("status_log") or []) + ["checkpoint1_resumed"],
        },
    ) as mock_post:
        # 第一次 invoke:应停在关卡⓪ interrupt(不计入 node_06 mock)
        g.invoke(state, config=config)
        assert mock_post.call_count == 0, f"关卡⓪ 之前不应调用 node_06 mock,实际 {mock_post.call_count} 次"

        # resume 关卡⓪ → 走到节点 6 interrupt(仍未 resume,不调用 _post_resume)
        g.invoke(Command(resume=True), config=config)
        assert mock_post.call_count == 0, f"走到节点 6 interrupt 后 _post_resume 仍不应被调用,实际 {mock_post.call_count} 次"

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

    2026-09 迁移后:两个 thread 第一次 invoke 都停在关卡⓪,
    再 resume 关卡⓪ → 关卡① → 乱序 resume 关卡② → END。
    """
    _, draft_file = seeded

    draft_a = tmp_path / "drafts" / "thread_a" / "draft_content.json"
    draft_b = tmp_path / "drafts" / "thread_b" / "draft_content.json"
    _seed_draft(draft_a, 35_000_000)
    _seed_draft(draft_b, 35_000_000)

    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(tmp_path / "drafts"))

    # 预置产物(同一目录,但 node_04 按 session_dir 找)
    os_root = tmp_path / "os_outputs"
    _seed_openstoryline_outputs(os_root, session_id="sid-iso")
    import nodes.node_04_import_and_plan as _m4
    monkeypatch.setattr(_m4, "OPENSTORYLINE_OUTPUTS_ROOT", os_root)

    state_a = _seed_state(str(draft_a))
    state_b = _seed_state(str(draft_b))
    state_a["session_id"] = "thread_a"
    state_b["session_id"] = "thread_b"

    config_a = {"configurable": {"thread_id": "thread_a"}}
    config_b = {"configurable": {"thread_id": "thread_b"}}

    g_a = _build_inmemory_graph("thread_a")
    g_b = _build_inmemory_graph("thread_b")

    # 两个 thread 都跑到关卡⓪ interrupt
    g_a.invoke(state_a, config=config_a)
    g_b.invoke(state_b, config=config_b)
    assert _is_interrupted(g_a, config_a)
    assert _is_interrupted(g_b, config_b)

    # 两个 thread 都 resume 关卡⓪ → 关卡① interrupt
    g_a.invoke(Command(resume=True), config=config_a)
    g_b.invoke(Command(resume=True), config=config_b)
    assert _is_interrupted(g_a, config_a)
    assert _is_interrupted(g_b, config_b)

    # 乱序 resume 关卡① → 关卡② interrupt;b 先到结束, a 后到结束
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


# ---------------------------------------------------------------------------
# 测试 5:关卡⓪(2026-09 迁移解耦新增)— interrupt/resume 后 node_04 读产物
# ---------------------------------------------------------------------------
def _seed_openstoryline_outputs(os_root: Path, *, session_id: str = "sid-int-test") -> Path:
    """预置一份 plan_timeline_pro 产物到 ``<os_root>/<session_id>/plan_timeline_pro/``。

    内容按 plan §4.4 真实 schema:
    ``{"tracks": {"video": [...]}, ...}``,duration 满足 Pydantic 不变量。
    """
    import json

    session_dir = os_root / session_id
    ptp_dir = session_dir / "plan_timeline_pro"
    ptp_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "tracks": {
            "video": [
                {
                    "source_path": "C:/tmp/fake.mp4",
                    "source_window": {"start": 0, "end": 35000_000 // 1000, "duration": 35000_000 // 1000},
                    "timeline_window": {"start": 0, "end": 35000_000 // 1000, "duration": 35000_000 // 1000},
                    "clip_id": "c-int-1",
                    "size": [1920, 1080],
                },
            ],
            "subtitles": [{"text": "集成测试字幕"}],
            "voiceover": [],
            "bgm": [],
        },
    }
    plan_file = ptp_dir / "plan_timeline_pro_int.json"
    plan_file.write_text(
        json.dumps(
            {"payload": plan, "artifact_id": "art-int-1", "session_id": session_id},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_file


def test_checkpoint0_then_resume_to_node_04(
    seeded, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09 新增:关卡⓪ interrupt/resume + node_04 读 openstoryline 产物。

    验证:
    1. 第一次 invoke 停在 ``checkpoint0_storyline_plan``(关卡⓪);
    2. 关卡⓪ payload ``checkpoint == "⓪"``;
    3. resume 关卡⓪ → node_04 读产物 → 写出 ``storyline_plan``;
    4. 之后走正常路径,停在关卡①(节点 6)。
    """
    state, _draft_file = seeded
    state["session_id"] = "int-cp0"
    state["video_input_path"] = "C:/tmp/fake.mp4"
    config = {"configurable": {"thread_id": "int-cp0"}}

    # 预置 openstoryline 产物 + monkeypatch node_04 的 OPENSTORYLINE_OUTPUTS_ROOT
    os_root = tmp_path / "os_outputs"
    _seed_openstoryline_outputs(os_root, session_id="sid-int-test")
    import nodes.node_04_import_and_plan as m4
    monkeypatch.setattr(m4, "OPENSTORYLINE_OUTPUTS_ROOT", os_root)

    # 节点 2 / 3 屏蔽真实进程(_patch_external 已 autouse)

    g = _build_inmemory_graph("int-cp0")

    # 第一次 invoke:应停在 checkpoint0_storyline_plan interrupt
    out = g.invoke(state, config=config)
    assert _is_interrupted(g, config), "应停在 checkpoint0_storyline_plan interrupt"
    snap = g.get_state(config)
    # 关卡⓪ 的 interrupt payload 含 checkpoint="⓪"
    interrupts = list(snap.interrupts) if hasattr(snap, "interrupts") else []
    payloads = [i.value for i in interrupts if hasattr(i, "value")] if interrupts else []
    if not payloads:
        # 兼容旧 API:snap.tasks[].interrupts[*].value
        for t in getattr(snap, "tasks", []) or []:
            for i in getattr(t, "interrupts", []) or []:
                payloads.append(getattr(i, "value", i))
    assert payloads, "应有关卡⓪ 的 interrupt payload"
    assert any(p.get("checkpoint") == "⓪" for p in payloads), f"关卡⓪ payload 应含 checkpoint='⓪',实际 {payloads}"

    # resume 关卡⓪ → 节点 4 读产物 → 节点 5 跳过(已有 draft_path) → 节点 6 interrupt
    g.invoke(Command(resume=True), config=config)
    # 节点 4 成功后写 storyline_plan;后续 节点 6 应再次 interrupt
    assert _is_interrupted(g, config), "resume 关卡⓪ 后应停在节点 6 interrupt"
    # checkpoint0_resumed 已写入状态;用 get_state 拿最新 status_log 验证
    snap_after_cp0 = g.get_state(config)
    log_after_cp0 = (
        snap_after_cp0.values.get("status_log", [])
        if hasattr(snap_after_cp0, "values") and snap_after_cp0.values
        else []
    )
    assert log_after_cp0.count("checkpoint0_resumed") == 1
    assert any("node_04_import_and_plan_done" in s for s in log_after_cp0)

    # 关卡① payload 含 checkpoint="①"
    interrupts_cp1 = list(snap_after_cp0.interrupts) if hasattr(snap_after_cp0, "interrupts") else []
    payloads_cp1 = [i.value for i in interrupts_cp1 if hasattr(i, "value")] if interrupts_cp1 else []
    if not payloads_cp1:
        for t in getattr(snap_after_cp0, "tasks", []) or []:
            for i in getattr(t, "interrupts", []) or []:
                payloads_cp1.append(getattr(i, "value", i))
    assert any(p.get("checkpoint") == "①" for p in payloads_cp1), f"关卡① payload 应含 checkpoint='①',实际 {payloads_cp1}"

    # 再 resume 关卡① → 节点 7 → 关卡② interrupt
    g.invoke(Command(resume=True), config=config)
    assert _is_interrupted(g, config), "应停在节点 12 interrupt"

    # resume 关卡② → END
    final3 = g.invoke(Command(resume=True), config=config)
    assert not _is_interrupted(g, config), "流程应已结束"
    # checkpoint0/1/2 各 resumed 一次(幂等)
    assert final3["status_log"].count("checkpoint0_resumed") == 1
    assert final3["status_log"].count("checkpoint1_resumed") == 1
    assert final3["status_log"].count("checkpoint2_resumed") == 1