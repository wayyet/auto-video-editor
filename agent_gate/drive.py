"""无人值守驱动:反复拉起 Agent 处理待办任务,直到流水线完成,或停在人工关卡。

为什么要这一层(而不是让一个 Agent 会话一直循环):
    - 每个会话的上下文会越攒越大(每个视频任务带 ~6 张图)。
    - 会话会因为 --max-turns / --max-steps / 超时 / 额度而结束。
    - 任务协议是无状态的(task_id 由请求内容哈希而来),所以「结束了就再拉起一个」天然安全。

退出码(CLI 用):0 完成;1 流水线失败;2 缺少输入;3 超出预算;4 Agent 没进展;
               5 worker 崩溃且不能自动恢复;10 停在人工关卡,等真人。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from agent_gate import runner, service
from agent_gate.launcher import AgentLauncher, summarize_output
from agent_gate.mailbox import Mailbox

EXIT_CODES = {"done": 0, "failed": 1, "need_input": 2, "budget": 3, "stalled": 4,
              "crashed": 5, "ask_human": 10}

BATCH_PROMPT = (
    "你是 video-agent 流水线的「大模型」。thread_id={thread_id}。\n"
    "只做一件事:答题。步骤:\n"
    "1. 调 gate_next_tasks(thread_id=\"{thread_id}\", wait_s=5, limit={batch}) 取待办任务。\n"
    "2. 对返回的每个任务:读它的 prompt 和图片,按要求产出答案,调 gate_submit_result 提交。\n"
    "3. 本会话累计答完 {batch} 个任务,或者取不到任务,就立刻结束。\n"
    "   返回里 deferred_tasks 大于 0 表示还有任务因为图片太多没给你:累计不到 {batch} 个时,再取一次。\n"
    "不要等人工关卡,不要调用任何 resume,不要启动新运行。\n"
)


def drive(
    *,
    thread_id: str,
    outputs_root: str | Path,
    graph_factory: str,
    launcher: AgentLauncher,
    initial_state_file: Optional[Path] = None,
    batch: int = 5,
    max_sessions: int = 40,
    max_wall_s: float = 7200.0,
    poll_s: float = 2.0,
    stall_limit: int = 2,
    auto_restart: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    mb = Mailbox(runner.gate_root(outputs_root, thread_id))
    t0 = clock()
    sessions = 0
    stalled = 0
    restarts = 0

    def finish(outcome: str, **extra: Any) -> dict[str, Any]:
        st = service.status_summary(mb, thread_id=thread_id)
        mb.log_event("drive_finished", outcome=outcome, sessions=sessions)
        return {"outcome": outcome, "exit_code": EXIT_CODES[outcome], "sessions": sessions,
                "status": st, **extra}

    while True:
        st = service.status_summary(mb, thread_id=thread_id)
        action = st["next_action"]

        if action == "done":
            return finish("done")
        if action == "ask_human":
            return finish("ask_human")
        if action == "report_failure":
            return finish("failed")

        if action == "start_run":
            if initial_state_file is None:
                return finish("need_input", message="还没有这个 thread 的运行记录:请给初始 state 文件或 --video")
            res = runner.spawn_worker(thread_id=thread_id, outputs_root=outputs_root,
                                      graph_factory=graph_factory, initial_state_file=initial_state_file)
            if not res["ok"] and res.get("code") != "already_running":
                return finish("failed", message=str(res))
            continue

        if action == "restart_worker":
            if not auto_restart or restarts >= 1:
                return finish("crashed", message="worker 心跳过期,且已自动恢复过 1 次或被禁用")
            restarts += 1
            mb.log_event("drive_restart_worker")
            runner.spawn_worker(thread_id=thread_id, outputs_root=outputs_root,
                                graph_factory=graph_factory)   # 不给初始 state:从最近检查点继续
            continue

        if clock() - t0 > max_wall_s:
            return finish("budget", message=f"超过 max_wall_s={max_wall_s:.0f}")

        if action == "answer_tasks":
            if sessions >= max_sessions:
                return finish("budget", message=f"超过 max_sessions={max_sessions}")
            sessions += 1
            before = st["answered_tasks"]
            res = launcher.run(BATCH_PROMPT.format(thread_id=thread_id, batch=batch))
            sdir = mb.root / "agent_sessions"
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / f"{sessions:03d}.out.txt").write_text(res.stdout[-6000:], encoding="utf-8")
            (sdir / f"{sessions:03d}.err.txt").write_text(res.stderr[-3000:], encoding="utf-8")
            after = service.status_summary(mb, thread_id=thread_id)["answered_tasks"]
            mb.log_event("agent_session", n=sessions, returncode=res.returncode, timed_out=res.timed_out,
                         answered_before=before, answered_after=after,
                         **summarize_output(launcher.name, res.stdout))
            stalled = 0 if after > before else stalled + 1
            if stalled >= stall_limit:
                return finish("stalled", message=f"连续 {stalled} 个 Agent 会话没有提交任何答案,"
                                                 f"见 {sdir}")
            continue

        sleep(poll_s)   # action == "wait":worker 在算,没有待办任务
