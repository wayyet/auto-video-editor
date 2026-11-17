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

    # 节点失败(run.failed,无挂起关卡)后从最后 checkpoint 续跑:重跑失败节点
    .\\.venv\\Scripts\\python.exe scripts\\resume_workflow.py --thread-id prod-001 --continue

语义与边界(执行计划 §3.5 / §4.2)
--------
- **恢复管线与启动入口一致**:Postgres checkpointer + ``monitoring.workflow_runner.
  run_with_events``,因此 resume 同样产出 ``run.resumed`` / ``node.resumed`` /
  终态事件,监控页与 SSE 无差别消费(§4.2 任务 2/5)。
- **恢复前校验**:先 ``graph.aget_state(config)`` 读取挂起信息,校验指定
  checkpoint(关卡标识 ``①/②`` 或 legacy_id)确实属于该 thread,不匹配即
  fail-closed(退出码 5),绝不盲目注入 ``Command(resume=...)``(§4.2 任务 4)。
- **入口不混用**(§3.5 结论):本脚本只恢复**直接 CLI** 启动的运行;
  ``agent_gate run_start / drive`` 启动的运行仍由 ``agent_gate resume`` 恢复
  (worker 状态机 + mailbox)。两条路径共享 PostgreSQL checkpoint 与事件表,
  但不共享控制状态,跨入口恢复不受支持。
- resume value 传给 ``interrupt()`` 的值:关卡 ①/② 均按布尔语义消费
  (节点 docstring:``Command(resume=True)``);默认 ``true``。
- **--continue 续跑(2026-11-17 创建台失败重试)**:无挂起关卡时不再直接拒绝,
  而是以 ``None`` 输入续跑(``run_with_events(g, None, config)``)—— LangGraph
  从最后 checkpoint 恢复,失败 superstep 的写入未提交,失败节点会被重新执行
  (与 ``agent_gate`` 的 ``continue_after_crash`` 模式同语义;事件流仍发
  ``run.resumed``/``node.resumed``,监控页无差别消费)。fail-closed:Postgres
  里确无 checkpoint 或查询失败(数据库不可达)一律拒绝(退出码 7),不许盲目
  续跑;state 有 ``draft_path`` 时仍执行恢复前文件门禁(草稿产出后才需要保护)。
  有挂起关卡时 ``--continue`` 不改变原 resume 行为(优先走关卡恢复)。

退出码
--------
- ``0``  成功(含"恢复后停在下一道人工关卡"的正常挂起;最终 state 有
  ``error_log`` 时逐行打印到 stderr,便于定位);
- ``1``  图执行异常(run_with_events 先发 ``run.failed`` 再抛出);
- ``2``  pre-flight 失败 / 参数非法;
- ``4``  该 thread 无挂起关卡可恢复;
- ``5``  指定 checkpoint 与挂起关卡不匹配(或多重挂起未指定);
- ``6``  恢复前文件安全门禁未通过(阶段 3:剪映未关闭 / 草稿缺失或损坏 /
  目录被占用;stderr 附问题清单与人工修改后草稿的 SHA-256 审计值)。
- ``7``  --continue 续跑前置失败(fail-closed):Postgres 确无 checkpoint /
  数据库不可用 / 恢复初始化链路失败(应用层迁移 / checkpointer 连接 /
  图构建 / checkpoint 状态读取,任一段异常统一此退出码,stderr 附阶段标签)。
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
#: 阶段 3(人工关卡与剪映草稿导入):恢复前文件安全门禁未通过
#: (剪映未关闭 / 草稿缺失或损坏 / 目录被占用),详见 stderr 问题清单
EXIT_RESUME_GUARD = 6
#: --continue 续跑:Postgres 确无 checkpoint 或查询失败(fail-closed,
#: 语义对齐 agent_gate check_mode 的 no_checkpoint / database_unavailable)
EXIT_NO_CHECKPOINT = 7


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
            "要恢复的关卡标识:interrupt payload 的 checkpoint(①/②)或 "
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
        # dest 必须显式给:argparse 默认取 "--continue" → "continue"(Python 关键字)
        "--continue",
        dest="continue_run",
        action="store_true",
        help=(
            "无挂起关卡时从最后 checkpoint 续跑(失败节点重试,创建台「重试失败"
            "节点」用):None 输入续跑,失败节点重新执行;Postgres 确无 checkpoint"
            "或查询失败一律拒绝(fail-closed)。与 --checkpoint 互斥"
        ),
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
    p.add_argument(
        "--allow-jianying-running",
        action="store_true",
        help=(
            "放行「剪映仍在运行」的恢复前置检查(默认拒绝:恢复前必须保存并关闭剪映,"
            "避免工作流写入与剪映自动保存竞争;仅开发/测试用)"
        ),
    )
    p.add_argument(
        "--llm-backend",
        choices=("stub", "claude-cli", "mcode-cli"),
        default=None,
        help=(
            "可选:恢复执行前把指定 LLM 后端注入 storyline_capabilities 默认 client"
            "(第二期创建台恢复用,与 run_workflow --llm-backend 对齐)。"
            "不传 = 完全保持既有行为(懒加载 StubLLMClient)。"
        ),
    )
    return p.parse_args()


def _apply_llm_backend(backend: Optional[str]) -> None:
    """恢复执行前注入 LLM 后端(与 run_workflow.py 同款;第二期阶段 5)。

    2026-11-17 P1 起改走公共装配 ``agent_gate.bootstrap.install_llm_backend``:
    同一个 client 实例同时注入 VLM 与英文翻译(``LLMTranslateClient``)——
    避免恢复执行后翻译能力退回 Mock(修订计划 §5.3 三入口对齐)。
    不传时**不注入** —— 旧 CLI 行为完全不变。
    gate 后端需要 mailbox,不在本入口的选择集内(agent_gate CLI 自行处理)。
    """
    if not backend:
        return
    from agent_gate.bootstrap import install_llm_backend

    install_llm_backend(backend)
    print(f"[llm] 已注入 LLM 后端: {backend}", file=sys.stderr)


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

    匹配顺序:payload ``checkpoint``(①/②)→ ``legacy_id`` → interrupt ``id``。
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
    """恢复入口:LLM 后端注入先行(与 run_workflow 同序:注入在图执行前),
    再进入恢复主体(与 run_workflow 的 _main_async → _invoke_async 同款分层)。"""
    _apply_llm_backend(args.llm_backend)
    return await _resume_async_impl(args)


async def _continue_after_failure(
    g: Any,
    saver: Any,
    config: dict,
    args: argparse.Namespace,
    snapshot: Any,
) -> int:
    """--continue 续跑主体:失败运行从最后 checkpoint 恢复(None 输入)。

    fail-closed 校验(语义对齐 ``agent_gate`` ``check_mode`` 的
    ``no_checkpoint`` / ``database_unavailable``):Postgres 里确有 checkpoint
    才放行;查询失败(数据库不可达)≠ 无 checkpoint,同样拒绝 —— 不许盲目
    续跑,但错误语义要准确,避免把"数据库挂了"误导成"换新任务重跑"。

    state 有 ``draft_path`` 时仍执行恢复前文件门禁(草稿产出后才需要保护:
    恢复后工作流要继续改写同一份草稿,避免与剪映自动保存竞争);失败发生在
    产草稿前则跳过(没有草稿可校验,剪映是否开着与续跑无关)。
    """
    print(
        "[resume] --continue:无挂起关卡,尝试从最后 checkpoint 续跑(重跑失败节点)",
        file=sys.stderr,
    )
    try:
        tuple_ = await saver.aget_tuple(config)
    except Exception as e:  # noqa: BLE001 — 查询失败 ≠ 无 checkpoint:fail-closed
        print(
            f"[resume] ✗ 无法确认该 thread 是否有 checkpoint(数据库查询失败: "
            f"{type(e).__name__}: {e});请先恢复数据库连接再重试,不要新建任务盲目重跑"
            f"(退出码 {EXIT_NO_CHECKPOINT})",
            file=sys.stderr,
        )
        return EXIT_NO_CHECKPOINT
    if tuple_ is None:
        print(
            f"[resume] ✗ Postgres 里没有该 thread 的 checkpoint,无从续跑;"
            f"请新建任务(退出码 {EXIT_NO_CHECKPOINT})",
            file=sys.stderr,
        )
        return EXIT_NO_CHECKPOINT

    if args.dry_run:
        ts = ""
        try:
            ts = str((tuple_.checkpoint or {}).get("ts") or "")
        except AttributeError:
            pass
        print(
            f"[resume] --dry-run:不执行续跑。最后 checkpoint 存在{f'(ts={ts})' if ts else ''}",
            file=sys.stderr,
        )
        return EXIT_OK

    values = dict(snapshot.values or {})
    draft_path = values.get("draft_path")
    if draft_path:
        from draft_ops.draft_lock import assert_resume_ready

        guard = assert_resume_ready(
            draft_path,
            require_jianying_closed=not args.allow_jianying_running,
        )
        for name, sha in (guard.get("hashes") or {}).items():
            print(f"[resume] sha256[{name}]={sha}", file=sys.stderr)
        if not guard["ok"]:
            print("[resume] ✗ 续跑前检查未通过(退出码 6):", file=sys.stderr)
            for p in guard["problems"]:
                print(f"[resume]   - {p}", file=sys.stderr)
            return EXIT_RESUME_GUARD
    else:
        print(
            "[resume] state 无 draft_path(失败发生在草稿产出前),跳过草稿文件门禁",
            file=sys.stderr,
        )

    # None 输入续跑:run_with_events 把 None 输入按 resume 处理(发 run.resumed),
    # LangGraph 从最后 checkpoint 恢复,失败 superstep 写入未提交 → 失败节点重执行。
    from monitoring.workflow_runner import run_with_events

    result = await run_with_events(g, None, config, on_event=_print_event)

    # 续跑后的落点提示:可能停在下一道人工关卡(正常挂起),也可能已到终点。
    after = await g.aget_state(config)
    pending_after = describe_interrupts(after)
    if pending_after:
        print(
            "[resume] ⏸ 已停在下一道人工关卡:"
            f"{[i.get('checkpoint') for i in pending_after]}",
            file=sys.stderr,
        )
    else:
        print("[resume] ✓ 续跑执行完成,无新的挂起关卡", file=sys.stderr)

    rc = _check_final_state(result)
    return rc


async def _resume_async_impl(args: argparse.Namespace) -> int:
    """恢复主体:校验挂起 → Command(resume) → run_with_events(§4.2 任务 4/5)。

    2026-11-17:无挂起关卡且给了 ``--continue`` 时改走 :func:`_continue_after_failure`
    (失败节点重试);不给 ``--continue`` 保持原行为(退出码 4 拒绝)。
    2026-11-18 P0-04(审查计划 §4.3):初始化链路(应用层迁移 → checkpointer
    连接 → 图构建 → checkpoint 状态读取)整体 fail-closed —— 任一段异常都按
    「数据库不可用/初始化失败」返回退出码 7,不让异常绕过统一退出码语义
    (未捕获异常会以退出码 1「图执行失败」误导操作员),更不许带病进入
    ``run_with_events``。
    """
    # P0-06(2026-11-18):attempt 关联标识由创建台经 env 注入,与
    # console_run.log / resumes[] 审计记录一一对应;手动 CLI 调用无此 env,
    # 打 "-" 不失败。
    attempt_id = str(os.environ.get("AUTO_VIDEO_RESUME_ATTEMPT_ID") or "").strip()
    print(f"[resume] attempt={attempt_id or '-'} thread={args.thread_id}", file=sys.stderr)

    from graph import build_graph, get_checkpointer, normalize_thread_id

    thread_id = normalize_thread_id(args.thread_id)
    # 应用层迁移先行(fail-closed,与 run_workflow 同口径)。
    from db.migrations import apply_migrations_to_db

    stage = "应用层迁移"
    checkpointer_cm = None
    saver = None
    try:
        apply_migrations_to_db()
        stage = "checkpointer 初始化(PostgreSQL 连接)"
        checkpointer_cm = get_checkpointer()
        saver = await checkpointer_cm.__aenter__()
        stage = "工作流图构建"
        g = build_graph(
            checkpointer=saver,
            thread_id=thread_id,
            start_heartbeat_thread=False,
            run_preflight=False,
        )
        config = {"configurable": {"thread_id": thread_id}}
        stage = "checkpoint 状态读取(aget_state)"
        snapshot = await g.aget_state(config)
    except Exception as e:  # noqa: BLE001 — 初始化任何一段失败都统一退出码 7
        print(
            f"[resume] ✗ 恢复初始化失败 —— 阶段: {stage};"
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        print(
            f"[resume]   数据库不可用或初始化失败,拒绝恢复/续跑;"
            f"请先恢复数据库连接再重试,不要新建任务盲目重跑(退出码 {EXIT_NO_CHECKPOINT})",
            file=sys.stderr,
        )
        if checkpointer_cm is not None and saver is not None:
            try:
                await checkpointer_cm.__aexit__(type(e), e, e.__traceback__)
            except Exception:  # noqa: BLE001 — 清理失败不掩盖退出码语义
                pass
        return EXIT_NO_CHECKPOINT

    try:
        # §4.2 任务 4:恢复前读挂起信息,校验 checkpoint 确属该 thread。
        interrupts = describe_interrupts(snapshot)
        _print_interrupts(interrupts)
        if not interrupts:
            if not getattr(args, "continue_run", False):
                print(
                    f"[resume] ✗ thread={thread_id!r} 无挂起关卡可恢复(退出码 {EXIT_NOT_SUSPENDED});"
                    f"如需从失败处续跑(重跑失败节点),请加 --continue",
                    file=sys.stderr,
                )
                return EXIT_NOT_SUSPENDED
            return await _continue_after_failure(g, saver, config, args, snapshot)

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

        # 阶段 3(人工关卡与剪映草稿导入):恢复前文件安全门禁。
        # dry-run 不受影响(只读);真实恢复前要求剪映已关闭 + 草稿可解析,
        # 通过时把人工修改后草稿的 SHA-256 打到 stderr 供审计留档。
        from draft_ops.draft_lock import assert_resume_ready

        guard = assert_resume_ready(
            (snapshot.values or {}).get("draft_path"),
            require_jianying_closed=not args.allow_jianying_running,
        )
        for name, sha in (guard.get("hashes") or {}).items():
            print(f"[resume] sha256[{name}]={sha}", file=sys.stderr)
        if not guard["ok"]:
            print("[resume] ✗ 恢复前检查未通过(退出码 6):", file=sys.stderr)
            for p in guard["problems"]:
                print(f"[resume]   - {p}", file=sys.stderr)
            return EXIT_RESUME_GUARD

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
    finally:
        await checkpointer_cm.__aexit__(None, None, None)


async def _main_async(args: argparse.Namespace) -> int:
    if args.production:
        os.environ["WORKFLOW_ENV"] = "production"
    if not str(args.thread_id or "").strip():
        print("[resume] ✗ --thread-id 不能为空", file=sys.stderr)
        return EXIT_PREFLIGHT
    if getattr(args, "continue_run", False) and str(args.checkpoint or "").strip():
        print(
            "[resume] ✗ --continue 与 --checkpoint 互斥:续跑针对无挂起关卡的失败运行,"
            "无需指定关卡;要恢复挂起关卡请去掉 --continue",
            file=sys.stderr,
        )
        return EXIT_PREFLIGHT

    # 附件评审 20261105 阶段3G / 决策 D3-A:与 run_workflow 同款入口 A 的
    # LLM 后端提示(红线5"生产不得静默使用 stub")。显式给了 --llm-backend
    # 则尊重调用方;否则提示假答案;若还设了 VIDEO_LLM_BACKEND(入口 A 不读
    # 它)且非 stub,以退出码 2 拒跑。置于预检之前,拒跑时不做任何环境准备。
    from scripts._entry_a_llm import llm_backend_notice

    rc = llm_backend_notice(getattr(args, "llm_backend", None))
    if rc != 0:
        return rc

    rc = _maybe_preflight(args)
    if rc != 0:
        return rc
    return await _resume_async(args)


def main() -> int:
    args = _parse_args()
    try:
        # Windows: psycopg async 需 SelectorEventLoop(与 run_workflow 同口径)。
        # ⚠️ 全进程只允许这一层 asyncio.run —— 2026-11-06 阶段0 诊断发现旧版在
        # 同步 _main_async 里又嵌套了一层 asyncio.run(默认 ProactorEventLoop),
        # async psycopg 连接直接 InterfaceError,Windows 上 CLI 恢复必然失败。
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
