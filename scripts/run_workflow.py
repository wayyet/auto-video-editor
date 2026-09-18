"""auto-video-editor 主程序入口。

标准化「程序入口」概念。当前项目里没有 ``main.py`` / ``__main__.py``,
所有测试都假定用户直接调 ``graph.build_graph()``。本脚本填补空缺:
按 pre-flight → build_graph → invoke 的顺序完成一个完整 workflow 运行。

典型用法
--------

.. code-block:: powershell

    # 默认开发模式(sqlite checkpointer,跑本地 fixture 30s.mp4)
    .\\.venv\\Scripts\\python.exe scripts\\run_workflow.py --thread-id video-001

    # 生产模式(WORKFLOW_ENV=production + PostgresSaver)
    .\\.venv\\Scripts\\python.exe scripts\\run_workflow.py --production --thread-id prod-001

    # CI / 测试:跳过 pre-flight
    .\\.venv\\Scripts\\python.exe scripts\\run_workflow.py --skip-preflight

注意
----
- ``--skip-preflight`` 仅供 CI / 单测 / 已知环境就绪的场景;生产环境务必保留
  pre-flight 自检。
- ``--production`` 等价于 ``WORKFLOW_ENV=production``;需要本机
  Postgres 已就绪(由 pre-flight 步骤 4-6 负责)。
- 管理员权限:Step 1(``Start-Service``)需要 admin;非管理员跑会失败
  并给出明确指引。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

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

from runtime.preflight import (  # noqa: E402  — sys.path 调整后才能 import
    PreflightConfig,
    PreflightError,
    run_preflight,
)


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    p = argparse.ArgumentParser(
        prog="run_workflow",
        description="auto-video-editor 主程序入口",
    )
    p.add_argument("--thread-id", default="video-001", help="LangGraph thread_id(默认 video-001)")
    p.add_argument(
        "--skip-preflight",
        action="store_true",
        help="跳过 pre-flight 检查(测试/CI 用,生产请勿使用)",
    )
    p.add_argument(
        "--production",
        action="store_true",
        help="等价于 WORKFLOW_ENV=production,使用 Postgres checkpointer",
    )
    p.add_argument(
        "--initial-state-json",
        type=Path,
        default=None,
        help="可选:从 JSON 文件读 initial state",
    )
    p.add_argument(
        "--skip-preflight-schema-setup",
        action="store_true",
        help="跳过 checkpoint 表创建(默认会确保 schema 存在)",
    )
    return p.parse_args()


def _maybe_preflight(args: argparse.Namespace) -> int:
    """按参数决定是否跑 pre-flight;返回进程退出码(0 继续,非 0 直接退)。"""
    if args.skip_preflight:
        print("[pre-flight] (跳过,--skip-preflight)")
        return 0

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
        return 2
    except NotImplementedError as e:
        # 非 Windows 平台:直接抛错,降级路径留给调用方
        print(f"[pre-flight] ✗ {e}", file=sys.stderr)
        return 2

    for s in result.steps:
        mark = "✓" if s.ok else "✗"
        suffix = f" — {s.error}" if s.error else ""
        print(f"[pre-flight] {mark} {s.step} ({s.duration_ms}ms){suffix}")

    if not result.ok:
        return 2
    return 0


def _default_initial_state(thread_id: str) -> dict:
    """构造一个最小可用的初始 WorkflowState(便于 `--production` 模式调试)。

    ``video_input_path`` 默认指向 ``tests/fixtures/30s.mp4``(若存在)。
    生产场景建议用 ``--initial-state-json`` 显式注入真实路径。
    """
    fixture = ROOT / "tests" / "fixtures" / "30s.mp4"
    state: dict = {
        "session_id": thread_id,
        "video_input_path": str(fixture) if fixture.exists() else "",
        "error_log": [],
    }
    return state


def _load_initial_state(args: argparse.Namespace) -> dict:
    if args.initial_state_json:
        return json.loads(args.initial_state_json.read_text(encoding="utf-8"))
    return _default_initial_state(args.thread_id)


async def _invoke_async(args: argparse.Namespace) -> int:
    """生产路径:Postgres checkpointer + ainvoke。"""
    from graph import build_graph, get_checkpointer

    state = _load_initial_state(args)
    async with get_checkpointer() as saver:
        # pre-flight 已在主入口跑过,这里 run_preflight=False 避免重复
        g = build_graph(
            checkpointer=saver,
            thread_id=args.thread_id,
            start_heartbeat_thread=False,
            run_preflight=False,
        )
        config = {"configurable": {"thread_id": args.thread_id}}
        await g.ainvoke(state, config=config)
    return 0


def _invoke_sync(args: argparse.Namespace) -> int:
    """开发路径:sqlite/in-memory checkpointer + invoke。"""
    from graph import build_graph

    state = _load_initial_state(args)
    g = build_graph(
        checkpointer=None,
        thread_id=args.thread_id,
        start_heartbeat_thread=False,
        run_preflight=False,
    )
    config = {"configurable": {"thread_id": args.thread_id}}
    g.invoke(state, config=config)
    return 0


async def _main_async(args: argparse.Namespace) -> int:
    if args.production:
        os.environ["WORKFLOW_ENV"] = "production"

    rc = _maybe_preflight(args)
    if rc != 0:
        return rc

    if os.environ.get("WORKFLOW_ENV") == "production":
        return await _invoke_async(args)
    return _invoke_sync(args)


def main() -> int:
    args = _parse_args()
    try:
        # Windows: psycopg async 需 SelectorEventLoop,Python 3.13 默认 ProactorEventLoop 不兼容
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