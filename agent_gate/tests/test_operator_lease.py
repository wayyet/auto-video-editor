"""线程级操作员租约测试(ZCode 接入设计 §10.5 阶段 D,D-T4)。

覆盖:
- 原子取得 / 续约(D-T2:PostgreSQL 主键 + 事务,并发竞争只有一个赢家)
- 冲突返回稳定错误码 ``operator_already_claimed``(D-T3)
- 消费触点接线:``service.wait_for_work`` / ``service.submit_result`` /
  ``runner.spawn_worker(start / continue_after_crash)``
- 释放路径(D-T4):owner 主动释放、正常退出(atexit 登记表)、
  TTL 过期接管、同主机崩溃进程(PID 已死)立即接管
- ``mode=resume`` 人工放行**永不**被租约拦截

PostgreSQL 不可用 ⇒ 整个套件已被 conftest skip,这里不重复处理。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid

import pytest

from agent_gate import lease, runner, service
from agent_gate.mailbox import Mailbox
from agent_gate.safety import safe_gate_root


@pytest.fixture(autouse=True)
def _stable_owner(monkeypatch):
    """用例内 owner 只由显式参数控制;清掉环境变量干扰。"""
    monkeypatch.delenv("AGENT_GATE_OPERATOR_ID", raising=False)
    monkeypatch.delenv(lease.PID_CHECK_ENV, raising=False)


@pytest.fixture
def tid() -> str:
    """每个用例一个新 thread_id(符合 safe_thread_id 规则)。"""
    return f"lease-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def mb(tmp_path, tid) -> Mailbox:
    return Mailbox(safe_gate_root(tmp_path, tid))


TASK_ID = "t_" + "0" * 16  # 符合 TASK_ID_RE:^t_[0-9a-f]{16}$


def _write_task(mb: Mailbox, task_id: str = TASK_ID) -> None:
    """造一个最小任务(worker 侧写文件的形状)。"""
    mb.tasks_dir.mkdir(parents=True, exist_ok=True)
    (mb.tasks_dir / f"{task_id}.task.json").write_text(json.dumps({
        "task_id": task_id,
        "node": "understand_clips",
        "answer_format": "text",
        "system_prompt": "s",
        "user_prompt": "u",
        "json_schema": None,
        "frames": [],
        "notes": [],
        "attempt_limit": 3,
    }, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# acquire 原语
# --------------------------------------------------------------------------

def test_acquire_then_second_owner_rejected(tid):
    """第一个操作员取得后,第二个活操作员被稳定拒绝。"""
    a = lease.acquire(tid, "op-a", ttl_s=300)
    assert a["ok"] is True and a["action"] == "acquired"

    b = lease.acquire(tid, "op-b", ttl_s=300)
    assert b["ok"] is False
    assert b["code"] == "operator_already_claimed"
    assert b["owner"] == "op-a"
    assert b["expires_at"], "拒绝体里要给 expires_at,消费者才知道还要等多久"


def test_same_owner_renews(tid):
    """同一 owner 再次 acquire = 续约,不是冲突。"""
    assert lease.acquire(tid, "op-a", ttl_s=300)["ok"]
    r = lease.acquire(tid, "op-a", ttl_s=300)
    assert r["ok"] is True and r["action"] == "renewed"
    view = lease.lease_view(tid)
    assert view["owner"] == "op-a"
    assert view["renew_count"] == 1


def test_run_id_kept_on_renew_when_empty(tid):
    """续约时 run_id 传空 → 保留原 run_id;传新值 → 覆盖。"""
    lease.acquire(tid, "op-a", run_id="run-1", ttl_s=300)
    lease.acquire(tid, "op-a", run_id="", ttl_s=300)
    assert lease.lease_view(tid)["run_id"] == "run-1"
    lease.acquire(tid, "op-a", run_id="run-2", ttl_s=300)
    assert lease.lease_view(tid)["run_id"] == "run-2"


def test_expired_lease_taken_over(tid):
    """TTL 过期 → 第二个操作员立即接管(D-T2:崩溃后由 TTL 回收)。"""
    assert lease.acquire(tid, "op-a", ttl_s=0.3)["ok"]
    time.sleep(0.8)
    r = lease.acquire(tid, "op-b", ttl_s=300)
    assert r["ok"] is True and r["action"] == "takeover" and r["reason"] == "expired"
    assert r["previous_owner"] == "op-a"
    assert lease.lease_view(tid)["owner"] == "op-b"


def test_dead_holder_pid_taken_over_immediately(tid):
    """同主机持有人进程已死 → 不等 TTL,立即接管。

    这是「MCode 每轮 exec 一个新进程」「MCP 会话结束后新会话续跑」能
    正常工作的关键(串行消费者不互相挡路)。
    """
    assert lease.acquire(tid, "op-a", ttl_s=900)["ok"]
    # 模拟持有人死亡:把租约行的 PID 改成一个几乎不可能存在的值
    import psycopg

    with psycopg.connect(os.environ["AGENT_GATE_POSTGRES_URI"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {lease.LEASE_TABLE} SET pid = 2147480000 WHERE thread_id = %s",
                (tid,),
            )
    r = lease.acquire(tid, "op-b", ttl_s=900)
    assert r["ok"] is True and r["action"] == "takeover" and r["reason"] == "holder_gone"


def test_alive_holder_blocks_even_without_expiry(tid):
    """持有人进程活着(PID 可见)且没过期 → 拒绝,不能靠 PID 检查抢活人。"""
    assert lease.acquire(tid, "op-a", ttl_s=900)["ok"]
    import psycopg

    with psycopg.connect(os.environ["AGENT_GATE_POSTGRES_URI"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {lease.LEASE_TABLE} SET pid = %s WHERE thread_id = %s",
                (os.getpid(), tid),  # 当前测试进程,一定活着
            )
    r = lease.acquire(tid, "op-b", ttl_s=900)
    assert r["ok"] is False and r["code"] == "operator_already_claimed"


def test_pid_check_disabled_falls_back_to_ttl(tid, monkeypatch):
    """``AGENT_GATE_LEASE_PID_CHECK=0`` → 关闭快速回收,只剩 TTL 一条路。"""
    monkeypatch.setenv(lease.PID_CHECK_ENV, "0")
    assert lease.acquire(tid, "op-a", ttl_s=900)["ok"]
    import psycopg

    with psycopg.connect(os.environ["AGENT_GATE_POSTGRES_URI"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {lease.LEASE_TABLE} SET pid = 2147480000 WHERE thread_id = %s",
                (tid,),
            )
    r = lease.acquire(tid, "op-b", ttl_s=900)
    assert r["ok"] is False and r["code"] == "operator_already_claimed"


def test_release_only_own(tid):
    """只能释放自己的租约;释放后别人立即可取得。"""
    lease.acquire(tid, "op-a", ttl_s=900)
    assert lease.release(tid, "op-b")["released"] is False
    assert lease.release(tid, "op-a")["released"] is True
    assert lease.lease_view(tid) is None
    assert lease.acquire(tid, "op-b", ttl_s=900)["ok"]


def test_release_owned_leases_clears_process_registry(tid):
    """release_owned_leases 按进程登记表释放(acquire 默认登记)。"""
    t2 = tid + "-x"
    lease.acquire(tid, "op-a", ttl_s=900)
    lease.acquire(t2, "op-a", ttl_s=900)
    n = lease.release_owned_leases()
    assert n >= 2
    assert lease.lease_view(tid) is None
    assert lease.lease_view(t2) is None
    # 再次调用无事可做
    assert lease.release_owned_leases() == 0


def test_concurrent_acquire_single_winner(tid):
    """并发竞争:同一线程 N 个操作员同时抢,恰好一个成功(D-T2 原子性)。"""
    n = 8
    barrier = threading.Barrier(n)
    results: list[dict] = [{}] * n

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = lease.acquire(tid, f"op-{i}", ttl_s=300)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r.get("ok")]
    assert len(winners) == 1, f"应当恰好一个赢家,实际 {len(winners)}:{results}"
    losers = [r for r in results if not r.get("ok")]
    assert losers and all(r["code"] == "operator_already_claimed" for r in losers)


# --------------------------------------------------------------------------
# 消费触点接线(D-T3)
# --------------------------------------------------------------------------

def test_wait_for_work_rejects_second_consumer(mb, tid):
    """gate_next_tasks 路径:第二个消费者拿到 operator_already_claimed + 空 tasks。"""
    r1 = service.wait_for_work(mb, thread_id=tid, wait_s=0, operator="op-a")
    assert r1.get("code") != "operator_already_claimed"

    r2 = service.wait_for_work(mb, thread_id=tid, wait_s=0, operator="op-b")
    assert r2["ok"] is False
    assert r2["code"] == "operator_already_claimed"
    assert r2["tasks"] == []
    assert r2["owner"] == "op-a"


def test_wait_for_work_same_owner_still_works(mb, tid):
    """单消费者流程不受影响:同一 owner 反复取任务 = 续约(D-T5 回归点)。"""
    for _ in range(3):
        r = service.wait_for_work(mb, thread_id=tid, wait_s=0, operator="op-a")
        assert r.get("code") != "operator_already_claimed"


def test_submit_result_guards_lease(mb, tid):
    """submit 路径:别人的活租约 → 拒绝提交(被夺走消费权的消费者不能继续写)。"""
    _write_task(mb)
    # op-b 先持有(模拟它是当前消费者)
    assert service.wait_for_work(mb, thread_id=tid, wait_s=0, operator="op-b")  # 取得
    res = service.submit_result(mb, TASK_ID, "答案", operator="op-a")
    assert res["ok"] is False and res["code"] == "operator_already_claimed"
    assert mb.read_result(TASK_ID) is None, "被拒的提交不该落盘"

    # 当前消费者自己提交 → 正常
    res = service.submit_result(mb, TASK_ID, "答案", operator="op-b")
    assert res["ok"] is True


def test_submit_batch_guards_lease(mb, tid):
    """submit_batch 走同一个守卫:其中一个被拒,整体 ok=False。"""
    _write_task(mb)
    service.wait_for_work(mb, thread_id=tid, wait_s=0, operator="op-b")
    res = service.submit_batch(mb, {TASK_ID: "答案"}, operator="op-a")
    assert res["ok"] is False
    assert res["results"][TASK_ID]["code"] == "operator_already_claimed"


def test_spawn_worker_start_rejected_when_other_holds(tmp_path, tid):
    """run_start 路径:其他活操作员持有租约 → start 被拒,不拉起 worker。"""
    mb = Mailbox(safe_gate_root(tmp_path, tid))
    assert lease.acquire(tid, "op-a", ttl_s=900)["ok"]

    init = mb.root / "initial_state.json"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(json.dumps({"session_id": tid, "video_input_path": "x.mp4"}),
                    encoding="utf-8")
    res = runner.spawn_worker(thread_id=tid, outputs_root=tmp_path,
                              graph_factory="graph:build_graph",
                              initial_state_file=init, mode="start", operator="op-b")
    assert res["ok"] is False and res["code"] == "operator_already_claimed"
    assert mb.read_status() is None, "被拒的启动不该写 starting 状态"


def test_spawn_worker_continue_allowed_for_same_operator(tmp_path, tid):
    """同一操作员先消费再 continue_after_crash → 不被自己挡住(恢复路径)。"""
    mb = Mailbox(safe_gate_root(tmp_path, tid))
    # 模拟 op-x 是消费者(它持有租约;这里是测试进程,PID 活着)
    r = service.wait_for_work(mb, thread_id=tid, wait_s=0, operator="op-x")
    assert r.get("code") != "operator_already_claimed"

    # continue_after_crash 的前置状态:worker crashed + 心跳新鲜
    from agent_gate.protocol import utc_now

    mb.write_status({"thread_id": tid, "state": "crashed", "pid": 12345,
                     "started_at": utc_now(), "heartbeat_at": utc_now(),
                     "interrupts": [], "error": None})

    # 同一操作员尝试 continue_after_crash:check_mode 会因「无 checkpoint」拒绝,
    # 但拒绝码必须是 no_checkpoint(走到了状态机),而不是 operator_already_claimed
    res = runner.spawn_worker(thread_id=tid, outputs_root=tmp_path,
                              graph_factory="graph:build_graph",
                              mode="continue_after_crash", operator="op-x")
    assert res.get("code") == "no_checkpoint"


def test_spawn_worker_resume_bypasses_lease(tmp_path, tid):
    """人工放行(resume)永不因租约被拦 —— 停在人工关卡的线程必须能由人接管。"""
    mb = Mailbox(safe_gate_root(tmp_path, tid))
    assert lease.acquire(tid, "op-agent", ttl_s=900)["ok"]

    # 伪造 waiting_human 状态,让 check_mode 的 resume 分支走到租约检查之后
    mb.write_status({"thread_id": tid, "state": "waiting_human", "pid": None,
                     "started_at": "2026-01-01T00:00:00Z",
                     "heartbeat_at": "2026-01-01T00:00:00Z",
                     "interrupts": [{"checkpoint": "①", "step": 1}],
                     "error": None})
    res = runner.spawn_worker(thread_id=tid, outputs_root=tmp_path,
                              graph_factory="graph:build_graph",
                              resume_values={"①": True}, mode="resume")
    # resume 需要 checkpoint 存在才能真跑;这里没有 checkpoint ⇒ 停在
    # 后续的 spawn 阶段(worker 会失败),但**绝不能**是租约拒绝码。
    assert res.get("code") != "operator_already_claimed"


def test_status_and_get_task_not_gated(mb, tid):
    """只读入口(run_status / gate_get_task / events)不被租约拦。"""
    lease.acquire(tid, "op-a", ttl_s=900)
    st = service.status_summary(mb, thread_id=tid)
    assert st["thread_id"] == tid
    assert service.get_task(mb, "t1") is None  # 没有任务,但调用本身不被拒


# --------------------------------------------------------------------------
# owner 身份
# --------------------------------------------------------------------------

def test_owner_id_env_overrides(monkeypatch):
    monkeypatch.setenv("AGENT_GATE_OPERATOR_ID", "fixed-operator")
    assert lease.owner_id() == "fixed-operator"
    assert lease.owner_id(default="cli") == "fixed-operator"


def test_owner_id_default_used_without_env(monkeypatch):
    monkeypatch.delenv("AGENT_GATE_OPERATOR_ID", raising=False)
    assert lease.owner_id(default="cli") == "cli"


def test_owner_id_auto_is_process_stable(monkeypatch):
    """不传 default 时,同一进程内多次调用拿到**同一个**自动 ID。"""
    monkeypatch.delenv("AGENT_GATE_OPERATOR_ID", raising=False)
    a = lease.owner_id()
    b = lease.owner_id()
    assert a == b
    assert a != lease.owner_id(default="cli")


# --------------------------------------------------------------------------
# 跨进程实测(D-T5):两个真实 MCP server 子进程 = 两个消费者
# --------------------------------------------------------------------------
# MCode / Claude Code / ZCode 三类消费者的入口是**同一个**二进制 —— 各自的
# .mcp.json 都拉起 `python -m agent_gate.mcp_server`,每会话一个进程、一个
# 自动 owner。所以「两个 MCP 子进程」在协议层就等价于「两个不同 Agent 消费者」。

def test_mcp_cross_process_lease_takeover(tmp_path):
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    outputs, ckpt = tmp_path / "o", tmp_path / "c"

    def params() -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "agent_gate.mcp_server", "--outputs-root", str(outputs),
                  "--graph-factory", "toy_graph:build_graph"],
            env=dict(os.environ, PYTHONUTF8="1"),
        )

    async def call(tool: str, args: dict) -> dict:
        from mcp.types import TextContent

        async with stdio_client(params()) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(tool, args)
                blocks = [c for c in res.content if isinstance(c, TextContent)]
                return json.loads(blocks[0].text)

    async def scenario() -> tuple[dict, dict, dict]:
        from mcp.types import TextContent

        # 消费者1 的连接**整个场景期间保持打开**(进程活着 = 租约有效)
        async with stdio_client(params()) as (r1, w1):
            async with ClientSession(r1, w1) as s1:
                await s1.initialize()
                # 只读入口不受租约影响
                st_res = await s1.call_tool("run_status", {"thread_id": "xlease1"})
                st = json.loads([c for c in st_res.content if isinstance(c, TextContent)][0].text)
                # 消费者1 取得租约
                g1_res = await s1.call_tool("gate_next_tasks", {"thread_id": "xlease1", "wait_s": 0})
                g1 = json.loads([c for c in g1_res.content if isinstance(c, TextContent)][0].text)
                # 消费者1 仍活着时,消费者2(另一个进程 = 另一个自动 owner)被拒
                g2 = await call("gate_next_tasks", {"thread_id": "xlease1", "wait_s": 0})
                return st, g1, g2

    st, g1, g2 = asyncio.run(scenario())
    assert st["thread_id"] == "xlease1"
    assert g1.get("code") != "operator_already_claimed"
    assert g2["ok"] is False and g2["code"] == "operator_already_claimed"
    assert g2.get("owner"), "拒绝体要暴露当前持有人,消费者才知道是谁在消费"

    # 消费者1 已退出(正常退出 atexit 释放 / 强杀走 PID 存活检测)→ 消费者2 接管
    deadline = time.monotonic() + 30
    last: dict = {}
    while time.monotonic() < deadline:
        last = asyncio.run(call("gate_next_tasks", {"thread_id": "xlease1", "wait_s": 0}))
        if last.get("code") != "operator_already_claimed":
            break
        time.sleep(0.5)
    assert last.get("code") != "operator_already_claimed", \
        f"消费者1 退出后 30s 内消费者2 未能接管:{last}"
