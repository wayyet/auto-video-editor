"""创建台运行编排(第二期阶段 1 准备 + 阶段 2 子进程启动)。

- :func:`prepare_run`(阶段 1)= 上传落盘 → 构建 initial state → 写 ``console_run.json``;
  任何一步失败:删除本次新建的任务目录,不留"可运行"的半成品(计划 §6.2 规则 7)。
- :func:`start_run`(阶段 2)= PG 预检 → LLM 后端门禁 → ``subprocess.Popen``
  启动既有 ``scripts/run_workflow.py``(唯一执行适配器,不另建 LangGraph runner,
  计划 §7.1/§7.2);API 不等待工作流结束。
- 启动纪律(计划 §7.3):argv 数组 + ``shell=False`` + ``cwd`` 固定仓库根;
  提示词只进 JSON State 不进命令行;独立进程组;独立草稿目录
  (``AUTO_VIDEO_EDITOR_DRAFT_DIR``,计划 §7.4);PID/日志落盘。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import config  # 运行时读 config.CONSOLE_*(可 monkeypatch),不在 import 期固化值
from web.console_uploads import ConsoleUploadError, save_uploaded_videos, validate_thread_id
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
    outputs_root: Path | None = None,
    probe: Callable[[Path], dict[str, Any]] | None = None,
) -> dict:
    """接收一次创建请求,完成"素材落盘 + 初始 State + 运行记录"。

    Args:
        prompt: 用户提示词(API 层已做非空/长度校验;此处不再重复截断)。
        uploads: 上传文件对象列表(透传给 :func:`save_uploaded_videos`)。
        target_duration_ms / ratio / lang: 结构化参数(API 层已校验)。
        outputs_root: 任务根目录;缺省读 ``config.CONSOLE_OUTPUTS_ROOT``(运行时读)。
        probe: 视频探测函数(缺省 ffprobe;单测注入)。

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
            outputs_root=root,
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
    "console_run_json_path",
    "prepare_run",
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
#: 仅进程内内存态 —— 服务重启后的运行状态恢复属后续阶段(计划 §10.4)。
_ACTIVE_PROCS: dict[str, "subprocess.Popen[bytes]"] = {}
_ACTIVE_LOCK = threading.Lock()


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
    """把已退出的子进程从注册表清除(让并发配额反映"正在跑"的数量)。"""
    for tid, proc in list(_ACTIVE_PROCS.items()):
        if proc.poll() is not None:
            del _ACTIVE_PROCS[tid]


def _build_launch_env(task_dir: Path) -> dict[str, str]:
    """子进程 env:继承受控环境 + 独立草稿目录 + production(计划 §7.4)。"""
    env = dict(os.environ)
    env["AUTO_VIDEO_EDITOR_DRAFT_DIR"] = str((task_dir / "drafts").resolve())
    env["WORKFLOW_ENV"] = "production"
    return env


def start_run(
    task_dir: Path,
    *,
    popen: Callable[..., Any] | None = None,
    pg_check: Callable[[], None] | None = None,
    venv_python: str | None = None,
) -> dict:
    """启动 ``task_dir`` 里已 prepared 的工作流任务(阶段 2)。

    Args:
        task_dir: :func:`prepare_run` 生成的任务目录(console_run.json 必须存在
            且 ``status == "prepared"``)。
        popen: 进程创建函数(缺省 ``subprocess.Popen``;单测注入假实现)。
        pg_check: PG 预检函数(缺省 :func:`_check_postgres`;单测注入)。
        venv_python: 子进程解释器(缺省主 .venv)。

    Returns:
        更新后的运行记录(status="starting")。

    Raises:
        ConsoleLaunchError: 409(重复启动/超并发)/ 503(PG 或后端门禁)/ 500(Popen 失败)。
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

    with _ACTIVE_LOCK:
        _prune_active_procs()
        if tid in _ACTIVE_PROCS:
            raise ConsoleLaunchError(f"任务 {tid} 已在运行", status_code=409)
        limit = int(config.CONSOLE_MAX_CONCURRENT_RUNS)
        if len(_ACTIVE_PROCS) >= limit:
            raise ConsoleLaunchError(
                f"活跃任务数已达上限({limit}),请稍后重试", status_code=409
            )

    # fail-closed 前置检查:任一不过就拒绝启动(不伪成功)
    check_pg()
    backend = resolve_console_backend()

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
    return record
