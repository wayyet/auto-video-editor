"""直接 CLI checkpoint 恢复入口(执行计划 §4.2 任务 3-5:补齐入口 A 的 resume 能力)。

用法
--------

.. code-block:: powershell

    # 只查看挂起关卡,不恢复(等价 agent_gate status 的直接 CLI 版)
    .\\.venv\\Scripts\\python.exe scripts\\resume_workflow.py --thread-id prod-001 --dry-run

    # 恢复指定关卡(推荐显式给 --checkpoint)
    .\\.venv\\Scripts\\python.exe scripts\\resume_workflow.py --thread-id prod-001 --checkpoint ① --value true

    # 唯一挂起关卡时可省略 --checkpoint
    .\\.venv\\Scripts\\python.exe scripts\\resume_workflow.py --thread-id prod-001 --value true

语义与边界(执行计划 §3.5 / §4.2)
--------
- **恢复管线与启动入口一致**:Postgres checkpointer + ``monitoring.workflow_runner.
  run_with_events``,因此 resume 同样产出 ``run.resumed`` / ``node.resumed`` /
  终态事件,监控页与 SSE 无差别消费(§4.2 任务 2/5)。
- **恢复前校验**:先 ``graph.aget_state(config)`` 读取挂起信息,校验指定
  checkpoint(关卡标识 ``①/②/③`` 或 legacy_id)确实属于该 thread,不匹配即
  fail-closed(退出码 5),绝不盲目注入 ``Command(resume=...)``(§4.2 任务 4)。
- **入口不混用**(§3.5 结论):本脚本只恢复**直接 CLI** 启动的运行;
  ``agent_gate run_start / drive`` 启动的运行仍由 ``agent_gate resume`` 恢复
  (worker 状态机 + mailbox)。两条路径共享 PostgreSQL checkpoint 与事件表,
  但不共享控制状态,跨入口恢复不受支持。
- resume value 传给 ``interrupt()`` 的值:关卡 ①/②/③ 均按布尔语义消费
  (节点 docstring:``Command(resume=True)``);默认 ``true``。

退出码
--------
- ``0``  成功(含"恢复后停在下一道人工关卡"的正常挂起;最终 state 有
  ``error_log`` 时逐行打印到 stderr,便于定位);
- ``1``  图执行异常(run_with_events 先发 ``run.failed`` 再抛出);
- ``2``  pre-flight 失败 / 参数非法;
- ``4``  该 thread 无挂起关卡可恢复;
- ``5``  指定 checkpoint 与挂起关卡不匹配(或多重挂起未指定)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

# 让 ``import graph`` / ``import runtime.preflight`` 能解析到项目根
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 默认 stdout 是 GBK;强制 UTF-8 以免中文/符号乱码或报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

EXIT_OK = 0
EXIT_GRAPH_ERROR = 1
EXIT_PREFLIGHT = 2
EXIT_NOT_SUSPENDED = 4
EXIT_CHECKPOINT_MISMATCH = 5


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    p = argparse.ArgumentParser(
        prog="resume_workflow",
        description="直接 CLI 的 checkpoint 恢复入口(与 run_workflow --production 同管线)",
    )
    p.add_argument("--thread-id", required=True, help="LangGraph thread_id(必填)")
    p.add_argument(
        "--checkpoint",
        default=None,
        help=(
            "要恢复的关卡标识:interrupt payload 的 checkpoint(①/②/③)或 "
            "legacy_id(如 checkpoint2_add_bgm)。唯一挂起时可省略;多重挂起时必填"
        ),
    )
    p.add_argument(
        "--value",
        default="true",
        help="传给 interrupt() 的恢复值(JSON 解析,默认 true)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出挂起关卡,不执行恢复(操作员先核对再放行)",
    )
    p.add_argument(
        "--skip-preflight",
        action="store_true",
        help="跳过 pre-flight 检查(测试/CI 用,生产请勿使用)",
    )
    p.add_argument(
        "--production",
        action="store_true",
        help="等价于 WORKFLOW_ENV=production(与 run_workflow --production 对齐)",
    )
    p.add_argument(
        "--skip-preflight-schema-setup",
        action="store_true",
        help="跳过 checkpoint 表创建(默认会确保 schema 存在)",
    )
    return p.parse_args()


def parse_resume_value(raw: str) -> Any:
    """把 ``--value`` 字符串解析成传给 ``interrupt()`` 的 Python 值。

    与 agent_gate CLI 的 ``--value`` 语义一致(「传给 interrupt 的值(JSON)」):
    优先 ``json.loads``;失败时退回布尔字面量,再退回原始字符串。
    独立成纯函数便于单测。
    """
    text = str(raw)
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        lowered = text.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        return text


def describe_interrupts(snapshot: Any) -> list[dict[str, Any]]:
    """把 state snapshot 的挂起 interrupt 归一成可打印/可匹配的 dict 列表。

    字段与 ``agent_gate.runner.describe_interrupts`` 对齐(id / checkpoint /
    step / instructions / payload),另含 ``legacy_id`` —— 直接 CLI 允许用旧名
    指定关卡(与 payload 契约字段一致,见 ``nodes/_checkpoint.py``)。
    """
    out: list[dict[str, Any]] = []
    for i in list(getattr(snapshot, "interrupts", None) or []):
        v = i.value if isinstance(i.value, dict) else {"value": i.value}
        out.append(
            {
                "id": i.id,
                "checkpoint": v.get("checkpoint"),
                "legacy_id": v.get("legacy_id"),
                "step": v.get("step"),
                "instructions": v.get("instructions"),
                "payload": v,
            }
        )
    return out


def match_interrupt(
    interrupts: list[dict[str, Any]], checkpoint: Optional[str]
) -> Optional[dict[str, Any]]:
    """按 ``--checkpoint`` 选出要恢复的挂起关卡;选不出返回 ``None``。

    匹配顺序:payload ``checkpoint``(①/②/③)→ ``legacy_id`` → interrupt ``id``。
    ``checkpoint`` 为空时:恰好一个挂起 → 该关卡;0 个或多个 → ``None``(歧义,
    必须显式指定)。纯函数,便于单测。
    """
    if not interrupts:
        return None
    if checkpoint is None or not str(checkpoint).strip():
        return interrupts[0] if len(interrupts) == 1 else None
    wanted = str(checkpoint).strip()
    for key in ("checkpoint", "legacy_id", "id"):
        hits = [i for i in interrupts if str(i.get(key) or "") == wanted]
        if hits:
            return hits[0]
    return None


def _print_interrupts(interrupts: list[dict[str, Any]]) -> None:
    """打印挂起关卡清单(操作员核对用)。"""
    if not interrupts:
        print("[resume] 当前无挂起人工关卡", file=sys.stderr)
        return
    print(f"[resume] 挂起人工关卡 ×{len(interrupts)}:", file=sys.stderr)
    for i in interrupts:
        print(
            f"[resume]   checkpoint={i['checkpoint']!r} legacy_id={i['legacy_id']!r} "
            f"step={i['step']} id={i['id']}",
            file=sys.stderr,
        )
        if i.get("instructions"):
            print(f"[resume]     指引: {i['instructions']}", file=sys.stderr)


def _maybe_preflight(args: argparse.Namespace) -> int:
    """按参数决定是否跑 pre-flight;返回进程退出码(0 继续,非 0 直接退)。"""
    if args.skip_preflight:
        print("[pre-flight] (跳过,--skip-preflight)")
        return 0

    from runtime.preflight import (
        PreflightConfig,
        PreflightError,
        run_preflight,
    )

    base = PreflightConfig.from_env()
    cfg = PreflightConfig(
        skip_docker_service=base.skip_docker_service,
        skip_proxy_check=base.skip_proxy_check,
        skip_postgres_start=base.skip_postgres_start,
        skip_schema_setup=args.skip_preflight_schema_setup or base.skip_schema_setup,
        docker_service_wait_sec=base.docker_service_wait_sec,
        postgres_health_wait_sec=base.postgres_health_wait_sec,
        dry_run=base.dry_run,
    )
    try:
        result = run_preflight(cfg)
    except PreflightError as e:
        print(f"[pre-flight] ✗ {e}", file=sys.stderr)
        return EXIT_PREFLIGHT
    except NotImplementedError as e:
        print(f"[pre-flight] ✗ {e}", file=sys.stderr)
        return EXIT_PREFLIGHT

    for s in result.steps:
        mark = "✓" if s.ok else "✗"
        suffix = f" — {s.error}" if s.error else ""
        print(f"[pre-flight] {mark} {s.step} ({s.duration_ms}ms){suffix}")

    return EXIT_OK if result.ok else EXIT_PREFLIGHT


def _check_final_state(result: Any) -> int:
    """检查恢复后的最终 state,返回进程退出码(与 run_workflow 同语义)。

    阶段3 起 ``openstoryline_ready`` 字段与"OpenStoryline 未就绪提前终止
    (退出码 3)"语义随 human 模式链一并删除:恢复走到最终 state 即成功
    (恒 ``EXIT_OK``);``error_log`` 各行打印到 stderr 供定位。
    """
    if isinstance(result, dict):
        for line in result.get("error_log") or []:
            print(f"[workflow]   {line}", file=sys.stderr)
    return EXIT_OK


def _print_event(event: dict) -> None:
    """CLI 事件输出(stderr;与 run_workflow 同格式,便于日志对照)。"""
    node = f" {event['node_id']}" if event.get("node_id") else ""
    event_id = event.get("id")
    print(
        f"[wf] #{event_id if event_id is not None else '-'} {event['event_type']}{node}",
        file=sys.stderr,
    )


async def _resume_async(args: argparse.Namespace) -> int:
    """恢复路径:校验挂起 → Command(resume) → run_with_events(§4.2 任务 4/5)。"""
    from graph import build_graph, get_checkpointer, normalize_thread_id

    thread_id = normalize_thread_id(args.thread_id)
    # 应用层迁移先行(fail-closed,与 run_workflow 同口径)。
    from db.migrations import apply_migrations_to_db

    apply_migrations_to_db()

    async with get_checkpointer() as saver:
        g = build_graph(
            checkpointer=saver,
            thread_id=thread_id,
            start_heartbeat_thread=False,
            run_preflight=False,
        )
        config = {"configurable": {"thread_id": thread_id}}

        # §4.2 任务 4:恢复前读挂起信息,校验 checkpoint 确属该 thread。
        snapshot = await g.aget_state(config)
        interrupts = describe_interrupts(snapshot)
        _print_interrupts(interrupts)
        if not interrupts:
            print(
                f"[resume] ✗ thread={thread_id!r} 无挂起关卡可恢复(退出码 {EXIT_NOT_SUSPENDED})",
                file=sys.stderr,
            )
            return EXIT_NOT_SUSPENDED

        matched = match_interrupt(interrupts, args.checkpoint)
        if matched is None:
            available = sorted(
                {str(i.get("checkpoint") or i.get("legacy_id") or i["id"]) for i in interrupts}
            )
            print(
                f"[resume] ✗ 指定 checkpoint 不匹配或多重挂起未指定(退出码 {EXIT_CHECKPOINT_MISMATCH});"
                f"可用: {available}",
                file=sys.stderr,
            )
            return EXIT_CHECKPOINT_MISMATCH

        if args.dry_run:
            print(
                f"[resume] --dry-run:不执行恢复。将恢复 checkpoint={matched['checkpoint']!r}"
                f"(id={matched['id']})",
                file=sys.stderr,
            )
            return EXIT_OK

        value = parse_resume_value(args.value)
        resume_map = {matched["id"]: value}

        # §4.2 任务 5:resume 走 run_with_events —— run.resumed / node.resumed /
        # 终态事件与启动路径同管线(§7.1;Command 输入即恢复运行)。
        from langgraph.types import Command

        from monitoring.workflow_runner import run_with_events

        result = await run_with_events(
            g, Command(resume=resume_map), config, on_event=_print_event
        )

        # 恢复后的落点提示:可能停在下一道人工关卡(正常挂起),也可能已到终点。
        after = await g.aget_state(config)
        pending_after = describe_interrupts(after)
        if pending_after:
            print(
                "[resume] ⏸ 已停在下一道人工关卡:"
                f"{[i.get('checkpoint') for i in pending_after]}",
                file=sys.stderr,
            )
        else:
            print("[resume] ✓ 恢复执行完成,无新的挂起关卡", file=sys.stderr)

    rc = _check_final_state(result)
    return rc


def _main_async(args: argparse.Namespace) -> int:
    if args.production:
        os.environ["WORKFLOW_ENV"] = "production"
    if not str(args.thread_id or "").strip():
        print("[resume] ✗ --thread-id 不能为空", file=sys.stderr)
        return EXIT_PREFLIGHT

    rc = _maybe_preflight(args)
    if rc != 0:
        return rc
    return asyncio.run(_resume_async(args))


def main() -> int:
    args = _parse_args()
    try:
        # Windows: psycopg async 需 SelectorEventLoop(与 run_workflow 同口径)
        if sys.platform == "win32":
            import selectors

            return asyncio.run(
                _main_async(args),
                loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
            )
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
