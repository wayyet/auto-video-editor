"""节点 4a:import_video — 2026-10 拆分 node_04 新增(手动导入关卡)。

本节点**自己不导入任何视频**。它只做两件事:

1. 在 ``outputs/<job_id>/import_video_request.json`` 写"请求";
2. 轮询同目录的 ``import_video_result.json``,等 Web UI 左上角【📥 导入视频】
   按钮被点击后由 ``POST /api/system/import-video`` 端点回写结果。

真正的"选文件 + 上传"全部发生在 OpenStoryline Web 端一侧
(``web/static/app.js::importVideo`` 复用既有 ``uploadMediaChunked``)。
这是"项目本身绝不自动导入"的**物理保证**,而不只是一条约定。

握手方式为**文件**而非 ``interrupt()``,与仓库里已跑通的 ``clean_cache`` 模式
完全同构(节点函数只被 HTTP 端点调用,图的 START 边已剥离,
``tests/integration/test_no_auto_import_video.py`` 做守护测试)。

--------------------------------------------------------------------------
硬约束:轻依赖(跨 venv 契约)
--------------------------------------------------------------------------
本模块会被 ``openstoryline/.venv`` 里的 ``agent_fastapi.py`` **跨 venv 导入**
(与既有 ``clean_cache`` 端点同一手法),因此顶层 import 只允许:

    stdlib + ``config`` + ``storyline.output_isolation``

**禁止** ``langgraph`` / ``pydantic`` / 任何 ``nodes.*`` 图节点模块。
``tests/unit/test_node_04a_import_video.py`` 有一条**源码文本断言**守住这条
硬约束(比运行时试 import 更早失败,且不依赖 openstoryline venv)。
同理,类型标注一律用内置 ``dict``,**不要** ``from state import WorkflowState``
——那会诱导后人顺手加更重的依赖。

--------------------------------------------------------------------------
config 读取约定
--------------------------------------------------------------------------
``IMPORT_VIDEO_WAIT_TIMEOUT_S`` / ``IMPORT_VIDEO_POLL_INTERVAL_S`` /
``IMPORT_VIDEO_MANUAL_REQUIRED`` 三项都是**运行时**通过 ``config.XXX`` 读的
(不是模块顶层 ``from config import``),因此集成测试可以用
``monkeypatch.setattr(config, "IMPORT_VIDEO_WAIT_TIMEOUT_S", 0)`` 把轮询压到
0 秒,避免测试挂起。与 ``graph.py:575`` 的 ``import config as _config`` 同一惯例。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
from storyline.output_isolation import OutputJobPaths


# ---------------------------------------------------------------------------
# 握手文件名常量(Web 端点也 import 这两个常量,勿改名)
# ---------------------------------------------------------------------------
import_video_request_filename: str = "import_video_request.json"
import_video_result_filename: str = "import_video_result.json"

#: 前端 ``<input accept>`` 与后端 ``detect_media_kind`` 保持一致的扩展名白名单。
ACCEPT_EXTS: tuple[str, ...] = (".mp4", ".mov", ".mkv", ".avi", ".webm")
ACCEPT_MIME_PREFIXES: tuple[str, ...] = ("video/",)


# ---------------------------------------------------------------------------
# 握手文件读写(纯文件 IO,无状态)
# ---------------------------------------------------------------------------
def _job_paths(job_id: str, root: Optional[Path] = None) -> OutputJobPaths:
    """``outputs/<job_id>/`` 路径句柄。``root`` 为 None 时走 ``config`` 默认。"""
    out_root = Path(root) if root is not None else config.storyline_outputs_root()
    return OutputJobPaths.for_job(job_id=job_id, root=out_root)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_import_request(job_id: str, root: Optional[Path] = None) -> Path:
    """写 ``outputs/<job_id>/import_video_request.json`` 并返回其路径。"""
    paths = _job_paths(job_id, root)
    paths.ensure()
    payload = {
        "job_id": job_id,
        "requested_at": _utcnow_iso(),
        "accept_exts": list(ACCEPT_EXTS),
        "accept_mime_prefixes": list(ACCEPT_MIME_PREFIXES),
    }
    target = Path(paths.root) / import_video_request_filename
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def read_import_result(job_id: str, root: Optional[Path] = None) -> Optional[dict]:
    """读 ``import_video_result.json``;不存在/损坏返回 ``None``。"""
    try:
        target = _job_paths(job_id, root).root / import_video_result_filename
    except Exception:  # noqa: BLE001 - root 非法时按"还没有结果"处理
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 文件不存在 / 半写(写入途中被读到)→ 视为还没结果,继续轮询
        return None
    return data if isinstance(data, dict) else None


def write_import_result(
    job_id: str, payload: dict, root: Optional[Path] = None
) -> Path:
    """写 ``outputs/<job_id>/import_video_result.json`` 并返回其路径。

    **Web 端点专用**(也用于 ``IMPORT_VIDEO_MANUAL_REQUIRED=False`` 的回滚路径)。
    先写临时文件再 ``os.replace``,避免节点在写入中途读到半截 JSON。
    """
    paths = _job_paths(job_id, root)
    paths.ensure()
    target = Path(paths.root) / import_video_result_filename
    tmp = target.with_suffix(".json.tmp")
    body = {"job_id": job_id, **payload}
    tmp.write_text(
        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, target)
    return target


def clear_import_request(job_id: str, root: Optional[Path] = None) -> bool:
    """删掉 ``import_video_request.json``;删成功返回 True。

    只在 :func:`import_video` 走到**非 imported** 终态(超时 / 发请求失败)时调用。
    理由:留着陈旧请求会让之后的网页按钮把它当成"当前待应答的 job"回写结果
    (没人读的死信),让人以为导入成功。故超时即清。
    """
    try:
        target = _job_paths(job_id, root).root / import_video_request_filename
        target.unlink()
        return True
    except (OSError, ValueError):
        return False


def find_pending_request(
    root: Optional[Path] = None,
) -> Optional[tuple[str, dict]]:
    """扫 ``<root>/*/import_video_request.json``,返回**最新未应答**的一条。

    返回 ``(job_id, request_payload)``;没有待处理的请求时返回 ``None``。

    刻意放在本模块而**不是** ``agent_fastapi.py``:
    - 端点只认"扫出来的那一条",**不接受任意 ``job_id`` 入参** → 网页侧无法
      诱导把结果写进别的任务目录(安全设计)。
    - 这样才能被单测直接覆盖,不必起 uvicorn。

    排序用 ``mtime``(请求文件写入时刻)而非目录名,后者是随机 hex。
    """
    out_root = Path(root) if root is not None else config.storyline_outputs_root()
    try:
        candidates = sorted(Path(out_root).glob(f"*/{import_video_request_filename}"))
    except OSError:
        return None

    pending: list[tuple[float, str, dict]] = []
    for req_file in candidates:
        try:
            payload = json.loads(req_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        job_id = str(payload.get("job_id") or req_file.parent.name)
        # 已应答的请求跳过(同 job 重复点击时不会重复命中)
        if (req_file.parent / import_video_result_filename).exists():
            continue
        try:
            mtime = req_file.stat().st_mtime
        except OSError:
            mtime = 0.0
        pending.append((mtime, job_id, payload))

    if not pending:
        return None
    _, job_id, payload = max(pending, key=lambda item: item[0])
    return job_id, payload


# ---------------------------------------------------------------------------
# 图节点入口
# ---------------------------------------------------------------------------
def import_video(
    state: dict,
    *,
    outputs_root: Optional[Path] = None,
    poll_interval_s: Optional[float] = None,
    timeout_s: Optional[int] = None,
) -> dict:
    """发请求 + 等结果。图节点名即 ``import_video``。

    Args:
        state: 当前工作流状态(需含 ``session_id`` / ``video_input_path``)。
        outputs_root: 注入位(测试用),auto-video-editor 端 ``outputs/{job_id}`` 根。
        poll_interval_s: 注入位,None 时读 ``config.IMPORT_VIDEO_POLL_INTERVAL_S``。
        timeout_s: 注入位,None 时读 ``config.IMPORT_VIDEO_WAIT_TIMEOUT_S``。

    Returns:
        state 的浅拷贝 + 4 个 ``import_video_*`` 字段 + ``status_log`` /
        ``error_log``。三种非 ``imported`` 终态(failed / timeout)都**正常返回
        而不抛异常**,让图继续走到 ``get_storyboard_plan`` —— 后者在没有 plan
        产物时会走既有的 ``_fallback_to_shot_plan`` 降级,符合本仓库
        "环境级失败降级、不阻塞主链"的既有纪律(见 ``node_05_generate_draft.py``)。
    """
    errors = list(state.get("error_log", []) or [])
    statuses = list(state.get("status_log", []) or [])
    job_id = state.get("session_id")

    # 1. 没有 job_id → 无法定位握手目录,记错早退(不抛异常)
    if not job_id:
        errors.append("[import_video] state 缺少 session_id,无法发起导入请求")
        return {
            **state,
            "import_video_status": "failed",
            "import_video_media_path": None,
            "import_video_web_session_id": None,
            "import_video_filename": None,
            "status_log": statuses + ["import_video_failed"],
            "error_log": errors,
        }

    job_id = str(job_id)

    # 2. 幂等前置检查:结果已存在(resume / 重放)→ 直接复用,不重发请求、不重等
    existing = read_import_result(job_id, root=outputs_root)
    if existing is not None:
        return _finalize(
            state, job_id, existing, statuses, errors, reused=True
        )

    # 3. 应急回滚开关:MANUAL_REQUIRED=False 时自己把 video_input_path 当结果
    if not config.IMPORT_VIDEO_MANUAL_REQUIRED:
        video_path = state.get("video_input_path")
        auto_payload = {
            "ok": True,
            "web_session_id": None,
            "media_id": None,
            "filename": os.path.basename(str(video_path or "")) or None,
            "stored_path": str(video_path) if video_path else None,
            "triggered_by": "auto_fallback",
            "finished_at": _utcnow_iso(),
        }
        try:
            write_import_result(job_id, auto_payload, root=outputs_root)
        except OSError as e:
            errors.append(f"[import_video] 回滚路径写结果文件失败: {e}")
            return {
                **state,
                "import_video_status": "failed",
                "import_video_media_path": None,
                "import_video_web_session_id": None,
                "import_video_filename": None,
                "status_log": statuses + ["import_video_failed"],
                "error_log": errors,
            }
        return _finalize(state, job_id, auto_payload, statuses, errors, reused=False)

    # 4. 发请求
    try:
        write_import_request(job_id, root=outputs_root)
    except OSError as e:
        errors.append(f"[import_video] 写请求文件失败: {e}")
        clear_import_request(job_id, root=outputs_root)
        return {
            **state,
            "import_video_status": "failed",
            "import_video_media_path": None,
            "import_video_web_session_id": None,
            "import_video_filename": None,
            "status_log": statuses + ["import_video_failed"],
            "error_log": errors,
        }

    # 5. 轮询等待 Web 端回写结果
    interval = (
        float(poll_interval_s)
        if poll_interval_s is not None
        else float(config.IMPORT_VIDEO_POLL_INTERVAL_S)
    )
    limit = (
        float(timeout_s) if timeout_s is not None else float(config.IMPORT_VIDEO_WAIT_TIMEOUT_S)
    )
    deadline = time.monotonic() + limit

    result: Optional[dict] = None
    while True:
        result = read_import_result(job_id, root=outputs_root)
        if result is not None:
            break
        # 先判超时再 sleep:timeout_s=0 时立即退出,不睡一整轮
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.0, interval))

    if result is None:
        errors.append(
            f"[import_video] 等待网页端【导入视频】按钮超时({int(limit)}s),"
            "本节点未导入任何视频;流程继续,后续节点走降级路径。"
            "如需放宽,调大环境变量 IMPORT_VIDEO_WAIT_TIMEOUT_S。"
        )
        # 超时即清请求:避免陈旧请求被之后的网页按钮当成"当前待应答 job"回写。
        clear_import_request(job_id, root=outputs_root)
        return {
            **state,
            "import_video_status": "timeout",
            "import_video_media_path": None,
            "import_video_web_session_id": None,
            "import_video_filename": None,
            "status_log": statuses + ["import_video_timeout"],
            "error_log": errors,
        }

    return _finalize(state, job_id, result, statuses, errors, reused=False)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _finalize(
    state: dict,
    job_id: str,
    result: dict,
    statuses: list[str],
    errors: list[str],
    *,
    reused: bool,
) -> dict:
    """把 result 文件内容映射成 4 个 ``import_video_*`` state 字段。"""
    if result.get("ok"):
        return {
            **state,
            "import_video_status": "imported",
            "import_video_media_path": result.get("stored_path"),
            "import_video_web_session_id": result.get("web_session_id"),
            "import_video_filename": result.get("filename"),
            "status_log": statuses + ["import_video_done"],
            "error_log": errors,
        }

    reason = str(result.get("error") or result.get("detail") or "unknown")
    errors.append(f"[import_video] 网页端导入失败: {reason}")
    statuses.append("import_video_failed")
    if reused:
        statuses.append("import_video_reused")
    return {
        **state,
        "import_video_status": "failed",
        "import_video_media_path": None,
        "import_video_web_session_id": None,
        "import_video_filename": None,
        "status_log": statuses,
        "error_log": errors,
    }


__all__ = [
    "import_video",
    "write_import_request",
    "read_import_result",
    "write_import_result",
    "clear_import_request",
    "find_pending_request",
    "import_video_request_filename",
    "import_video_result_filename",
    "ACCEPT_EXTS",
    "ACCEPT_MIME_PREFIXES",
]
