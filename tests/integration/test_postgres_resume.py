"""Postgres checkpointer 跨实例恢复集成测试(PostgreSQL 统一改造方案 §7.2 / 阶段 C)。

为什么需要这个文件
------------------
阶段 C 之前,"跨进程恢复"(新进程 + 同一个存档接管挂起任务)是用 SQLite
后端验证的:测试另开一个 saver 连同一个 ``.sqlite`` 文件,模拟"新进程接管
旧任务"。SQLite 后端删除后,这条覆盖必须落到**真实 Postgres** ——
只有 Postgres 能同时满足"新连接"与"持久化"。

约定与 ``tests/integration/test_postgres_checkpointer.py`` 完全一致:
- 独立环境变量 ``TEST_POSTGRES_URI`` 优先,其次 ``POSTGRES_URI``,
  都没有则用 ``config.POSTGRES_URI``。
- **显式**给了 URI ⇒ Postgres 测试必须真跑,连不上直接 fail(杜绝"假绿")。
- 没给且默认库不可达 ⇒ 带 ``postgres`` 标记 skip。
- Windows 默认事件循环是 ProactorEventLoop,psycopg 异步不支持
  (``Psycopg cannot use the 'ProactorEventLoop' to run in async mode``),
  故显式用 SelectorEventLoop。

覆盖场景
--------
1. 最小图:interrupt → 退出 saver context → **新开** saver → 重新 compile
   → 中断态仍在 → resume 能跑完。
2. 真实工作流图:跑到关卡①挂起 → 退出 saver context → **新开** saver
   → 重新 compile → 关卡① 的 interrupt payload 仍在 → resume 后推进到关卡②。

两个用例都用 ``uuid4().hex`` 生成唯一 thread_id,彼此(以及与生产数据)
不会撞车。

⚠️ 关于"resume 之后按原 thread_id 再读一次状态"
------------------------------------------------
本机 ``video_workflow`` 库**存储**编码是 ``SQL_ASCII``。如果 ``client_encoding``
也跟着走 ``SQL_ASCII``,psycopg3 对 TEXT 列返回 ``bytes`` ⇒ LangGraph 在 resume 时
读回的 config 里 ``thread_id`` 也是 ``bytes`` ⇒ 再写回 TEXT 列时 psycopg 把它渲染成
``\\x<hex>`` 字面量 ⇒ **resume 产生的新 checkpoint 落在另一个 thread_id 桶里**
(方案 §3.1 M2 记的"线程分裂")。

✅ **这个问题已经修掉了**,修法是迁移 ``003_set_client_encoding_utf8``
(``ALTER DATABASE ... SET client_encoding TO 'UTF8'``):新连接默认 UTF-8 ⇒ TEXT 列
返回 ``str`` ⇒ thread_id 写回去还是文本版。

⇒ 本文件**可以**直接断言"resume 之后再按原 thread_id 读快照",
用来锁住这个行为不再退化(见 ``test_resume_does_not_split_thread_id``)。
"""

from __future__ import annotations

import asyncio
import json
import os
import selectors
import socket
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


# ---------------------------------------------------------------------------
# Postgres 连通性约定(与 test_postgres_checkpointer.py 一致)
# ---------------------------------------------------------------------------
def _default_test_uri() -> str:
    """默认测试 URI = ``config.POSTGRES_URI``(库名 video_workflow)。"""
    try:
        from config import POSTGRES_URI

        return POSTGRES_URI
    except Exception:  # pragma: no cover - config 不可导入时的兜底
        return "postgresql://postgres:devpass@localhost:5432/video_workflow"


# 显式指定 ⇒ 必须真跑(连不上就 fail);未指定 ⇒ 允许带标记 skip。
TEST_URI = (
    os.environ.get("TEST_POSTGRES_URI")
    or os.environ.get("POSTGRES_URI")
    or _default_test_uri()
)
_URI_IS_EXPLICIT = bool(
    os.environ.get("TEST_POSTGRES_URI") or os.environ.get("POSTGRES_URI")
)


def _postgres_reachable(uri: str, timeout: float = 1.5) -> bool:
    """快速判断 Postgres URI 是否可达(避免每个测试都等连接超时)。

    只解析 host:port 做 TCP connect 测试。
    """
    try:
        # postgresql://user:pass@host:port/dbname
        from urllib.parse import urlparse

        parsed = urlparse(uri)
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        (not _URI_IS_EXPLICIT) and not _postgres_reachable(TEST_URI),
        reason=(
            f"Postgres not reachable at {TEST_URI}(本机未启或 URI 不通)— "
            "跳过 Postgres 集成测试;显式设置 TEST_POSTGRES_URI 可让连不上直接 fail"
        ),
    ),
]


@pytest.fixture()
def postgres_uri() -> str:
    """保证 Postgres 可用;显式指定 URI 时连不上直接 fail(不静默 skip)。"""
    if not _postgres_reachable(TEST_URI):
        if _URI_IS_EXPLICIT:
            pytest.fail(
                f"显式指定了 Postgres URI,但目标不可达: {TEST_URI} — "
                "Postgres 集成测试不允许静默 skip(方案 §7.2)"
            )
        pytest.skip(f"Postgres not reachable at {TEST_URI}")
    return TEST_URI


def _run_async(coro):
    """在 SelectorEventLoop 上跑协程。

    Windows 默认事件循环是 ProactorEventLoop,psycopg 异步不可用
    (``Psycopg cannot use the 'ProactorEventLoop' to run in async mode``),
    必须显式换 SelectorEventLoop —— 与 ``scripts/run_workflow.py`` 同一套写法。
    """
    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 场景 1:最小图 —— 新 saver 接管旧存档
# ---------------------------------------------------------------------------
class _MiniState(TypedDict):
    value: str
    step: int


def _mini_node(state: _MiniState) -> _MiniState:
    """一个会 interrupt 的节点。"""
    answer = interrupt({"ask": "继续?"})
    return {"value": f"{state['value']}:{answer}", "step": state["step"] + 1}


def _compile_mini(saver: Any):
    g = StateGraph(_MiniState)
    g.add_node("ask", _mini_node)
    g.add_edge(START, "ask")
    g.add_edge("ask", END)
    return g.compile(checkpointer=saver)


def test_postgres_state_survives_new_saver(postgres_uri: str) -> None:
    """interrupt → 退出 saver → **新开** saver → 重新 compile → 中断态还在 → resume 跑完。

    这条是 SQLite 后端删除后"跨进程恢复"最直接的替身:
    第二个 saver 是**全新的数据库连接**,但仍能读到上一个连接写入的中断态。
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    thread_id = f"pg-resume-mini-{uuid4().hex}"
    config = {"configurable": {"thread_id": thread_id}}

    async def _body() -> None:
        # ---- 第一段:跑到 interrupt,然后退出 saver context(等价于进程退出) ----
        async with AsyncPostgresSaver.from_conn_string(postgres_uri) as saver:
            await saver.setup()
            g1 = _compile_mini(saver)
            await g1.ainvoke({"value": "start", "step": 0}, config=config)

            snap1 = await g1.aget_state(config)
            assert snap1.interrupts, "第一段应停在 interrupt"
            assert snap1.values.get("value") == "start"

        # ---- 第二段:新开 saver(新连接),重新 compile ----
        async with AsyncPostgresSaver.from_conn_string(postgres_uri) as saver2:
            await saver2.setup()
            g2 = _compile_mini(saver2)

            # 核心断言:新连接仍能看到上一个连接留下的中断态
            snap2 = await g2.aget_state(config)
            assert snap2.interrupts, "新 saver 应仍能看到 interrupt"
            assert snap2.next, "新 saver 应仍知道下一个待执行节点"
            assert snap2.values.get("value") == "start"

            # resume 能真正把图跑完(挂起的任务被接着执行,而不是从头再来)
            out = await g2.ainvoke(Command(resume="ok"), config=config)
            assert out["value"] == "start:ok", f"resume 后应继续跑完,实际 {out!r}"
            assert out["step"] == 1

    _run_async(_body())


# ---------------------------------------------------------------------------
# 场景 2:真实工作流图 —— 新 saver 接管关卡① 的挂起任务
# ---------------------------------------------------------------------------
class _StubProc:
    def __init__(self, pid: int = 22222) -> None:
        self.pid = pid


@pytest.fixture(autouse=True)
def _patch_external(monkeypatch: pytest.MonkeyPatch) -> None:
    """屏蔽节点 2/3 的真实进程拉起(与 test_week5_resilience.py 同款)。"""
    import nodes.node_02_launch_openstoryline as m2
    import nodes.node_03_open_preview as m3

    monkeypatch.setattr(m2, "_wait_for_ready", lambda url, t: True)
    monkeypatch.setattr(
        m3,
        "subprocess",
        type("S", (), {"Popen": staticmethod(lambda *a, **k: _StubProc())}),
    )


def _seed_draft(draft_dir: Path, duration_us: int = 35_000_000) -> Path:
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_file = draft_dir / "draft_content.json"
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": duration_us,
        "materials": {"videos": [{"id": "v1"}]},
        "tracks": [{
            "type": "video",
            "fps": 30,
            "segments": [{"id": "s1", "target_timerange": {"start": 0, "duration": duration_us}}],
        }],
    }
    draft_file.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return draft_file


def _seed_state(draft_path: str) -> dict[str, Any]:
    return {
        "session_id": "pg-resume",
        "video_input_path": "C:/tmp/fake.mp4",
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


def test_postgres_workflow_resume_after_new_saver(
    postgres_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实工作流图:关卡① 挂起 → 退出 saver → 新开 saver → 重新 compile → 仍能 resume。

    这是旧的 ``test_resume_all_three_checkpoints_via_sqlite_persistence``
    的 Postgres 版:旧版用"第二个本地文件 saver 连同一个文件"模拟进程重启,
    现在用"第二个 AsyncPostgresSaver(新连接)连同一个库"。
    """
    from graph import build_graph, get_checkpointer
    from jy_common.translate_client import MockTranslateClient, set_default_client

    # get_checkpointer() 读 env 的 POSTGRES_URI —— 对齐到 fixture 解析出的库
    monkeypatch.setenv("POSTGRES_URI", postgres_uri)
    set_default_client(MockTranslateClient())

    draft_dir = tmp_path / "drafts" / "demo"
    draft_file = _seed_draft(draft_dir)
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(draft_dir))

    state = _seed_state(str(draft_file))
    thread_id = f"pg-resume-wf-{uuid4().hex}"
    config = {"configurable": {"thread_id": thread_id}}

    async def _body() -> None:
        # ---- 第一段:跑到关卡① 挂起,然后退出 saver context ----
        async with get_checkpointer() as saver:
            g1 = build_graph(
                checkpointer=saver,
                thread_id=thread_id,
                start_heartbeat_thread=False,
            )
            await g1.ainvoke(state, config=config)

            snap1 = await g1.aget_state(config)
            assert snap1.interrupts, "关卡① 应挂起"
            assert snap1.interrupts[0].value["checkpoint"] == "①"

        # ---- 第二段:新开 saver(新连接)+ 重新 compile ----
        async with get_checkpointer() as saver2:
            g2 = build_graph(
                checkpointer=saver2,
                thread_id=thread_id,
                start_heartbeat_thread=False,
            )

            # 核心断言:新连接仍能看到上一个连接留下的关卡① 中断态
            snap2 = await g2.aget_state(config)
            assert snap2.interrupts, "新 saver 应仍能看到关卡① 的 interrupt"
            assert snap2.interrupts[0].value["checkpoint"] == "①"
            assert tuple(snap2.next) == ("node_06_human_reorder",)

            # resume 关卡① → 应推进到关卡②
            out = await g2.ainvoke(Command(resume=True), config=config)

            interrupts = out.get("__interrupt__") or []
            assert interrupts, "resume 关卡① 后应在关卡② 挂起"
            assert interrupts[0].value["checkpoint"] == "②"

            # 节点 5 不应被重跑;关卡① 的 resume 标记应写入
            log = list(out.get("status_log") or [])
            assert log.count("node_05_generate_draft_done") == 1
            assert log.count("checkpoint1_resumed") == 1

    _run_async(_body())


# ---------------------------------------------------------------------------
# 场景 3:resume 之后**不能**产生 \x<hex> 版 thread_id(方案 §3.1 M2)
# ---------------------------------------------------------------------------
def test_resume_does_not_split_thread_id(postgres_uri: str) -> None:
    """resume 之后,``checkpoints`` 表里只应有文本那一套 thread_id。

    背景:本机库**存储**编码是 ``SQL_ASCII``。若 ``client_encoding`` 也跟着走
    ``SQL_ASCII``,psycopg3 对 TEXT 列返回 ``bytes`` ⇒ LangGraph resume 时读回的
    ``thread_id`` 是 bytes ⇒ 写回 TEXT 列时被渲染成 ``\\x<hex>`` 字面量
    ⇒ 同一逻辑线程分裂成两套(thread_id 查不到续跑的那一半)。

    修法是迁移 ``003_set_client_encoding_utf8``(库级 ``client_encoding=UTF8``);
    本用例锁住这个行为不再退化。
    """
    import psycopg

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    thread_id = f"pg-resume-split-{uuid4().hex}"
    config = {"configurable": {"thread_id": thread_id}}

    async def _body() -> None:
        async with AsyncPostgresSaver.from_conn_string(postgres_uri) as saver:
            await saver.setup()
            g = _compile_mini(saver)
            await g.ainvoke({"value": "start", "step": 0}, config=config)
            snap = await g.aget_state(config)
            assert snap.interrupts, "最小图应在 interrupt 处挂起"

        # 新连接 + 重新 compile → resume
        async with AsyncPostgresSaver.from_conn_string(postgres_uri) as saver2:
            await saver2.setup()
            g2 = _compile_mini(saver2)
            out = await g2.ainvoke(Command(resume="ok"), config=config)
            assert out["value"] == "start:ok"

    _run_async(_body())

    hex_literal = "\\x" + thread_id.encode("utf-8").hex()
    with psycopg.connect(postgres_uri, client_encoding="utf-8") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT thread_id, count(*) FROM checkpoints"
                " WHERE thread_id = %s OR thread_id = %s GROUP BY 1",
                (thread_id, hex_literal),
            )
            rows = cur.fetchall()
    hex_rows = [r for r in rows if str(r[0]).startswith("\\x")]
    assert not hex_rows, (
        f"resume 后出现了 hex 版 thread_id:{hex_rows} —— "
        "client_encoding 没生效,TEXT 列返回了 bytes"
    )
    text_rows = [r for r in rows if str(r[0]) == thread_id]
    assert text_rows, f"文本版 thread_id 不见了:{rows}"
