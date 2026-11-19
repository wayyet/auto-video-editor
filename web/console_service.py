"""创建台运行编排(第二期阶段 1-2 + 阶段 5 增强)。

- :func:`prepare_run`(阶段 1)= 上传落盘 → 构建 initial state → 写 ``console_run.json``;
  任何一步失败:删除本次新建的任务目录,不留"可运行"的半成品(计划 §6.2 规则 7)。
- :func:`start_run`(阶段 2)= PG 预检 → LLM 后端门禁 → ``subprocess.Popen``
  启动既有 ``scripts/run_workflow.py``(唯一执行适配器,不另建 LangGraph runner,
  计划 §7.1/§7.2);API 不等待工作流结束。
- 启动纪律(计划 §7.3):argv 数组 + ``shell=False`` + ``cwd`` 固定仓库根;
  提示词只进 JSON State 不进命令行;独立进程组;独立草稿目录
  (``AUTO_VIDEO_EDITOR_DRAFT_DIR``,计划 §7.4);PID/日志落盘。
- 阶段 5 增强(计划 §11 阶段 5 核心四项):
  - :func:`list_console_runs` / :func:`get_console_run` 历史列表/详情(状态调和,
    事件库为事实来源);
  - :func:`resume_run` 人工关卡恢复(复用 ``scripts/resume_workflow.py``,入口 A 同款);
  - :func:`cancel_run` 任务取消(仅限本服务实例注册的活跃进程,防 PID 复用误杀);
  - 运行超时收割 ``CONSOLE_RUN_TIMEOUT_SEC``(惰性,随查询/写路径触发)。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import config  # 运行时读 config.CONSOLE_*(可 monkeypatch),不在 import 期固化值
from web.console_uploads import (
    CONSOLE_THREAD_ID_RE,
    ConsoleUploadError,
    save_uploaded_videos,
    validate_thread_id,
)
from web.console_state import build_initial_state, new_thread_id, write_initial_state_json

#: 允许经创建台启动的 LLM 后端(与 run_workflow.py --llm-backend choices 对齐;
#: gate 后端需要 mailbox,归 agent_gate CLI/MCP 管,不进创建台)
CONSOLE_LAUNCH_BACKENDS: tuple[str, ...] = ("stub", "claude-cli", "mcode-cli")


def _now_iso() -> str:
    """UTC ISO 时间戳(记录用;本地时区无关紧要,统一 UTC 便于排序)。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def console_run_json_path(task_dir: Path) -> Path:
    return Path(task_dir) / "console_run.json"


def _write_run_json(task_dir: Path, record: dict) -> Path:
    path = console_run_json_path(task_dir)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def prepare_run(
    *,
    prompt: str,
    uploads: list[Any],
    target_duration_ms: int,
    ratio: str,
    lang: str,
    required_features: dict[str, bool] | None = None,
    outputs_root: Path | None = None,
    probe: Callable[[Path], dict[str, Any]] | None = None,
    speed_multiplier: float = 1.0,
) -> dict:
    """接收一次创建请求,完成"素材落盘 + 初始 State + 运行记录"。

    Args:
        prompt: 用户提示词(API 层已做非空/长度校验;此处不再重复截断)。
        uploads: 上传文件对象列表(透传给 :func:`save_uploaded_videos`)。
        target_duration_ms / ratio / lang: 结构化参数(API 层已校验)。
        required_features: 阶段 6 必需功能显式声明(9 类布尔;None = 由 QA 侧
            按运行时能力推导);透传 build_initial_state 落进 initial state。
        outputs_root: 任务根目录;缺省读 ``config.CONSOLE_OUTPUTS_ROOT``(运行时读)。
        probe: 视频探测函数(缺省 ffprobe;单测注入)。
        speed_multiplier: 2026-11-14 快进速度(倍),API 层已校验,透传
            build_initial_state 落进 ``storyline_targets``。

    Returns:
        ::

            {"thread_id": …, "task_dir": Path, "files": […], "initial_state": dict,
             "record": {…console_run.json 内容…}}

    Raises:
        ConsoleUploadError: 上传/校验失败(任务目录已回滚清理)。
    """
    root = (
        Path(outputs_root) if outputs_root is not None else Path(config.CONSOLE_OUTPUTS_ROOT)
    )
    thread_id = new_thread_id()
    validate_thread_id(thread_id)  # 防御性:生成器与校验器规则漂移时立即暴露
    task_dir = root / thread_id
    task_dir.mkdir(parents=True, exist_ok=False)  # 已存在 = 极小概率撞号,直接失败重生成

    try:
        files = save_uploaded_videos(
            thread_id,
            uploads,
            outputs_root=root,
            probe=probe,
        )
        state = build_initial_state(
            thread_id=thread_id,
            video_paths=[item["path"] for item in files],
            prompt=prompt,
            target_duration_ms=target_duration_ms,
            ratio=ratio,
            lang=lang,
            required_features=required_features,
            outputs_root=root,
            speed_multiplier=speed_multiplier,
        )
        state_path = write_initial_state_json(task_dir, state)
        # 预创建草稿目录(AUTO_VIDEO_EDITOR_DRAFT_DIR 的指向;阶段 2 的子进程 env 用)
        (task_dir / "drafts").mkdir(exist_ok=True)

        record = {
            "thread_id": thread_id,
            "created_at": _now_iso(),
            "status": "prepared",
            "files": [
                {
                    "path": item["path"],
                    "original_name": item["original_name"],
                    "bytes": item["bytes"],
                }
                for item in files
            ],
            "initial_state_path": str(state_path.resolve()),
        }
        _write_run_json(task_dir, record)
        return {
            "thread_id": thread_id,
            "task_dir": task_dir,
            "files": files,
            "initial_state": state,
            "record": record,
        }
    except BaseException:
        # 半成品回滚:本次任务目录是这里新建的,失败即整体移除,
        # 不留"看起来可运行"的孤儿目录(计划 §6.2 规则 7)。
        shutil.rmtree(task_dir, ignore_errors=True)
        raise


__all__ = [
    "CONSOLE_LAUNCH_BACKENDS",
    "ConsoleLaunchError",
    "ConsoleUploadError",
    "cancel_run",
    "console_run_json_path",
    "get_console_run",
    "list_console_runs",
    "prepare_run",
    "reconcile_run_state",
    "reconcile_stale_starting",
    "reap_now",
    "resume_run",
    "start_run",
]


# ---------------------------------------------------------------------------
# 阶段 2:子进程启动 + 真实 LLM 后端门禁
# ---------------------------------------------------------------------------
class ConsoleLaunchError(Exception):
    """启动失败;``status_code`` 映射 API 错误码(409/500/503)。"""

    def __init__(self, message: str, *, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


#: 活跃子进程注册表 {thread_id: Popen};进程退出后惰性清除。
#: 仅进程内内存态 —— 服务重启后由列表/详情端点按事件库调和恢复视图(阶段 5)。
_ACTIVE_PROCS: dict[str, "subprocess.Popen[bytes]"] = {}
#: 各活跃进程的启动时刻(time.monotonic 基准),超时收割用(阶段 5)。
_STARTED_AT: dict[str, float] = {}
_ACTIVE_LOCK = threading.Lock()
#: 启动/恢复「占用登记表」(2026-11-18 P0-01/02,审查计划 §4.1):
#: 从状态门禁通过后、子进程启动前独占 thread_id,到进程注册进 _ACTIVE_PROCS
#: (转正)或启动链路任一步失败(finally 释放)为止。修复"锁内检查活跃进程
#: → 放锁 → spawn → 注册"窗口内两个并发请求同时通过检查的竞态。
#: 与 _ACTIVE_PROCS 同为进程内内存态:当前生产部署是**单 uvicorn 进程**
#:(start_production.ps1 无 --workers),进程内锁即跨请求互斥;若未来多
#: worker/多实例部署,必须换成共享存储(DB 锁/租约),不得以本表为唯一保障。
_CLAIMED_TIDS: set[str] = set()


def _check_postgres(timeout: float = 5.0) -> None:
    """PG 预检(fail-closed,计划 §11 阶段 2"加入 PostgreSQL 启动检查")。

    连不上就直接拒绝启动 —— 绝不"数据库不可用也把任务伪装成已启动"
    (计划 §9.3 同款纪律)。
    """
    from db.postgres_config import resolve_postgres_target

    try:
        target = resolve_postgres_target()
        import psycopg

        with psycopg.connect(target.uri, connect_timeout=max(1, int(timeout))):
            pass
    except Exception as e:  # noqa: BLE001 — 任何连接失败都等同"不可达"
        raise ConsoleLaunchError(
            f"PostgreSQL 不可达,拒绝启动工作流: {e}", status_code=503
        ) from e


def resolve_console_backend() -> str:
    """解析创建台 LLM 后端并执行门禁(计划 §8.2/§8.3)。

    - 未配置 ``CONSOLE_LLM_BACKEND`` → 503(不静默回落 Stub);
    - ``stub`` 需 ``CONSOLE_ALLOW_STUB=true`` 放行(生产必须真实模型);
    - 未知后端 → 503。
    """
    backend = (config.CONSOLE_LLM_BACKEND or "").strip().lower()
    if not backend:
        raise ConsoleLaunchError(
            "CONSOLE_LLM_BACKEND 未配置,拒绝启动(生产不允许静默使用 Stub)",
            status_code=503,
        )
    if backend == "stub" and not config.CONSOLE_ALLOW_STUB:
        raise ConsoleLaunchError(
            "stub 后端未放行:需显式设置 CONSOLE_ALLOW_STUB=true(仅开发/测试)",
            status_code=503,
        )
    if backend not in CONSOLE_LAUNCH_BACKENDS:
        raise ConsoleLaunchError(
            f"未知 LLM 后端: {backend!r}(允许: {', '.join(CONSOLE_LAUNCH_BACKENDS)})",
            status_code=503,
        )
    return backend


#: 创建台表单可显式指定的后端白名单;stub 永不可由浏览器指定(防静默 Stub)
CONSOLE_FORM_BACKENDS: tuple[str, ...] = ("claude-cli", "mcode-cli")


def resolve_launch_backend(requested: str | None) -> str:
    """解析一次启动的生效后端:浏览器显式选择优先,否则回落 env 门禁。

    - requested 非空:必须 ∈ ``CONSOLE_FORM_BACKENDS``,否则 400;显式选择
      即生效(不要求 env 已配置 —— 显式真实后端不构成"静默使用 Stub")。
    - requested 为空:走 :func:`resolve_console_backend`(env 门禁原样保留,
      旧客户端/curl 不带字段时行为与历史版本完全一致)。
    """
    backend = str(requested or "").strip().lower()
    if not backend:
        return resolve_console_backend()
    if backend not in CONSOLE_FORM_BACKENDS:
        raise ConsoleLaunchError(
            f"llm_backend 非法: {backend!r}(允许: {', '.join(CONSOLE_FORM_BACKENDS)})",
            status_code=400,
        )
    return backend


def resolve_record_backend(recorded: str | None) -> str:
    """恢复路径解析后端:优先用运行记录里固化的值(同一运行必须同一后端)。

    记录缺失/为空(旧任务)→ 回落 :func:`resolve_console_backend`(env 门禁);
    记录里的 stub 仍需 ``CONSOLE_ALLOW_STUB`` 放行(创建时已放行,此处防御
    配置漂移);非法值视为记录损坏 → 500。
    """
    backend = str(recorded or "").strip().lower()
    if not backend:
        return resolve_console_backend()
    if backend not in CONSOLE_LAUNCH_BACKENDS:
        raise ConsoleLaunchError(
            f"运行记录 llm_backend 非法: {backend!r}"
            f"(允许: {', '.join(CONSOLE_LAUNCH_BACKENDS)})",
            status_code=500,
        )
    if backend == "stub" and not config.CONSOLE_ALLOW_STUB:
        raise ConsoleLaunchError(
            "运行记录后端为 stub,但 CONSOLE_ALLOW_STUB 未放行,拒绝恢复",
            status_code=503,
        )
    return backend


def _default_venv_python() -> str:
    """优先用仓库主 .venv 解释器(计划 §7.1);缺失时退回当前解释器。"""
    candidate = Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def _default_popen() -> Callable[..., Any]:
    """缺省进程创建函数(独立出口,单测可 monkeypatch 而不动全局 subprocess)。"""
    return subprocess.Popen


def _prune_active_procs() -> None:
    """把已退出的子进程从注册表清除;``CONSOLE_RUN_TIMEOUT_SEC > 0`` 时执行超时收割。

    超时收割是惰性的:随 start/resume/查询路径触发,不引后台线程(阶段 5)。
    超时属硬失败:树杀 + record 置 failed(计划 §10.4 最大运行时间)。
    """
    timeout = float(getattr(config, "CONSOLE_RUN_TIMEOUT_SEC", 0.0) or 0.0)
    now = time.monotonic()
    for tid, proc in list(_ACTIVE_PROCS.items()):
        started = _STARTED_AT.get(tid)
        timed_out = False
        if proc.poll() is None and timeout > 0 and started is not None:
            if (now - started) > timeout:
                _terminate_process_tree(proc)
                timed_out = True
        if proc.poll() is not None:
            _ACTIVE_PROCS.pop(tid, None)
            _STARTED_AT.pop(tid, None)
            # P0-05(2026-11-18):进程退出即收尾 —— 退出码/结束时刻/结果写进
            # 末条 resumes[] 条目,并在事件库无终态时把 record 从 starting 收敛
            # 为 failed,绝不遗留永久 starting。收尾含一次只读事件库查询,仅在
            # 进程真实退出时触发(频率极低),锁内执行无死锁风险(snapshot 不取本锁)。
            _finalize_attempt_on_exit(tid, proc.poll())
            if timed_out:
                _mark_record(
                    _task_dir_for(tid),
                    {
                        "status": "failed",
                        "error": f"运行超时(>{timeout:.0f}s),已强制终止",
                        "failed_at": _now_iso(),
                    },
                )


#: CLI 退出码 → 恢复尝试结果(与 scripts/resume_workflow.py 退出码表对齐;
#: 工作流状态与尝试状态分开记录 —— 尝试结果只是审计,终态仍由事件库调和)。
_ATTEMPT_RESULT_BY_EXIT: dict[int, str] = {
    0: "succeeded",
    1: "failed_execution",
    2: "failed_preflight",
    4: "failed_preflight",
    5: "failed_preflight",
    6: "failed_preflight",
    7: "failed_preflight",
    130: "cancelled",
}


def _finalize_attempt_on_exit(thread_id: str, exit_code: int | None) -> None:
    """进程退出后收尾本次恢复尝试(P0-05;调用方持 ``_ACTIVE_LOCK``)。

    - 末条 ``resumes[]`` 条目补 ``finished_at``/``exit_code``/``result``;
    - ``exit_code != 0`` 且事件库无可用终态 → record.status 从 starting 收敛为
      failed(事件库有终态则以事件库为准,不动记录);``exit_code == 0`` 一律
      交事件库调和(waiting_human/completed 属正常落点)。
    """
    # 注意:exit_code=0 是 falsy,不能用 `exit_code or -1` 兜底(None 才是未知)
    key = -1 if exit_code is None else exit_code
    result = _ATTEMPT_RESULT_BY_EXIT.get(key, "failed_unknown")
    try:
        task_dir = _task_dir_for(thread_id)
        record = json.loads(console_run_json_path(task_dir).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 记录读取失败时不收尾,不掩盖主流程
        return
    # P0-05:进程退出标记 —— 服务重启对账据此区分"收割器已观察过本次退出"
    # (跳过,避免把 exit=0 正常落点误翻成 failed)与"服务死亡导致从未收割"
    # (遗留 starting,需要收敛)。
    record["last_proc_exit"] = {"exit_code": exit_code, "at": _now_iso()}
    resumes = record.get("resumes") or []
    if resumes and isinstance(resumes[-1], dict) and "finished_at" not in resumes[-1]:
        resumes[-1]["finished_at"] = _now_iso()
        resumes[-1]["exit_code"] = exit_code
        resumes[-1]["result"] = result
        record["resumes"] = resumes
    if exit_code not in (0, None) and record.get("status") not in (
        "failed", "cancelled", "prepared", "completed",
    ):
        snap = _latest_snapshot(thread_id)
        derived = _derive_state_of(snap.get("last_event")) if snap.get("available") else None
        if derived not in ("completed", "failed", "waiting_human"):
            record["status"] = "failed"
            record["error"] = (
                f"恢复/运行进程已退出(exit={exit_code},result={result})且事件库无终态;"
                "如需继续可从 checkpoint 重试"
            )
            record["failed_at"] = _now_iso()
    try:
        _write_run_json(task_dir, record)
    except Exception:  # noqa: BLE001
        return


#: 服务重启对账节流(秒;time.monotonic 基准)—— reap_now 随每次查询触发,
#: 全目录扫描有成本,60s 一次足够对账;force 参数供单测绕过节流。
_STALE_RECONCILE_INTERVAL_SEC = 60.0
_last_stale_reconcile: float = 0.0


def reconcile_stale_starting(
    *, outputs_root: Path | None = None, force: bool = False
) -> list[str]:
    """服务重启对账(P0-05 审查计划 §4.4 条目 6;对应验收 P2-09)。

    扫描任务根目录,把同时满足以下条件的 starting/running 记录收敛为明确终态:
    - 不在本实例注册表/占用表(活跃进程由收割器负责);
    - 记录 PID 已死或缺失(还活着 → 可能是入口 A 直启,PID 复用风险下绝不误判)。

    收敛规则(事件库是事实来源):
    - 派生 ``waiting_human`` → 不动(正常停在人工关卡的稳态);
    - 派生 ``completed`` → record.status = completed;
    - 其余(派生 failed/running/无事件)→ record.status = failed 并附对账说明:
      进程已死且无终态事件,唯一诚实的解释是异常终止或服务重启;恢复重试仍可
      从 checkpoint 继续。末条无 ``finished_at`` 的 resumes[] 条目同步收尾。

    Returns:
        本次收敛的 thread_id 列表(节流窗口内返回空列表)。
    """
    global _last_stale_reconcile
    now = time.monotonic()
    if not force and (now - _last_stale_reconcile) < _STALE_RECONCILE_INTERVAL_SEC:
        return []
    _last_stale_reconcile = now
    root = (
        Path(outputs_root) if outputs_root is not None else Path(config.CONSOLE_OUTPUTS_ROOT)
    )
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    converged: list[str] = []
    for child in entries:
        if not child.is_dir() or not CONSOLE_THREAD_ID_RE.match(child.name):
            continue
        with _ACTIVE_LOCK:
            if child.name in _ACTIVE_PROCS or child.name in _CLAIMED_TIDS:
                continue
        record_path = console_run_json_path(child)
        if not record_path.exists():
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 损坏记录跳过
            continue
        if record.get("status") not in ("starting", "running"):
            continue
        if _pid_alive(record.get("pid")) is True:
            continue
        # 只收敛"服务死亡导致从未被收割"的遗留记录:末条 attempt 无 finished_at
        # (该次退出的收尾没发生过),或从未有任何收割标记。收割器已观察过本次
        # 进程退出的记录(无论 exit 0/非 0)一律跳过 —— exit=0 的正常落点
        # (waiting_human/completed)不因事件库暂时不可达被误翻成 failed。
        resumes_probe = record.get("resumes") or []
        unfinished_attempt = bool(resumes_probe) and isinstance(resumes_probe[-1], dict) \
            and "finished_at" not in resumes_probe[-1]
        if not unfinished_attempt and record.get("last_proc_exit") is not None:
            continue
        snap = _latest_snapshot(child.name)
        derived = _derive_state_of(snap.get("last_event")) if snap.get("available") else None
        resumes = record.get("resumes") or []
        if resumes and isinstance(resumes[-1], dict) and "finished_at" not in resumes[-1]:
            resumes[-1]["finished_at"] = _now_iso()
            resumes[-1]["exit_code"] = None
            resumes[-1]["result"] = (
                "succeeded" if derived in ("completed", "waiting_human") else "failed_execution"
            )
            record["resumes"] = resumes
        if derived == "waiting_human":
            pass  # 正常挂起稳态:调和态已正确,不改记录
        elif derived == "completed":
            record["status"] = "completed"
            converged.append(child.name)
        else:
            record["status"] = "failed"
            record["error"] = (
                "服务重启对账:进程已退出且事件库无终态事件(服务重启或进程提前退出);"
                "如需继续可从 checkpoint 重试"
            )
            record["failed_at"] = _now_iso()
            converged.append(child.name)
        try:
            _write_run_json(child, record)
        except Exception:  # noqa: BLE001 — 单条收敛失败不影响其他任务
            continue
    return converged


def reap_now() -> None:
    """惰性收割入口(超时清理;由 API 查询/写路径调用)。

    2026-11-18 P0-05 起顺带执行服务重启对账(内部 60s 节流):重启遗留的
    starting/running 记录收敛为明确终态,不遗留永久 starting。
    """
    with _ACTIVE_LOCK:
        _prune_active_procs()
    reconcile_stale_starting()


def active_thread_ids() -> list[str]:
    """本实例仍在册的活跃 thread_id 列表(先收割再读,调用方无需先 reap_now)。

    与 ``cancel_run`` 同一限制:只覆盖**本服务实例**管理的子进程;
    由入口 A(scripts/run_workflow.py 直启)跑的任务不在册,缓存清理等
    调用方不得据此认定"全系统无运行中任务"。
    """
    with _ACTIVE_LOCK:
        _prune_active_procs()
        return list(_ACTIVE_PROCS.keys())


def _try_claim_thread(thread_id: str) -> str | None:
    """原子占用一个 thread 的启动/恢复资格(2026-11-18 P0-01/02)。

    必须在启动子进程**之前**调用:占用失败(已有活跃进程 / 已被并发请求占用 /
    活跃+占用总数达上限)立即返回 409 文案,绝不排队等待 —— 前端 confirm 与
    进程内字典都不构成互斥,这里才是唯一闸门。占用成功后由调用方在启动链路
    结束(进程注册转正或任一步失败)时 :func:`_release_claim` 释放。

    人工关卡恢复与失败节点重试走同一 :func:`resume_run`,共用同一 thread 级
    占用,天然满足"两种恢复模式不得同时运行"(审查计划 P0-02)。
    """
    with _ACTIVE_LOCK:
        _prune_active_procs()
        if thread_id in _ACTIVE_PROCS:
            return f"任务 {thread_id} 已有活跃进程"
        if thread_id in _CLAIMED_TIDS:
            return f"任务 {thread_id} 已有恢复/重试进行中"
        limit = int(config.CONSOLE_MAX_CONCURRENT_RUNS)
        if len(_ACTIVE_PROCS) + len(_CLAIMED_TIDS) >= limit:
            return f"活跃任务数已达上限({limit}),请稍后重试"
        _CLAIMED_TIDS.add(thread_id)
        return None


def _release_claim(thread_id: str) -> None:
    """释放 thread 占用(与 :func:`_try_claim_thread` 配对;幂等)。"""
    with _ACTIVE_LOCK:
        _CLAIMED_TIDS.discard(thread_id)


def _build_launch_env(task_dir: Path, attempt_id: str | None = None) -> dict[str, str]:
    """子进程 env:继承受控环境 + staging 草稿目录 + production(计划 §7.4)。

    阶段 1(人工关卡与剪映草稿导入)起 ``AUTO_VIDEO_EDITOR_DRAFT_DIR`` 只指向
    **staging**(任务目录 drafts/);node_05 写完 staging 后会发布注册到
    ``config.JIANYING_DRAFT_ROOT`` 的正式目录并把后续节点切过去
    (见 ``draft_ops/draft_publisher.py``)。outputs 目录仅负责 staging 与任务产物。

    ``attempt_id``(2026-11-18 P0-06):恢复尝试关联标识经 env 注入子进程,
    由 resume_workflow.py 打进日志,与 resumes[] 审计/202 响应一一对应。
    """
    env = dict(os.environ)
    env["AUTO_VIDEO_EDITOR_DRAFT_DIR"] = str((task_dir / "drafts").resolve())
    env["WORKFLOW_ENV"] = "production"
    if attempt_id:
        env["AUTO_VIDEO_RESUME_ATTEMPT_ID"] = attempt_id
    return env


def start_run(
    task_dir: Path,
    *,
    llm_backend: str | None = None,
    popen: Callable[..., Any] | None = None,
    pg_check: Callable[[], None] | None = None,
    venv_python: str | None = None,
) -> dict:
    """启动 ``task_dir`` 里已 prepared 的工作流任务(阶段 2)。

    Args:
        task_dir: :func:`prepare_run` 生成的任务目录(console_run.json 必须存在
            且 ``status == "prepared"``)。
        llm_backend: 浏览器显式选择的后端(仅 claude-cli/mcode-cli,API 层已
            校验;空/None = 跟随服务端 CONSOLE_LLM_BACKEND 门禁)。
        popen: 进程创建函数(缺省 ``subprocess.Popen``;单测注入假实现)。
        pg_check: PG 预检函数(缺省 :func:`_check_postgres`;单测注入)。
        venv_python: 子进程解释器(缺省主 .venv)。

    Returns:
        更新后的运行记录(status="starting")。

    Raises:
        ConsoleLaunchError: 400(llm_backend 非法)/ 409(重复启动/超并发)/
            503(PG 或后端门禁)/ 500(Popen 失败)。
    """
    spawn = popen if popen is not None else _default_popen()
    check_pg = pg_check if pg_check is not None else _check_postgres

    record_path = console_run_json_path(Path(task_dir))
    if not record_path.exists():
        raise ConsoleLaunchError(f"任务不存在或未准备: {task_dir}", status_code=404)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    tid = str(record.get("thread_id") or "")
    validate_thread_id(tid)
    state_path = Path(str(record.get("initial_state_path") or ""))
    if record.get("status") != "prepared" or not state_path.exists():
        raise ConsoleLaunchError(
            f"任务 {tid} 状态为 {record.get('status')!r},不可重复启动", status_code=409
        )

    # P0-01(2026-11-18):原子占用替代"锁内检查→放锁→spawn→注册" ——
    # 占用失败立即 409,占用成功即独占该 thread 直到链路结束。
    claim_error = _try_claim_thread(tid)
    if claim_error:
        raise ConsoleLaunchError(claim_error, status_code=409)
    try:
        # fail-closed 前置检查:任一不过就拒绝启动(不伪成功)
        check_pg()
        backend = resolve_launch_backend(llm_backend)

        # argv 数组 + shell=False(计划 §7.3);提示词在 initial_state.json 里,
        # 不进命令行;--skip-preflight 依据见阶段 0 契约文档 §6。
        repo_root = Path(__file__).resolve().parents[1]
        argv = [
            venv_python if venv_python is not None else _default_venv_python(),
            "scripts/run_workflow.py",
            "--production",
            "--skip-preflight",
            "--thread-id", tid,
            "--initial-state-json", str(state_path),
            "--llm-backend", backend,
        ]
        log_path = Path(task_dir) / "console_run.log"
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)  # Windows 独立进程组
        try:
            with open(log_path, "ab") as log_file:
                proc = spawn(
                    argv,
                    shell=False,
                    cwd=str(repo_root),
                    env=_build_launch_env(Path(task_dir)),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
        except Exception as e:
            record["status"] = "failed"
            record["error"] = f"进程启动失败: {e}"
            record["failed_at"] = _now_iso()
            _write_run_json(Path(task_dir), record)
            raise ConsoleLaunchError(f"进程启动失败: {e}", status_code=500) from e

        record["status"] = "starting"
        record["pid"] = proc.pid
        record["llm_backend"] = backend
        record["started_at"] = _now_iso()
        record["log_path"] = str(log_path.resolve())
        record["argv"] = argv
        _write_run_json(Path(task_dir), record)
        with _ACTIVE_LOCK:
            _ACTIVE_PROCS[tid] = proc
            _STARTED_AT[tid] = time.monotonic()
        return record
    finally:
        # 转正或失败都释放占用;转正后的互斥由 _ACTIVE_PROCS 接管
        _release_claim(tid)


# ---------------------------------------------------------------------------
# 阶段 5:历史列表 / 状态调和 / 人工关卡恢复 / 任务取消 / 运行超时
# ---------------------------------------------------------------------------

def _task_dir_for(thread_id: str, outputs_root: Path | None = None) -> Path:
    """thread_id → 任务目录(thread_id 已过正则;再 resolve 归属校验防穿越)。"""
    root = (
        Path(outputs_root) if outputs_root is not None else Path(config.CONSOLE_OUTPUTS_ROOT)
    )
    candidate = (root / thread_id).resolve()
    root_resolved = root.resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ConsoleLaunchError(f"任务目录越界: {thread_id}", status_code=400)
    return candidate


def _mark_record(task_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """best-effort 更新 console_run.json(读取失败返回空 dict,不抛)。"""
    path = console_run_json_path(Path(task_dir))
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 记录损坏时更新不了,不掩盖调用方主流程
        return {}
    record.update(updates)
    try:
        _write_run_json(Path(task_dir), record)
    except Exception:  # noqa: BLE001
        return {}
    return record


def _terminate_process_tree(proc: Any) -> None:
    """树杀(Windows taskkill /T;POSIX terminate)。仅用于本服务注册过的进程。

    Windows 下工作流子进程会再派生 ffmpeg 等孙进程,只 terminate 本进程会留孤儿,
    因此用 taskkill /T 整树结束;kill 后短等退出,让调用方的 poll() 立即收敛。
    """
    try:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=30, check=False,
            )
        else:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 — 终止失败不掩盖调用方主流程
        pass


def _pid_alive(pid: Any) -> bool | None:
    """PID 探活;None = 无法判定(缺 PID/平台异常)。

    注意:服务重启后 PID 可能被系统复用,结果只能作启发;
    状态调和以事件库为事实来源,PID 只用于"子进程是否还活着"的辅助判断。
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_int <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_int)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:  # noqa: BLE001
            return None
    try:
        os.kill(pid_int, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但属其他用户
    except OSError:
        return None


def _latest_snapshot(thread_id: str) -> dict[str, Any]:
    """事件库快照(延迟 import;任何失败都降级为 available=False,不让历史页 500)。"""
    try:
        from monitoring.workflow_event_store import latest_snapshot

        snap = latest_snapshot(thread_id)
        return snap if isinstance(snap, dict) else {"available": False}
    except Exception:  # noqa: BLE001
        return {"available": False}


def _derive_state_of(last_event: Any) -> str:
    """复用只读监控 API 的同套"最近事件 → 运行状态"映射。"""
    try:
        from web.api_workflow import _derive_state

        return _derive_state(last_event)
    except Exception:  # noqa: BLE001
        return "no_events"


def reconcile_run_state(record: dict[str, Any], thread_id: str) -> str:
    """调和 console_run.json 记录与真实运行态(事件库是事实来源,计划 §9.3)。

    返回值词表:prepared / failed / cancelled / running / waiting_human /
    completed / starting(进程活着但尚无事件)/ unknown(进程已死且无事件,
    或事件库不可用 —— 绝不把无事件解释为成功)。
    """
    status = str(record.get("status") or "")
    if status in ("prepared", "failed", "cancelled"):
        return status
    # status == "starting"(或其它):事件库派生优先
    snap = _latest_snapshot(thread_id)
    if snap.get("available"):
        derived = _derive_state_of(snap.get("last_event"))
        if derived in ("running", "waiting_human", "completed", "failed"):
            return derived
    # 事件库无结论 → 进程探活启发(注册表内优先;PID 复用风险见 _pid_alive)
    proc = _ACTIVE_PROCS.get(thread_id)
    if proc is not None:
        return "starting" if proc.poll() is None else "unknown"
    alive = _pid_alive(record.get("pid"))
    if alive is True:
        return "starting"
    return "unknown"


def list_console_runs(
    *, limit: int = 50, outputs_root: Path | None = None
) -> list[dict[str, Any]]:
    """扫描任务目录生成历史列表(阶段 5;只读,不修改任何记录)。

    - 目录名必须匹配 ``console-<12hex>`` 且含 console_run.json,其余跳过;
    - 单条记录损坏只跳过该条,不影响整体列表;
    - 按 created_at 降序,``limit`` 截断。
    """
    root = (
        Path(outputs_root) if outputs_root is not None else Path(config.CONSOLE_OUTPUTS_ROOT)
    )
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    runs: list[dict[str, Any]] = []
    for child in entries:
        if not child.is_dir() or not CONSOLE_THREAD_ID_RE.match(child.name):
            continue
        record_path = console_run_json_path(child)
        if not record_path.exists():
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 损坏记录跳过
            continue
        runs.append(
            {
                "thread_id": child.name,
                "created_at": record.get("created_at"),
                "record_status": record.get("status"),
                "state": reconcile_run_state(record, child.name),
                "llm_backend": record.get("llm_backend"),
                "pid": record.get("pid"),
                "video_count": len(record.get("files") or []),
                "error": record.get("error"),
            }
        )
    runs.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    try:
        limit_int = max(1, int(limit))
    except (TypeError, ValueError):
        limit_int = 50
    return runs[:limit_int]


def _read_initial_state(task_dir: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    """读 initial_state.json(创建台查看模式回填表单用)。

    prompt 与剪辑参数不在 record 里,而在任务目录 initial_state.json 的
    ``storyline_targets`` 下。优先按任务目录定位(输出根迁移后仍有效),
    回退 record 里记的绝对路径;缺失/损坏返回 None,只读展示不因它失败。
    """
    candidates = [Path(task_dir) / "initial_state.json"]
    raw = str(record.get("initial_state_path") or "")
    if raw:
        candidates.append(Path(raw))
    for path in candidates:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
    return None


def get_console_run(
    thread_id: str, *, outputs_root: Path | None = None
) -> dict[str, Any]:
    """单条详情(record + 调和态 + 初始参数);不存在 → 404。"""
    validate_thread_id(thread_id)
    task_dir = _task_dir_for(thread_id, outputs_root)
    record_path = console_run_json_path(task_dir)
    if not record_path.exists():
        raise ConsoleLaunchError(f"任务不存在: {thread_id}", status_code=404)
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ConsoleLaunchError(f"运行记录损坏: {e}", status_code=500) from e
    return {
        "record": record,
        "task_dir": str(task_dir),
        "state": reconcile_run_state(record, thread_id),
        "initial_state": _read_initial_state(task_dir, record),
    }


# ---------------------------------------------------------------------------
# AI 内容明细(2026-11-19 第八期审查修订版;只读展示,零工作流副作用)
# ---------------------------------------------------------------------------

#: 字幕 cue 返回上限(审查文档 P1-10:防超量字幕/超大产物拖垮响应)
_AI_CONTENT_MAX_CUES = 500
#: 单文本字段截断上限(字符)
_AI_CONTENT_MAX_TEXT = 2000

#: web_topic 产物状态取值域(能力层三态 + 节点层 cache_hit 复用改写)
_WEB_TOPIC_STATUSES = ("ok", "cache_hit", "no_results", "failed")
#: 模板推荐产物状态取值域(节点任何情况都落盘,节点层异常降级为 unavailable)
_TEMPLATE_STATUSES = ("ok", "no_match", "unavailable")

#: 网页检索/字幕节点写进 status_log 的固定标签(证据链;标签名与节点源码一致)
_WEB_TOPIC_SKIPPED_TAG = "storyline_search_web_topic_skipped"
_WEB_TOPIC_FAILED_TAG = "storyline_search_web_topic_failed"
_GENERATE_SCRIPT_DONE_TAG = "storyline_generate_script_done"
_ZH_SUBTITLE_NODE_TAGS = ("node_08_add_subtitles_done", "node_08_add_subtitles_skipped")
_EN_TRANSLATE_DONE_TAG = "node_16a_translate_done"
_EN_TRANSLATE_SKIPPED_TAGS = ("node_16a_translate_skipped", "skipped_no_source_subtitles")

#: 仍在进行中的任务态:缺产物证据时按「等待生成」而不是「缺失」
#: (prepared=已创建未启动,同样不该显示成缺失;unknown 绝不解释成等待)
_PENDING_STATES = frozenset({"running", "starting", "waiting_human", "prepared"})


def _fetch_checkpoint_values(thread_id: str) -> dict[str, Any]:
    """读当前 thread 的 checkpoint state values(复用 ``web.gate_state`` 取数层)。

    lazy import:避免模块加载期连带 graph 构建。PG 不可达等异常由调用方
    (:func:`get_ai_content_details`)就地捕获 → 仅字幕区降级 unavailable,
    其余区块不受影响(审查文档 §5.5 部分失败策略)。本接缝同时是单测注入点。
    """
    from web.gate_state import fetch_state_values

    return dict(fetch_state_values(thread_id) or {})


def _clamp_str(value: Any, limit: int = _AI_CONTENT_MAX_TEXT) -> str | None:
    """str 化并截断;None 原样返回 None(不伪造空串)。"""
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _read_json_file(path: Path) -> tuple[Any | None, str | None]:
    """读单个 JSON 文件;返回 ``(doc, error)``,error ∈ None | "invalid"。

    文件不存在不算 error(返回 ``(None, None)``),由调用方按证据链推导状态。
    """
    if not path.exists():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError):
        return None, "invalid"


def _safe_domain(url: Any) -> str | None:
    """从 URL 提取域名;仅接受 http/https(审查文档 §6.3),解析失败返回 None。"""
    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return parsed.hostname


def _normalize_topic_entries(entries: Any) -> list[dict[str, Any]]:
    """检索条目归一化(原生 max_results=10,这里只做防线)。"""
    out: list[dict[str, Any]] = []
    if not isinstance(entries, list):
        return out
    for item in entries[:_AI_CONTENT_MAX_CUES]:
        if not isinstance(item, dict):
            continue
        url = _clamp_str(item.get("url"))
        out.append(
            {
                "title": _clamp_str(item.get("title"), 300),
                "url": url,
                "domain": _safe_domain(url),
                "snippet": _clamp_str(item.get("snippet"), 500),
            }
        )
    return out


def _normalize_template_item(item: Any) -> dict[str, Any] | None:
    """候选/选中模板对象归一化(description/score 在对象内,不在 artifact 顶层)。"""
    if not isinstance(item, dict):
        return None
    score = item.get("score")
    tags = item.get("tags")
    version = item.get("version")
    return {
        "template_id": _clamp_str(item.get("template_id"), 300),
        "description": _clamp_str(item.get("description"), 500),
        "score": score if isinstance(score, (int, float)) else None,
        "language": _clamp_str(item.get("language"), 16),
        "tags": [str(t) for t in tags[:20]] if isinstance(tags, list) else [],
        "version": version if isinstance(version, int) else None,
    }


def _normalize_cues(segments: Any, text_key: str) -> list[dict[str, Any]]:
    """checkpoint 字幕段 → cue 列表(封顶 + 字段收紧;原始毫秒保留)。"""
    if not isinstance(segments, list):
        return []
    cues: list[dict[str, Any]] = []
    for seg in segments[:_AI_CONTENT_MAX_CUES]:
        if not isinstance(seg, dict):
            continue
        cues.append(
            {
                "index": seg.get("index"),
                "start_ms": seg.get("start_ms"),
                "end_ms": seg.get("end_ms"),
                "text": _clamp_str(seg.get(text_key)),
            }
        )
    return cues


def _pending_or_missing(task_state: str) -> str:
    """无产物且无标签证据时的兜底状态:任务进行中 → pending,否则 missing。"""
    return "pending" if task_state in _PENDING_STATES else "missing"


# 引擎展示名(审查修订版 §6.3):只认产物里的证据;历史产物**不得**伪造当前引擎。
_WEB_TOPIC_UNKNOWN_ENGINE_LABEL = "未知引擎(历史记录)"
_WEB_TOPIC_ENGINE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("mediacrawler", "MediaCrawler"),
    ("google", "Google"),
    ("bing", "Bing"),
    ("duckduckgo", "DuckDuckGo"),
)


def _web_topic_engine_label(doc: Any) -> str:
    """引擎展示名推导:产物 engine 优先 → provider/条目 source 关键词 → 历史记录。

    老产物(2026-11-19 schema v2 之前生成)没有 engine 字段:有结果条目时从
    source 推导(duckduckgo_html → DuckDuckGo);失败无条目的老产物显示
    「未知引擎(历史记录)」—— 绝不默认显示当前配置的引擎。
    """
    if isinstance(doc, dict):
        engine = doc.get("engine")
        if isinstance(engine, str) and engine.strip():
            return engine.strip()
        hay = " ".join(
            [str(doc.get("provider") or "")]
            + [
                str((e or {}).get("source") or "")
                for e in (doc.get("search_web_topic") or [])
                if isinstance(e, dict)
            ]
        ).lower()
        for keyword, label in _WEB_TOPIC_ENGINE_KEYWORDS:
            if keyword in hay:
                return label
    return _WEB_TOPIC_UNKNOWN_ENGINE_LABEL


def _web_topic_section(
    task_dir: Path, task_state: str, status_log: list[str], error_log: list[str]
) -> dict[str, Any]:
    """01 网页话题检索:artifact 状态透传;无产物时按 status_log/error_log 证据链推导。

    节点在开关关闭/检索词为空/检索异常三种情况下**不写产物**(只落标签),
    因此「文件缺失 ≠ 未生成」—— 证据链优先级见审查文档 §5.4。
    """
    base: dict[str, Any] = {
        "status": None,
        "engine": _WEB_TOPIC_UNKNOWN_ENGINE_LABEL,
        "query": None,
        "searched_at": None,
        "entries": [],
        "reason": None,
        # 2026-11-20 审查阶段5:MC 回退 Bing 时透传回退状态与原因(审查 P2-07:
        # 不能只显示 Bing 而隐藏 MC 失败;reason 用产物 message,已脱敏限长)
        "fallback": None,
    }
    doc, err = _read_json_file(task_dir / "storyline" / "search_web_topic.json")
    if err == "invalid":
        return {**base, "status": "invalid", "reason": "检索产物 JSON 无法解析(文件存在但格式损坏)"}
    if isinstance(doc, dict):
        engine_label = _web_topic_engine_label(doc)
        status = doc.get("status")
        if status not in _WEB_TOPIC_STATUSES:
            return {
                **base,
                "engine": engine_label,
                "status": "invalid",
                "reason": f"检索产物状态无法识别: {status!r}",
            }
        reason = None
        if status == "failed":
            reason = _clamp_str(doc.get("message"), 300) or "检索执行失败"
        elif status == "no_results":
            reason = "检索完成,搜索引擎未返回结果"
        fallback_raw = doc.get("fallback")
        fallback = (
            {
                "to_provider": _clamp_str(fallback_raw.get("to_provider"), 64),
                "primary_error_code": _clamp_str(
                    fallback_raw.get("primary_error_code"), 64
                ),
                "reason": _clamp_str(doc.get("message"), 300),
            }
            if isinstance(fallback_raw, dict)
            else None
        )
        return {
            **base,
            "engine": engine_label,
            "status": status,
            "query": _clamp_str(doc.get("query")),
            "searched_at": _clamp_str(doc.get("searched_at"), 64),
            "entries": _normalize_topic_entries(doc.get("search_web_topic")),
            "reason": reason,
            "fallback": fallback,
        }
    if _WEB_TOPIC_SKIPPED_TAG in status_log:
        return {**base, "status": "skipped", "reason": "本次任务跳过网页检索(检索开关关闭或检索词为空)"}
    if _WEB_TOPIC_FAILED_TAG in status_log:
        detail = next(
            (line for line in error_log if "storyline:search_web_topic" in line), None
        )
        return {
            **base,
            "status": "failed",
            "reason": _clamp_str(detail, 300) or "网页检索节点执行失败(无产物落盘)",
        }
    status = _pending_or_missing(task_state)
    return {
        **base,
        "status": status,
        "reason": "等待网页检索节点生成" if status == "pending" else "任务已结束但无检索产物与状态证据",
    }


def _template_section(task_dir: Path, task_state: str) -> dict[str, Any]:
    """02 文案模板推荐:节点任何情况都落盘,状态 ok/no_match/unavailable 透传。"""
    base: dict[str, Any] = {
        "status": None,
        "template_id": None,
        "template_version": None,
        "selected_template": None,
        "candidates": [],
        "retrieval_method": None,
        "selection_method": None,
        "selected_template_content": None,
        "content_truncated": False,
        "reason": None,
    }
    doc, err = _read_json_file(task_dir / "storyline" / "script_template_plan.json")
    if err == "invalid":
        return {**base, "status": "invalid", "reason": "模板推荐产物 JSON 无法解析"}
    if not isinstance(doc, dict):
        status = _pending_or_missing(task_state)
        return {
            **base,
            "status": status,
            "reason": "等待模板推荐节点生成" if status == "pending" else "任务已结束但无模板推荐产物",
        }
    status = doc.get("status")
    if status not in _TEMPLATE_STATUSES:
        return {**base, "status": "invalid", "reason": f"模板推荐产物状态无法识别: {status!r}"}
    reason = None
    if status == "no_match":
        reason = _clamp_str(doc.get("fallback_reason"), 300) or "模板库中无匹配模板"
    elif status == "unavailable":
        reason = _clamp_str(doc.get("fallback_reason"), 300) or "模板推荐不可用"
    candidates_raw = doc.get("candidates")
    template_version = doc.get("template_version")
    return {
        **base,
        "status": status,
        "template_id": _clamp_str(doc.get("template_id"), 300),
        "template_version": (
            template_version if isinstance(template_version, (str, int)) else None
        ),
        "selected_template": _normalize_template_item(doc.get("selected_template")),
        "candidates": [
            _normalize_template_item(c)
            for c in (candidates_raw[:10] if isinstance(candidates_raw, list) else [])
            if isinstance(c, dict)
        ],
        "retrieval_method": _clamp_str(doc.get("retrieval_method"), 64),
        "selection_method": _clamp_str(doc.get("selection_method"), 64),
        "selected_template_content": _clamp_str(doc.get("selected_template_content")),
        "content_truncated": bool(doc.get("content_truncated")),
        "reason": reason,
    }


def _script_section(
    task_dir: Path,
    task_state: str,
    status_log: list[str],
    checkpoint_error: str | None = None,
) -> dict[str, Any]:
    """03 生成文案:script.json **先落盘后裁决** —— 文件存在不代表节点成功,
    完成证据以 status_log 的 done 标签为准(审查文档 §3.3)。

    checkpoint 不可读时无法核验 done 标签 → 该区降级 unavailable(不误判 failed,
    审查文档 §5.5:局部降级只影响受影响的区块)。
    """
    base: dict[str, Any] = {
        "status": None,
        "lang": None,
        "voiceover_style": None,
        "groups": [],
        "references": [],
        "reason": None,
    }
    doc, err = _read_json_file(task_dir / "storyline" / "script.json")
    done = _GENERATE_SCRIPT_DONE_TAG in status_log
    if err == "invalid":
        return {**base, "status": "invalid", "reason": "生成文案产物 JSON 无法解析"}
    if isinstance(doc, dict):
        groups_raw = doc.get("group_scripts")
        groups: list[dict[str, Any]] = []
        if isinstance(groups_raw, list):
            for g in groups_raw[:200]:
                if not isinstance(g, dict):
                    continue
                groups.append(
                    {
                        "group_id": _clamp_str(g.get("group_id"), 100),
                        "summary": _clamp_str(g.get("summary"), 500),
                        "narration": _clamp_str(g.get("narration")),
                    }
                )
        refs_raw = doc.get("references")
        references: list[dict[str, Any]] = []
        if isinstance(refs_raw, list):
            for r in refs_raw[:10]:
                if not isinstance(r, dict):
                    continue
                url = _clamp_str(r.get("url"))
                references.append(
                    {
                        "title": _clamp_str(r.get("title"), 300),
                        "url": url,
                        "domain": _safe_domain(url),
                    }
                )
        if done:
            status, reason = "ok", None
        elif checkpoint_error is not None:
            status = "unavailable"
            reason = f"{checkpoint_error};无法核验文案节点完成标签,以下为落盘内容"
        elif task_state in _PENDING_STATES:
            status, reason = "pending", "文案已落盘,等待旁白有效性裁决/节点完成"
        else:
            status = "failed"
            reason = "存在文案产物但节点未标记完成(旁白有效性裁决未通过或节点未完成,内容为裁决前落盘)"
        return {
            **base,
            "status": status,
            "lang": _clamp_str(doc.get("lang"), 16),
            "voiceover_style": _clamp_str(doc.get("voiceover_style"), 100),
            "groups": groups,
            "references": references,
            "reason": reason,
        }
    if done:
        return {
            **base,
            "status": "missing",
            "reason": "状态记录文案已完成,但产物文件缺失(可能被清理)",
        }
    status = _pending_or_missing(task_state)
    return {
        **base,
        "status": status,
        "reason": "等待生成文案节点" if status == "pending" else "任务已结束但无生成文案产物",
    }


def _subtitles_zh_section(
    values: dict[str, Any], task_state: str, status_log: list[str]
) -> dict[str, Any]:
    """04 中文字幕:无段落不一定是异常(node_08 仅在非空时写键)。"""
    base: dict[str, Any] = {
        "status": None,
        "count": 0,
        "cues": [],
        "cues_truncated": False,
        "reason": None,
    }
    segments = values.get("subtitle_segments_zh")
    if isinstance(segments, list) and segments:
        return {
            **base,
            "status": "ok",
            "count": len(segments),
            "cues": _normalize_cues(segments, "text_zh"),
            "cues_truncated": len(segments) > _AI_CONTENT_MAX_CUES,
        }
    if any(tag in status_log for tag in _ZH_SUBTITLE_NODE_TAGS):
        return {
            **base,
            "status": "skipped",
            "reason": "该任务没有生成中文字幕段(无旁白字幕源或节点跳过,属正常路径)",
        }
    status = _pending_or_missing(task_state)
    return {
        **base,
        "status": status,
        "reason": "等待中文字幕节点生成" if status == "pending" else "任务已结束但无中文字幕段",
    }


def _subtitles_en_section(
    values: dict[str, Any], task_state: str, status_log: list[str]
) -> dict[str, Any]:
    """05 英文字幕:skipped(无源字幕/缺英文草稿目录)是常态输入,不是失败。"""
    base: dict[str, Any] = {
        "status": None,
        "count": 0,
        "translation_status": None,
        "cues": [],
        "cues_truncated": False,
        "reason": None,
    }
    segments = values.get("subtitle_segments_en")
    tstat = values.get("subtitle_translation_status")
    tstat = tstat if tstat in ("real", "stub", "skipped") else None
    if isinstance(segments, list) and segments:
        return {
            **base,
            "status": "ok",
            "count": len(segments),
            "translation_status": tstat,
            "cues": _normalize_cues(segments, "text_en"),
            "cues_truncated": len(segments) > _AI_CONTENT_MAX_CUES,
        }
    if tstat == "skipped" or any(tag in status_log for tag in _EN_TRANSLATE_SKIPPED_TAGS):
        return {
            **base,
            "status": "skipped",
            "translation_status": "skipped",
            "reason": "本次未执行英文翻译(无源字幕或英文草稿目录缺失,属正常路径)",
        }
    if _EN_TRANSLATE_DONE_TAG in status_log:
        return {
            **base,
            "status": "missing",
            "reason": "状态记录翻译完成,但无英文字幕段",
        }
    status = _pending_or_missing(task_state)
    return {
        **base,
        "status": status,
        "reason": "等待英文翻译节点生成" if status == "pending" else "任务已结束但无英文字幕段",
    }


def get_ai_content_details(
    thread_id: str, *, outputs_root: Path | None = None
) -> dict[str, Any]:
    """AI 内容明细只读载荷(2026-11-19 第八期;五区块归一化,零工作流副作用)。

    - 任务存在性以 console_run.json 为准(缺失 → 404,与 :func:`get_console_run`
      对齐;outputs 下只有 storyline/ 的游离目录一律 404);
    - 三个磁盘 artifact 独立读取、独立降级(单区 invalid 不影响其它区);
    - checkpoint 字幕键经 :func:`_fetch_checkpoint_values`,不可读时仅两个字幕区
      unavailable,整体仍 200(审查文档 §5.5 部分失败策略);
    - 纯只读:不调 reap_now、不写任何文件、不触发节点。
    """
    validate_thread_id(thread_id)
    task_dir = _task_dir_for(thread_id, outputs_root)
    record_path = console_run_json_path(task_dir)
    if not record_path.exists():
        raise ConsoleLaunchError(f"任务不存在: {thread_id}", status_code=404)
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ConsoleLaunchError(f"运行记录损坏: {e}", status_code=500) from e

    task_state = reconcile_run_state(record, thread_id)

    # checkpoint 字幕键:读取失败只降级字幕区,不影响三个磁盘 artifact 区
    checkpoint_error: str | None = None
    values: dict[str, Any] = {}
    try:
        values = _fetch_checkpoint_values(thread_id)
    except Exception as e:  # noqa: BLE001 — PG 不可达/图构建失败等
        checkpoint_error = f"checkpoint 不可读: {e}"

    status_log = [str(x) for x in (values.get("status_log") or []) if str(x)]
    error_log = [str(x) for x in (values.get("error_log") or []) if str(x)]

    details: dict[str, Any] = {
        "thread_id": thread_id,
        "task_status": task_state,
        "generated_at": _now_iso(),
        "web_topic": _web_topic_section(task_dir, task_state, status_log, error_log),
        "template": _template_section(task_dir, task_state),
        "script": _script_section(task_dir, task_state, status_log, checkpoint_error),
    }
    if checkpoint_error is not None:
        details["subtitles_zh"] = {
            "status": "unavailable",
            "count": 0,
            "cues": [],
            "cues_truncated": False,
            "reason": checkpoint_error,
        }
        details["subtitles_en"] = {
            "status": "unavailable",
            "count": 0,
            "translation_status": None,
            "cues": [],
            "cues_truncated": False,
            "reason": checkpoint_error,
        }
    else:
        details["subtitles_zh"] = _subtitles_zh_section(values, task_state, status_log)
        details["subtitles_en"] = _subtitles_en_section(values, task_state, status_log)
    return details


def _resume_draft_guard(thread_id: str, *, allow_missing_draft: bool = False) -> dict[str, Any]:
    """恢复前文件安全门禁(阶段 3;独立出口便于单测注入)。

    剪映必须已关闭 + 草稿存在/JSON 可解析/mtime 合理;返回
    ``{"ok", "problems", "hashes"}``(hashes 为人工修改后草稿的 SHA-256 审计值)。
    ``allow_missing_draft``(2026-11-17 失败重试用):state 无 draft_path 时
    跳过草稿校验直接放行(失败发生在产草稿前,没有草稿可保护)。
    取数层 ``web.gate_state`` 延迟 import,避免模块级循环依赖。
    """
    from web.gate_state import resume_guard

    return resume_guard(thread_id, allow_missing_draft=allow_missing_draft)


def resume_run(
    thread_id: str,
    *,
    checkpoint: str | None = None,
    value: Any = True,
    popen: Callable[..., Any] | None = None,
    pg_check: Callable[[], None] | None = None,
    venv_python: str | None = None,
    draft_guard: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """人工关卡恢复(阶段 5):复用 ``scripts/resume_workflow.py`` 适配器。

    恢复语义与入口 A 完全一致(ADR-2026-11-04):console 创建的运行
    (``run_workflow.py`` 启动)只能由该 CLI 恢复;本函数只是把它的启动
    收进同一套 Popen 纪律(argv 数组 / shell=False / 独立进程组 / 日志追加)。

    阶段 3(人工关卡与剪映草稿导入):Popen 之前执行恢复前文件安全门禁
    (:func:`_resume_draft_guard`)—— 剪映未关闭 / 草稿缺失或损坏 → 409 拒绝,
    不盲发 Command(resume);通过时把人工修改后草稿的 SHA-256 写进
    ``console_run.json`` 的本次 resume 记录(审计)。

    2026-11-17(创建台失败重试):waiting_human 之外 ``failed`` 也放行 ——
    走 CLI ``--continue`` 续跑(None 输入,重跑失败节点;checkpoint 存在性
    由子进程 fail-closed 校验);无草稿时跳过草稿门禁,有草稿仍全量校验。

    Args:
        thread_id: 目标任务(事件库派生状态必须 waiting_human 或 failed,
            否则 409)。
        checkpoint: 关卡标识(①/② 或 interrupt id);省略时仅当
            "恰好一个挂起关卡"可自动匹配(否则子进程退出码 5,日志可查)。
            failed 重试模式下不适用(不发 --checkpoint)。
        value: 放行值(JSON 语义;经 json.dumps 后交 --value)。
            failed 重试模式下不适用(不发 --value)。
        draft_guard: 门禁函数(缺省 :func:`_resume_draft_guard`;单测注入)。

    Raises:
        ConsoleLaunchError: 404(任务不存在)/ 409(非等待态且非失败/
            并发满/已在运行/门禁不过)/ 503(PG 或记录 stub 未放行)/
            500(Popen 失败或记录后端非法)。
    """
    spawn = popen if popen is not None else _default_popen()
    check_pg = pg_check if pg_check is not None else _check_postgres
    guard = draft_guard if draft_guard is not None else _resume_draft_guard
    validate_thread_id(thread_id)
    task_dir = _task_dir_for(thread_id)
    record_path = console_run_json_path(task_dir)
    if not record_path.exists():
        raise ConsoleLaunchError(f"任务不存在: {thread_id}", status_code=404)
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ConsoleLaunchError(f"运行记录损坏: {e}", status_code=500) from e

    state = reconcile_run_state(record, thread_id)
    # 2026-11-17:failed 也放行 —— 失败节点重试(CLI --continue 续跑,无挂起
    # 关卡;checkpoint 是否存在由子进程 fail-closed 校验)。其余状态一律 409:
    # prepared 从未跑过(无 checkpoint)/ running 活跃 / completed·cancelled·
    # unknown 终态或未知。
    retry_mode = state == "failed"
    if state != "waiting_human" and not retry_mode:
        raise ConsoleLaunchError(
            f"任务 {thread_id} 未处于可恢复状态(当前: {state};"
            f"仅等待人工关卡可恢复、失败可重试),不可恢复",
            status_code=409,
        )

    # P0-01/02(2026-11-18):状态门禁通过后、启动链路之前原子占用该 thread ——
    # 人工恢复与失败重试共用本函数,同一占用天然互斥;占用失败立即 409。
    claim_error = _try_claim_thread(thread_id)
    if claim_error:
        raise ConsoleLaunchError(claim_error, status_code=409)
    try:
        # P0-06(2026-11-18):本次恢复尝试的关联标识 —— 贯穿 resumes[] 审计、
        # 子进程 env(CLI 日志行)与 202 响应,一次重试可全链路追踪。
        attempt_id = uuid.uuid4().hex[:12]
        # 阶段 3:恢复前文件安全门禁(剪映未关闭 / 草稿缺失损坏 → 409,不盲发 resume)
        # 2026-11-17:失败重试允许无草稿 —— 失败发生在产草稿前时没有草稿可保护;
        # 默认门禁透传 allow_missing_draft,注入门禁(单测)保持 (thread_id) 原签名。
        if retry_mode and draft_guard is None:
            guard_result = _resume_draft_guard(thread_id, allow_missing_draft=True)
        else:
            guard_result = guard(thread_id)
        if not guard_result.get("ok"):
            raise ConsoleLaunchError(
                "恢复前检查未通过:" + ";".join(guard_result.get("problems") or []),
                status_code=409,
            )

        check_pg()
        # 恢复的运行必须用同一真实后端:优先取创建时固化的 record["llm_backend"];
        # 记录缺失(旧任务)时回落 env 门禁(stub 仍需 CONSOLE_ALLOW_STUB 显式放行)
        backend = resolve_record_backend(record.get("llm_backend"))

        repo_root = Path(__file__).resolve().parents[1]
        argv = [
            venv_python if venv_python is not None else _default_venv_python(),
            "scripts/resume_workflow.py",
            "--production",
            "--skip-preflight",
            "--thread-id", thread_id,
            "--llm-backend", backend,
        ]
        if retry_mode:
            # 2026-11-17 失败重试:无挂起关卡,None 输入续跑;checkpoint 存在性与
            # 草稿门禁由子进程 fail-closed 校验,--value/--checkpoint 不适用。
            argv += ["--continue"]
        else:
            argv += ["--value", json.dumps(value, ensure_ascii=False)]
            if checkpoint is not None and str(checkpoint).strip():
                argv += ["--checkpoint", str(checkpoint).strip()]

        log_path = task_dir / "console_run.log"
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            with open(log_path, "ab") as log_file:
                proc = spawn(
                    argv,
                    shell=False,
                    cwd=str(repo_root),
                    env=_build_launch_env(task_dir, attempt_id=attempt_id),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
        except Exception as e:
            _mark_record(
                task_dir,
                {
                    "status": "failed",
                    "error": f"恢复进程启动失败: {e}",
                    "failed_at": _now_iso(),
                },
            )
            raise ConsoleLaunchError(f"恢复进程启动失败: {e}", status_code=500) from e

        resumes = list(record.get("resumes") or [])
        resumes.append(
            {
                # 2026-11-18 P0-06:attempt 关联标识(全链路追踪)
                "attempt_id": attempt_id,
                "at": _now_iso(),
                # 2026-11-17:retry = 失败节点重试(--continue 续跑);缺省为关卡恢复
                "mode": "retry" if retry_mode else "resume",
                "checkpoint": checkpoint,
                "value": value,
                # 阶段 3 审计:恢复时人工修改后草稿的 SHA-256
                "draft_sha256": guard_result.get("hashes") or {},
            }
        )
        record["resumes"] = resumes
        record["status"] = "starting"  # 运行继续;终态由事件库调和给出
        _write_run_json(task_dir, record)
        with _ACTIVE_LOCK:
            _ACTIVE_PROCS[thread_id] = proc
            _STARTED_AT[thread_id] = time.monotonic()
        return record
    finally:
        # 转正或失败都释放占用;转正后的互斥由 _ACTIVE_PROCS 接管
        _release_claim(thread_id)


def cancel_run(
    thread_id: str, *, terminate: Callable[[Any], None] | None = None
) -> dict[str, Any]:
    """取消运行(阶段 5):仅限本服务实例注册的活跃进程。

    服务重启后注册表为空,按记录里的 PID 直接 kill 有 PID 复用误杀风险,
    因此一律 409 拒绝(日志里留排障指引),绝不盲杀。

    Raises:
        ConsoleLaunchError: 404(任务不存在)/ 409(不在册或已退出)。
    """
    validate_thread_id(thread_id)
    task_dir = _task_dir_for(thread_id)
    record_path = console_run_json_path(task_dir)
    if not record_path.exists():
        raise ConsoleLaunchError(f"任务不存在: {thread_id}", status_code=404)
    kill = terminate if terminate is not None else _terminate_process_tree
    with _ACTIVE_LOCK:
        if thread_id in _CLAIMED_TIDS:
            raise ConsoleLaunchError(
                f"任务 {thread_id} 正在启动恢复进程,请稍后再试", status_code=409,
            )
        proc = _ACTIVE_PROCS.get(thread_id)
        if proc is None or proc.poll() is not None:
            raise ConsoleLaunchError(
                f"任务 {thread_id} 不由当前服务实例管理(服务可能已重启或进程已退出),"
                "已拒绝按 PID 误杀;如需人工终止请用任务记录中的 PID 自行核对",
                status_code=409,
            )
        kill(proc)
        _ACTIVE_PROCS.pop(thread_id, None)
        _STARTED_AT.pop(thread_id, None)
    record = _mark_record(
        task_dir,
        {
            "status": "cancelled",
            "cancelled_at": _now_iso(),
            "error": "已由操作员取消",
        },
    )
    if not record:
        raise ConsoleLaunchError("取消成功但运行记录更新失败", status_code=500)
    return record


#: 允许删除的调和态:三个终态 + prepared(已就绪未启动,从未有进程,纯惰性记录)。
#: waiting_human/running/starting 有活跃进程,unknown = 事件库不可达无法确证
#: (事件库是事实来源,绝不把"查不到"当成功),一律 409 拒删。
DELETABLE_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "prepared"}
)


def delete_run(thread_id: str) -> dict[str, Any]:
    """删除历史任务(2026-11-07):任务目录(含成片)+ PG 事件尽力清理,不可恢复。

    纪律:
    - 仅限终态/未启动记录;活跃运行与状态不明一律 409 拒删(fail-closed);
    - 双保险:服务注册表里有活跃进程即拒(调和态判完后仍可能被并发恢复);
    - 事件删除失败**不阻断**目录删除(孤儿事件无读取方危害,只随响应报告)。

    Raises:
        ConsoleLaunchError: 400(形状非法/越界)/ 404(不存在)/ 409(状态拒删)
            / 500(目录删除失败,如被播放器/同步盘占用)。
    """
    validate_thread_id(thread_id)
    task_dir = _task_dir_for(thread_id)
    record_path = console_run_json_path(task_dir)
    if not record_path.exists():
        raise ConsoleLaunchError(f"任务不存在: {thread_id}", status_code=404)
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ConsoleLaunchError(f"运行记录损坏: {e}", status_code=500) from e
    state = reconcile_run_state(record, thread_id)
    if state not in DELETABLE_STATES:
        hints = {
            "waiting_human": "任务正等待人工关卡,请先处理或取消后再删除",
            "running": "任务运行中,请等待完成或取消后再删除",
            "starting": "任务启动中,请稍后再删除",
            "unknown": "任务状态不明(事件库不可达且进程已退出),"
                       "请先恢复 Postgres 或重启服务调和状态后再删除",
        }
        raise ConsoleLaunchError(
            f"任务 {thread_id} 状态为 {state},拒绝删除:{hints.get(state, '仅终态任务可删除')}",
            status_code=409,
        )
    with _ACTIVE_LOCK:
        if thread_id in _CLAIMED_TIDS:
            raise ConsoleLaunchError(
                f"任务 {thread_id} 正在启动恢复进程,请稍后再试", status_code=409,
            )
        if thread_id in _ACTIVE_PROCS:
            raise ConsoleLaunchError(
                f"任务 {thread_id} 仍有本实例管理的活跃进程,拒绝删除", status_code=409,
            )
    events_deleted: int | None = None
    try:
        from monitoring.workflow_event_store import delete_thread_events

        events_deleted = delete_thread_events(thread_id)
    except Exception:  # noqa: BLE001 — 事件清理尽力而为,不阻断目录删除
        events_deleted = None
    try:
        shutil.rmtree(task_dir)
    except OSError as e:
        raise ConsoleLaunchError(
            f"任务目录删除失败({e});目录可能被占用(播放器/同步盘/杀毒),"
            "请关闭相关程序后重试",
            status_code=500,
        ) from e
    return {"thread_id": thread_id, "state": state, "events_deleted": events_deleted}
