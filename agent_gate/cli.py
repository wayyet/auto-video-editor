"""命令行入口:python -m agent_gate <子命令>。

一次性准备:init                      —— 生成「操作员目录」(.mcp.json、CLAUDE.md、AGENTS.md、.claude/ 配置)
           mcode-lock                —— (可选)生成加锁的 MiniMax Code 配置:去掉 bash / 读写文件等内置工具
给 Agent 用(也可手动用):start / status / tasks / task / submit / submit-batch / events
无人值守:drive                       —— 反复拉起 claude / mcode 处理任务,直到完成或停在人工关卡
给人用(Agent 不应调用):resume   —— 默认要求在交互终端里运行
内部用:worker                       —— 由 start / resume / drive 拉起,不要手动调
所有子命令只往 stdout 打一个 JSON,方便 Agent 解析。读 JSON / 配置文件一律用 utf-8-sig:能容忍 Windows PowerShell 5.1 写出的 BOM。
退出码:0 成功;1 操作失败;3 找不到;2 参数错误(argparse)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from agent_gate import runner, service
from agent_gate.mailbox import Mailbox
from agent_gate.safety import (
    UnsafeIdentifier,
    UnsafePath,
    media_roots,
    rejection,
    safe_media_path,
    safe_state_file,
)


def _emit(obj: Any, code: int = 0) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return code


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--thread-id", required=True, help="LangGraph thread_id,也是 outputs/<thread_id>/ 的目录名")
    p.add_argument("--outputs-root", default=os.environ.get("AGENT_GATE_OUTPUTS", "outputs"),
                   help="outputs 根目录(默认环境变量 AGENT_GATE_OUTPUTS,再默认 ./outputs)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agent_gate", description="让 Agent 程序驱动 auto-video-editor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    gf_default = os.environ.get("AGENT_GATE_GRAPH", "graph:build_graph")

    p = sub.add_parser("init", help="生成「操作员目录」:.mcp.json、CLAUDE.md、AGENTS.md、.claude/ 配置")
    p.add_argument("--project-dir", required=True, help="操作员目录(Agent 在这里启动;不存在会创建)")
    p.add_argument("--repo-root", required=True, help="auto-video-editor 仓库根目录(worker 在这里跑图)")
    p.add_argument("--outputs-root", help="outputs 根目录(默认 <repo-root>/outputs)")
    p.add_argument("--graph-factory", default=gf_default)
    p.add_argument("--python", help="写进配置的 python(默认当前这个;应是装了依赖的虚拟环境)")
    p.add_argument("--with-human-tool", action="store_true",
                   help="MCP 服务额外注册 human_resume(需真人确认;默认不注册,见文档「安全」)")
    p.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    p.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                   help="往 .mcp.json 的 env 里再写一个环境变量(可重复;例:--env STORYLINE_MODE=auto)")

    p = sub.add_parser("mcode-lock", help="生成加锁的 mcode 配置:复制 --base,再追加「去掉内置工具」的几段")
    p.add_argument("--base", required=True, help="mcode 配置文件(provider 已配好),在 $MINIMAX_DATA_DIR 下的 config.yaml")
    p.add_argument("--out", required=True, help="输出文件(里面有和 --base 一样的 API key,别放进 git)")
    p.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")

    p = sub.add_parser("start", help="拉起 worker 开始一次新运行(后台运行,立即返回)")
    _common(p)
    p.add_argument("--graph-factory", default=gf_default)
    p.add_argument("--initial-state-file", help="初始 state 的 JSON 文件")
    p.add_argument("--video", help="视频路径;没给 --initial-state-file 时用它生成最小初始 state")
    p.add_argument("--video-paths", nargs="+", default=None,
                   help="多个视频路径(多视频导入);给了它就优先于 --video")
    p.add_argument("--backend", default="gate", choices=["gate", "claude-cli", "mcode-cli", "stub"])
    p.add_argument("--require-preflight", action="store_true",
                   help="让图跑 preflight(默认跳过)")
    p.add_argument("--continue", dest="cont", action="store_true",
                   help="worker 崩溃后,从最近检查点继续(不传初始 state)")

    p = sub.add_parser("drive", help="无人值守:反复拉起 Agent 处理任务,直到完成或停在人工关卡")
    _common(p)
    p.add_argument("--graph-factory", default=gf_default)
    p.add_argument("--agent", required=True, choices=["claude", "mcode"])
    p.add_argument("--project-dir", required=True,
                   help="放着 .mcp.json 和 CLAUDE.md / AGENTS.md 的目录(Agent 在这里启动)")
    p.add_argument("--video", help="视频路径;还没有运行记录时用它生成最小初始 state")
    p.add_argument("--video-paths", nargs="+", default=None,
                   help="多个视频路径(多视频导入);给了它就优先于 --video")
    p.add_argument("--initial-state-file", help="初始 state 的 JSON 文件")
    p.add_argument("--batch", type=int, default=5, help="每个 Agent 会话一次最多取几个任务")
    p.add_argument("--max-sessions", type=int, default=40, help="最多拉起几个 Agent 会话")
    p.add_argument("--max-wall-s", type=float, default=7200.0, help="总时长上限(秒)")
    p.add_argument("--session-timeout-s", type=float, default=900.0, help="单个 Agent 会话时长上限(秒)")
    p.add_argument("--agent-bin", help="Agent 可执行文件(默认 PATH 里的 claude / mcode)")
    p.add_argument("--model", help="覆盖 Agent 使用的模型")
    p.add_argument("--permission", default="smart", choices=["smart", "full", "off"],
                   help="仅 mcode:--permission 取值")
    p.add_argument("--mcode-config", help="仅 mcode:加锁配置文件(mcode-lock 命令生成)。不给就用 mcode 默认配置,"
                                          "模型会带着 bash / 读写文件等内置工具")

    p = sub.add_parser("worker", help="(内部)在当前进程里跑图")
    _common(p)
    p.add_argument("--graph-factory", default=gf_default)
    p.add_argument("--initial-state-file")
    p.add_argument("--resume-file")
    p.add_argument("--backend", default="gate")
    p.add_argument("--require-preflight", action="store_true")

    p = sub.add_parser("status", help="查看运行状态和下一步该做什么")
    _common(p)

    p = sub.add_parser("tasks", help="列出待办 LLM 任务")
    _common(p)
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--wait-s", type=float, default=0.0, help="没有任务时最多等多少秒")

    p = sub.add_parser("task", help="看单个任务的完整内容")
    _common(p)
    p.add_argument("--task-id", required=True)

    p = sub.add_parser("submit", help="提交一个任务的答案")
    _common(p)
    p.add_argument("--task-id", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--answer", help="答案文本(JSON 任务传 JSON 字符串)")
    g.add_argument("--answer-file", help="从文件读答案(推荐:避免引号转义问题)")

    p = sub.add_parser("submit-batch", help="一次提交多个任务的答案")
    _common(p)
    p.add_argument("--results-file", required=True, help='JSON 文件:{"<task_id>": <答案>, ...}')

    p = sub.add_parser("events", help="看最近的事件日志")
    _common(p)
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("resume", help="【给人用】通过人工关卡后继续运行")
    _common(p)
    p.add_argument("--graph-factory", default=gf_default)
    p.add_argument("--checkpoint", required=True, help="关卡标识,如 ① ② ③ ⓪")
    p.add_argument("--value", default="true", help="传给 interrupt 的值(JSON),默认 true")
    p.add_argument("--backend", default="gate", choices=["gate", "claude-cli", "mcode-cli", "stub"])
    p.add_argument("--force-no-tty", action="store_true",
                   help="允许在非交互环境运行(仅限 CI;Agent 不应使用)")
    return ap


def _mb(args: argparse.Namespace) -> Mailbox:
    """按 --outputs-root / --thread-id 拿信箱。thread_id 走 F1 的白名单。"""
    return Mailbox(runner.gate_root(args.outputs_root, args.thread_id))


def _state_file_roots(args: argparse.Namespace) -> list[Path]:
    """``--initial-state-file`` 的允许目录(F2 纵深防御)。

    合法位置只有两类:
    - **outputs 根之内**(gate_root 就在它下面;``initial_state.json`` 默认也写在那儿);
    - **素材白名单之内**(操作员把初始 state 和素材放一起)。

    注意这跟 ``AGENT_GATE_MEDIA_ROOTS`` 的语义不同:那个是给**视频素材**用的,
    这里管的是**初始 state 文件**,所以额外放行 outputs 根。
    """
    roots = [Path(r).resolve() for r in media_roots()]
    roots.insert(0, Path(args.outputs_root).resolve())
    return roots


def _write_initial_state(
    mb: Mailbox, thread_id: str, video: str, videos: list[str] | None = None
) -> Path:
    """按 --video 生成最小初始 state。video 也要过素材白名单(F2 纵深防御)。

    多视频:``videos`` 非空时,每个路径都过白名单,初始 state 写
    ``video_input_paths``(``video_input_path`` 仍取第一个,保持兼容)。
    """
    raw_paths = [str(v) for v in (videos or []) if str(v or "").strip()]
    if not raw_paths:
        raw_paths = [video]
    safe_paths = [str(safe_media_path(v)) for v in raw_paths]
    p = mb.root / "initial_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "session_id": thread_id,
        "video_input_path": safe_paths[0],
        "error_log": [],
    }
    if len(safe_paths) > 1:
        payload["video_input_paths"] = safe_paths
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    args = build_parser().parse_args(argv)

    if args.cmd == "worker":
        try:
            initial = None
            if args.initial_state_file:
                initial = json.loads(safe_state_file(
                    args.initial_state_file, roots=_state_file_roots(args)).read_text(encoding="utf-8-sig"))
            resume = None
            if args.resume_file:
                resume = json.loads(Path(args.resume_file).read_text(encoding="utf-8-sig"))
            return runner.run_worker(
                thread_id=args.thread_id, outputs_root=args.outputs_root,
                graph_factory=args.graph_factory, initial_state=initial,
                resume_values=resume, backend=args.backend,
                require_preflight=args.require_preflight,
            )
        except (UnsafeIdentifier, UnsafePath, OSError, ValueError) as e:
            return _emit(rejection(e), 1)

    if args.cmd == "init":
        from agent_gate.scaffold import init_project, parse_env_pairs

        try:
            extra_env = parse_env_pairs(args.env)
            res = init_project(
                project_dir=args.project_dir, repo_root=args.repo_root, outputs_root=args.outputs_root,
                graph_factory=args.graph_factory, python=args.python,
                with_human_tool=args.with_human_tool, force=args.force, env=extra_env)
        except ValueError as e:
            return _emit({"ok": False, "code": "bad_env", "message": str(e)}, 1)
        return _emit(res)

    if args.cmd == "mcode-lock":
        from agent_gate.scaffold import build_mcode_locked_config

        out = Path(args.out)
        if out.exists() and not args.force:
            return _emit({"ok": False, "code": "exists", "message": f"{out} 已存在;要覆盖请加 --force"}, 1)
        try:
            text = build_mcode_locked_config(Path(args.base).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            return _emit({"ok": False, "code": "bad_base", "message": str(e)}, 1)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return _emit({"ok": True, "out": str(out),
                      "note": "输出文件里有和 --base 一样的 API key,别放进 git;drive 时用 --mcode-config 指向它"})

    try:
        mb = _mb(args)
    except (UnsafeIdentifier, UnsafePath) as e:
        return _emit(rejection(e), 1)

    if args.cmd == "drive":
        from agent_gate.drive import drive
        from agent_gate.launcher import build_launcher

        init_file: Optional[Path] = Path(args.initial_state_file) if args.initial_state_file else None
        videos = getattr(args, "video_paths", None)
        if init_file is None and (args.video or videos):
            try:
                init_file = _write_initial_state(mb, args.thread_id, args.video or "", videos)
            except (UnsafeIdentifier, UnsafePath) as e:
                return _emit(rejection(e), 1)
        if init_file is not None:
            try:
                init_file = safe_state_file(init_file, roots=_state_file_roots(args))
            except (UnsafeIdentifier, UnsafePath) as e:
                return _emit(rejection(e), 1)
        kw: dict[str, Any] = {"project_dir": args.project_dir, "executable": args.agent_bin,
                              "model": args.model, "session_timeout_s": args.session_timeout_s}
        warnings: list[str] = []
        if args.agent == "mcode":
            kw["permission"] = args.permission
            kw["config"] = args.mcode_config
            if not args.mcode_config:
                warnings.append("没有给 --mcode-config:mcode 的模型除了 MCP 工具,还有 bash / 读写文件 / 联网等内置工具"
                                "(只有文字规则约束,没有硬隔离)。生成加锁配置见 mcode-lock 命令。")
        res = drive(thread_id=args.thread_id, outputs_root=args.outputs_root,
                    graph_factory=args.graph_factory, launcher=build_launcher(args.agent, **kw),
                    initial_state_file=init_file, batch=args.batch, max_sessions=args.max_sessions,
                    max_wall_s=args.max_wall_s)
        if warnings:
            res["warnings"] = warnings
        return _emit(res, res["exit_code"])

    if args.cmd == "start":
        mode = "continue_after_crash" if args.cont else "start"
        init_file = Path(args.initial_state_file) if args.initial_state_file else None
        if init_file is None and not args.cont:
            if not args.video and not getattr(args, "video_paths", None):
                return _emit({"ok": False, "code": "need_input",
                              "message": "要么给 --initial-state-file,要么给 --video,要么用 --continue"}, 1)
            try:
                init_file = _write_initial_state(
                    mb, args.thread_id, args.video or "",
                    getattr(args, "video_paths", None),
                )
            except (UnsafeIdentifier, UnsafePath) as e:
                return _emit(rejection(e), 1)
        if init_file is not None:
            try:
                init_file = safe_state_file(init_file, roots=_state_file_roots(args))
            except (UnsafeIdentifier, UnsafePath) as e:
                return _emit(rejection(e), 1)
        res = runner.spawn_worker(
            thread_id=args.thread_id, outputs_root=args.outputs_root,
            graph_factory=args.graph_factory, initial_state_file=init_file,
            backend=args.backend, mode=mode, require_preflight=args.require_preflight,
        )
        return _emit(res, 0 if res["ok"] else 1)

    if args.cmd == "status":
        return _emit(service.status_summary(mb, thread_id=args.thread_id))

    if args.cmd == "tasks":
        return _emit(service.wait_for_work(mb, thread_id=args.thread_id,
                                           wait_s=args.wait_s, limit=args.limit))

    if args.cmd == "task":
        view = service.get_task(mb, args.task_id)
        return _emit(view, 0) if view else _emit({"ok": False, "code": "unknown_task"}, 3)

    if args.cmd == "submit":
        answer = (Path(args.answer_file).read_text(encoding="utf-8-sig")
                  if args.answer_file else args.answer)
        res = service.submit_result(mb, args.task_id, answer)
        return _emit(res, 0 if res.get("ok") else 1)

    if args.cmd == "submit-batch":
        results = json.loads(Path(args.results_file).read_text(encoding="utf-8-sig"))
        res = service.submit_batch(mb, results)
        return _emit(res, 0 if res["ok"] else 1)

    if args.cmd == "events":
        return _emit(mb.read_events(args.limit))

    if args.cmd == "resume":
        if not args.force_no_tty and not (sys.stdin.isatty() and sys.stdout.isatty()):
            return _emit({"ok": False, "code": "need_human_terminal",
                          "message": "人工关卡只能由人在交互终端里放行。"
                                     "如果你是 Agent:请停止并把关卡信息告诉用户。"}, 1)
        try:
            value = json.loads(args.value)
        except json.JSONDecodeError:
            value = args.value
        res = runner.spawn_worker(
            thread_id=args.thread_id, outputs_root=args.outputs_root,
            graph_factory=args.graph_factory, resume_values={args.checkpoint: value},
            backend=args.backend, mode="resume",
        )
        return _emit(res, 0 if res["ok"] else 1)

    return 2


if __name__ == "__main__":
    sys.exit(main())
