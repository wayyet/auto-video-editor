"""auto-video-editor 主程序入口。

标准化「程序入口」概念。当前项目里没有 ``main.py`` / ``__main__.py``,
所有测试都假定用户直接调 ``graph.build_graph()``。本脚本填补空缺:
按 pre-flight → build_graph → invoke 的顺序完成一个完整 workflow 运行。

典型用法
--------

.. code-block:: powershell

    # 常规运行(Postgres checkpointer,唯一执行路径)
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
- PostgreSQL 统一改造后**只有一条执行路径**:Postgres checkpointer + 统一
  runner(``monitoring.workflow_runner.run_with_events``,内部 astream 消费;
  阶段3 起 CLI 运行也产出 §7.1 标准事件并落共享 PG 事件表)。原
  ``_invoke_sync()``(SQLite / 内存 saver + ``invoke``)已删除(方案 §4.3)。
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
        "--video-input-paths",
        default="",
        help=(
            "可选:多个源视频路径(逗号分隔)。"
            "写入 initial_state.video_input_paths —— auto mode 多视频入口。"
            "不填时沿用单视频 video_input_path。"
        ),
    )
    p.add_argument(
        "--skip-preflight-schema-setup",
        action="store_true",
        help="跳过 checkpoint 表创建(默认会确保 schema 存在)",
    )
    p.add_argument(
        "--llm-backend",
        choices=("stub", "claude-cli", "mcode-cli"),
        default=None,
        help=(
            "可选:执行前把指定 LLM 后端注入 storyline_capabilities 默认 client"
            "(第二期创建台用)。不传 = 完全保持既有行为(懒加载 StubLLMClient)。"
        ),
    )
    return p.parse_args()


def _apply_llm_backend(backend: Optional[str]) -> None:
    """把指定后端注入为 storyline_capabilities 的默认 client(第二期阶段 2)。

    复用既有装配:``agent_gate.factory.build_client`` →
    ``storyline_capabilities.vlm_client.set_default_client``(计划 §8.2)。
    ``backend`` 为空时**不注入** —— 旧 CLI 行为完全不变(懒加载 Stub)。
    gate 后端需要 mailbox,不在本入口的选择集内(agent_gate CLI 自行处理)。
    """
    if not backend:
        return
    from agent_gate.factory import build_client
    from storyline_capabilities.vlm_client import set_default_client

    client = build_client(backend)
    set_default_client(client)
    print(f"[llm] 已注入 LLM 后端: {backend}", file=sys.stderr)


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


def _default_initial_state(thread_id: str, video_input_paths: str = "") -> dict:
    """构造一个最小可用的初始 WorkflowState(便于 `--production` 模式调试)。

    ``video_input_path`` 取第一个存在的候选素材:

    1. ``inputs/30s.mp4``            —— 本地真实素材(inputs/ 被 .gitignore 忽略,
                                       新克隆后需要自己放一份)
    2. ``tests/fixtures/30s.mp4``    —— 仓库自带 fixture

    两者都不存在时留空(下游节点会记录 error_log,而不是崩在启动阶段)。
    生产场景建议用 ``--initial-state-json`` 显式注入真实路径。

    ``video_input_paths``(逗号分隔)非空时写入 ``state["video_input_paths"]``,
    即 auto mode 的多视频入口(``video_input_path`` 仍取第一个,保持兼容)。
    """
    candidates = (
        ROOT / "inputs" / "30s.mp4",
        ROOT / "tests" / "fixtures" / "30s.mp4",
    )
    video = next((str(c) for c in candidates if c.exists()), "")
    state: dict = {
        "session_id": thread_id,
        "video_input_path": video,
        "error_log": [],
    }
    extra = [p.strip() for p in str(video_input_paths or "").split(",") if p.strip()]
    if extra:
        state["video_input_paths"] = extra
        state["video_input_path"] = extra[0]
    return state


def _pin_repo_relative_outputs_root(state: dict, thread_id: str) -> None:
    """把 ``storyline_outputs_root`` 强制钉回当前仓库相对目录 outputs/<thread_id>。

    背景(2026-11-06):E 盘旧仓库副本已废弃,但早期在该副本上运行留下的
    checkpoint state 携带旧绝对路径(如 ``E:\\...\\auto-video-editor\\outputs\\...``),
    而 ``nodes/storyline/_common.py::_resolve_outputs_root`` 优先采信 state 里的
    显式值、``join_storyline`` 又只在字段缺失时才回写 —— 旧值随 checkpoint
    续跑永远复活,assembly/render 全链输出写回废弃盘。
    生产入口在此强制覆盖:输出一律落当前仓库 ``outputs/<thread_id>/``;覆盖值
    随后随 checkpoint 持久化,旧路径从此不再生效。单测/集测直接构造 state
    调节点,不经本入口,注入 tmp_path 的用法不受影响。
    """
    state["storyline_outputs_root"] = str(ROOT / "outputs" / thread_id)


def _load_initial_state(args: argparse.Namespace) -> dict:
    """加载 initial state;``--initial-state-json`` 指向的文件缺失时自动生成。

    背景:``runtime/initial_state_*.json`` 被 .gitignore 排除(运行时生成物,
    不入库),新克隆 / 执行过 ``stop_production.ps1 -CleanState`` / 手动清理后
    都会缺失。原先这里直接 ``read_text()`` 抛裸 FileNotFoundError,只给出
    "No such file or directory",看不出下一步该做什么。
    """
    extra_paths = str(getattr(args, "video_input_paths", "") or "")
    if not args.initial_state_json:
        state = _default_initial_state(args.thread_id, extra_paths)
        _pin_repo_relative_outputs_root(state, args.thread_id)
        return state

    path = args.initial_state_json
    if not path.exists():
        state = _default_initial_state(args.thread_id, extra_paths)
        _pin_repo_relative_outputs_root(state, args.thread_id)
        print(
            f"[state] ⚠ 找不到 initial state 文件: {path}\n"
            f"[state]   已自动生成默认 initial state 并落盘"
            f"(video_input_path={state['video_input_path'] or '空'})",
            file=sys.stderr,
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"[state]   已写入 {path}", file=sys.stderr)
        except OSError as e:
            print(
                f"[state]   ✗ 写入失败({e});继续用内存中的默认 state 运行",
                file=sys.stderr,
            )
        return state

    state = json.loads(path.read_text(encoding="utf-8"))
    # 命令行显式给了多路径 → 覆盖文件里的同名字段
    if extra_paths:
        paths = [p.strip() for p in extra_paths.split(",") if p.strip()]
        state["video_input_paths"] = paths
        state["video_input_path"] = paths[0]
    # E 盘旧仓库已废弃:无论 JSON 文件写了什么,输出根一律钉回当前仓库相对目录
    _pin_repo_relative_outputs_root(state, args.thread_id)
    return state


def _check_final_state(result: Optional[dict]) -> int:
    """检查图的最终 state,返回进程退出码。

    阶段3 起 ``openstoryline_ready`` 字段与"node_02 未就绪提前终止(退出码 3)"
    语义随 human 模式链一并删除:只要流程走到最终 state 就算正常结束(**恒 0**);
    失败信息统一由 ``error_log`` 透出 —— 这里把 error_log 各行打印到 stderr,
    便于 CLI 使用者定位。
    """
    if not isinstance(result, dict):
        return 0
    errors = result.get("error_log") or []
    for line in errors:
        print(f"[workflow]   {line}", file=sys.stderr)
    return 0


def _print_event(event: dict) -> None:
    """CLI 事件输出(stderr,不污染 stdout 消费者;阶段3 统一事件接线)。"""
    node = f" {event['node_id']}" if event.get("node_id") else ""
    event_id = event.get("id")
    print(
        f"[wf] #{event_id if event_id is not None else '-'} {event['event_type']}{node}",
        file=sys.stderr,
    )


async def _invoke_async(args: argparse.Namespace) -> int:
    """生产路径:Postgres checkpointer + 统一 runner(唯一执行路径,阶段3)。"""
    from graph import build_graph, get_checkpointer, normalize_thread_id

    state = _load_initial_state(args)
    thread_id = normalize_thread_id(args.thread_id)
    # 应用层迁移先行:``chat_sessions`` / ``workflow_runs`` / ``workflow_run_events`` /
    # ``app_schema_migrations`` 必须在跑工作流之前就位(方案 §4.3)。失败即中止
    # 启动 —— fail-closed,不允许"迁移失败但工作流照跑"的半可用状态。
    from db.migrations import apply_migrations_to_db

    apply_migrations_to_db()
    async with get_checkpointer() as saver:
        # pre-flight 已在主入口跑过,这里 run_preflight=False 避免重复
        g = build_graph(
            checkpointer=saver,
            thread_id=thread_id,
            start_heartbeat_thread=False,
            run_preflight=False,
        )
        config = {"configurable": {"thread_id": thread_id}}
        # 阶段3:ainvoke → 统一 runner(astream 消费 + 事件落 PG 事件表)。
        # 返回值语义不变(最终 state values),退出码判定(_check_final_state)不变。
        from monitoring.workflow_runner import run_with_events

        result = await run_with_events(g, state, config, on_event=_print_event)
    return _check_final_state(result)


async def _main_async(args: argparse.Namespace) -> int:
    if args.production:
        os.environ["WORKFLOW_ENV"] = "production"

    rc = _maybe_preflight(args)
    if rc != 0:
        return rc

    # 第二期阶段 2:显式给了 --llm-backend 时,在执行图**之前**注入 LLM 后端,
    # 保证首节点(storyline_load_media 之后的 LLM 节点)就用上真实模型。
    _apply_llm_backend(getattr(args, "llm_backend", None))

    # PostgreSQL 统一改造(方案 §4.3):SQLite 分支已删除 ——
    # 无论 WORKFLOW_ENV 是什么,都走 Postgres 异步路径。
    return await _invoke_async(args)


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