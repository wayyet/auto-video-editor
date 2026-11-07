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
    "cancel_run",
    "console_run_json_path",
    "get_console_run",
    "list_console_runs",
    "prepare_run",
    "reconcile_run_state",
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
            if timed_out:
                _mark_record(
                    _task_dir_for(tid),
                    {
                        "status": "failed",
                        "error": f"运行超时(>{timeout:.0f}s),已强制终止",
                        "failed_at": _now_iso(),
                    },
                )


def reap_now() -> None:
    """惰性收割入口(超时清理;由 API 查询/写路径调用)。"""
    with _ACTIVE_LOCK:
        _prune_active_procs()


def active_thread_ids() -> list[str]:
    """本实例仍在册的活跃 thread_id 列表(先收割再读,调用方无需先 reap_now)。

    与 ``cancel_run`` 同一限制:只覆盖**本服务实例**管理的子进程;
    由入口 A(scripts/run_workflow.py 直启)跑的任务不在册,缓存清理等
    调用方不得据此认定"全系统无运行中任务"。
    """
    with _ACTIVE_LOCK:
        _prune_active_procs()
        return list(_ACTIVE_PROCS.keys())


def _build_launch_env(task_dir: Path) -> dict[str, str]:
    """子进程 env:继承受控环境 + staging 草稿目录 + production(计划 §7.4)。

    阶段 1(人工关卡与剪映草稿导入)起 ``AUTO_VIDEO_EDITOR_DRAFT_DIR`` 只指向
    **staging**(任务目录 drafts/);node_05 写完 staging 后会发布注册到
    ``config.JIANYING_DRAFT_ROOT`` 的正式目录并把后续节点切过去
    (见 ``draft_ops/draft_publisher.py``)。outputs 目录仅负责 staging 与任务产物。
    """
    env = dict(os.environ)
    env["AUTO_VIDEO_EDITOR_DRAFT_DIR"] = str((task_dir / "drafts").resolve())
    env["WORKFLOW_ENV"] = "production"
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


def get_console_run(
    thread_id: str, *, outputs_root: Path | None = None
) -> dict[str, Any]:
    """单条详情(record + 调和态);不存在 → 404。"""
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
    }


def _resume_draft_guard(thread_id: str) -> dict[str, Any]:
    """恢复前文件安全门禁(阶段 3;独立出口便于单测注入)。

    剪映必须已关闭 + 草稿存在/JSON 可解析/mtime 合理;返回
    ``{"ok", "problems", "hashes"}``(hashes 为人工修改后草稿的 SHA-256 审计值)。
    取数层 ``web.gate_state`` 延迟 import,避免模块级循环依赖。
    """
    from web.gate_state import resume_guard

    return resume_guard(thread_id)


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

    Args:
        thread_id: 目标任务(事件库派生状态必须 waiting_human,否则 409)。
        checkpoint: 关卡标识(①/②/③ 或 interrupt id);省略时仅当
            "恰好一个挂起关卡"可自动匹配(否则子进程退出码 5,日志可查)。
        value: 放行值(JSON 语义;经 json.dumps 后交 --value)。
        draft_guard: 门禁函数(缺省 :func:`_resume_draft_guard`;单测注入)。

    Raises:
        ConsoleLaunchError: 404(任务不存在)/ 409(非等待态/并发满/已在运行/
            门禁不过)/ 503(PG 或记录 stub 未放行)/ 500(Popen 失败或记录
            后端非法)。
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
    if state != "waiting_human":
        raise ConsoleLaunchError(
            f"任务 {thread_id} 未处于人工等待状态(当前: {state}),不可恢复",
            status_code=409,
        )

    # 阶段 3:恢复前文件安全门禁(剪映未关闭 / 草稿缺失损坏 → 409,不盲发 resume)
    guard_result = guard(thread_id)
    if not guard_result.get("ok"):
        raise ConsoleLaunchError(
            "恢复前检查未通过:" + ";".join(guard_result.get("problems") or []),
            status_code=409,
        )

    with _ACTIVE_LOCK:
        _prune_active_procs()
        if thread_id in _ACTIVE_PROCS:
            raise ConsoleLaunchError(
                f"任务 {thread_id} 已有活跃进程", status_code=409
            )
        limit = int(config.CONSOLE_MAX_CONCURRENT_RUNS)
        if len(_ACTIVE_PROCS) >= limit:
            raise ConsoleLaunchError(
                f"活跃任务数已达上限({limit}),请稍后重试", status_code=409
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
        "--value", json.dumps(value, ensure_ascii=False),
        "--llm-backend", backend,
    ]
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
                env=_build_launch_env(task_dir),
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
            "at": _now_iso(),
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
