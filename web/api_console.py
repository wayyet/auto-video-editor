"""创建台 API(第二期:上传 + State 构建 + 子进程启动 + 阶段 3 前端支撑)。

职责边界:
- 本模块是仓库里**唯一**允许"创建工作流运行"的 Web 入口;
- ``web/api_workflow.py`` 的 ``/api/workflow/*`` 保持 GET-only 只读,
  两者的边界由阶段 0 契约文档冻结,不得互相混入对方职责;
- 流程(计划 §7.2):校验 → 落盘 → initial state → PG 预检 → LLM 后端门禁
  → Popen 启动既有 ``scripts/run_workflow.py`` → 202 返回,不等待执行结束。

错误码契约(阶段 0 文档 §2.3):400 参数 / 403 Origin 或 CSRF / 409 并发或重复 /
413 超限 / 415 格式或内容 / 503 PG 或模型未就绪 / 500。

阶段 3 补充(浏览器客户端上线后的安全面):
- ``GET /api/console/config``  下发前端限制与默认值(不含 LLM 后端等敏感项);
- ``GET /api/console/csrf``    发放 CSRF token(仅同源可取);
- 写接口 CSRF 门禁:带 ``Origin`` 头(浏览器)必须携带有效 ``X-CSRF-Token``;
  无 Origin 的本机脚本/curl 豁免,保持既有 API 兼容。

阶段 5 补充(核心四项增强):
- ``GET  /api/console/runs``                 历史列表(状态调和,事件库为事实来源);
- ``GET  /api/console/runs/{thread_id}``     单条详情;
- ``POST /api/console/runs/{thread_id}/resume`` 人工关卡恢复(复用 resume_workflow.py);
- ``POST /api/console/runs/{thread_id}/cancel`` 取消运行(仅限本实例在册进程)。

2026-11-06 补充(人工关卡与剪映草稿导入 阶段 2):
- ``GET  /api/console/runs/{thread_id}/gates``       挂起关卡真实 payload(只读);
- ``POST /api/console/runs/{thread_id}/open-draft-dir`` 打开草稿目录(路径取自
  当前 Thread 的 checkpoint state,白名单根校验,绝不信任前端传路径)。

2026-11-06 补充(维护入口):
- ``POST /api/console/cache/clear``          清除可再生缓存(清理清单见 config.py
  缓存段;冷却限流 + 活跃任务 409;R-10 移除旧 /api/system/clean-cache 后,
  清理能力以此手动入口形式回归)。
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import config  # 运行时读 config.CONSOLE_*(可 monkeypatch),不在 import 期固化值
from web.api_workflow import _verify_same_origin
from web import cache_cleaner, console_service
from web.console_service import (
    CONSOLE_FORM_BACKENDS,
    ConsoleLaunchError,
    prepare_run,
    start_run,
)
from web.console_uploads import ALLOWED_VIDEO_EXTS, ConsoleUploadError, validate_thread_id
from web.human_gate_payload import COMMON_RESUME_NOTE

router = APIRouter(prefix="/api/console")

#: 契约冻结的可选值(阶段 0 文档 §2.1)
ALLOWED_RATIOS: tuple[str, ...] = ("9:16", "16:9", "4:3")
ALLOWED_LANGS: tuple[str, ...] = ("zh", "en")


class ResumeBody(BaseModel):
    """POST /runs/{tid}/resume 请求体(阶段 5)。"""

    #: 关卡标识(①/②/③ 或 interrupt id);省略时仅当恰好一个挂起关卡可自动匹配
    checkpoint: Optional[str] = None
    #: 放行值(JSON 语义,默认 true;交 resume_workflow.py --value)
    value: Any = True

# ---------------------------------------------------------------------------
# CSRF(阶段 3;阶段 0 文档 §2.4 预留项)
# ---------------------------------------------------------------------------

#: token 有效期;到期即失效,前端 403 后重新 GET /csrf 即可
CSRF_TOKEN_TTL_SEC: float = 3600.0

#: token -> 过期时间戳(monotonic 基准);服务重启即全部失效,前端重取
_CSRF_TOKENS: dict[str, float] = {}


def _prune_csrf_tokens() -> None:
    now = time.monotonic()
    for token, expiry in list(_CSRF_TOKENS.items()):
        if expiry <= now:
            _CSRF_TOKENS.pop(token, None)


def _issue_csrf_token() -> str:
    _prune_csrf_tokens()
    token = secrets.token_urlsafe(32)
    _CSRF_TOKENS[token] = time.monotonic() + CSRF_TOKEN_TTL_SEC
    return token


def _csrf_token_valid(token: str) -> bool:
    if not token:
        return False
    expiry = _CSRF_TOKENS.get(token)
    if expiry is None or expiry <= time.monotonic():
        _CSRF_TOKENS.pop(token, None)
        return False
    return True


def _enforce_csrf(request: Request) -> None:
    """浏览器(带 Origin)必须携带有效 ``X-CSRF-Token``;无 Origin 豁免。

    无 Origin 意味着非浏览器客户端(curl/本机脚本),此类请求不构成 CSRF 面;
    跨源浏览器请求已在 ``_verify_same_origin`` 被拒,这里兜"同源页面但未取
    token"的残余风险(如 DNS rebinding 场景下 Origin 可伪造为回环)。
    """
    if not request.headers.get("origin"):
        return
    token = request.headers.get("x-csrf-token", "")
    if not _csrf_token_valid(token):
        raise HTTPException(
            status_code=403,
            detail="CSRF token 缺失或无效;请先 GET /api/console/csrf 获取后随 X-CSRF-Token 头提交",
        )


def _response_urls(thread_id: str) -> dict[str, str]:
    """契约冻结的三个 URL(相对路径;复用既有只读监控 API,SSE 自动可订阅)。"""
    return {
        "monitor_url": f"/?thread_id={thread_id}",
        "snapshot_url": f"/api/workflow/runs/{thread_id}",
        "events_url": f"/api/workflow/runs/{thread_id}/events",
    }


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


@router.get("/config")
def console_config(request: Request) -> dict:
    """前端表单限制与默认值(只读;不含 LLM 后端、路径等敏感项)。"""
    _verify_same_origin(request)
    return {
        "ok": True,
        "max_videos": config.CONSOLE_MAX_VIDEOS,
        "max_video_bytes": config.CONSOLE_MAX_VIDEO_BYTES,
        "prompt_max_chars": config.CONSOLE_PROMPT_MAX_CHARS,
        "target_duration_ms_default": config.CONSOLE_TARGET_DURATION_MS_DEFAULT,
        "target_duration_ms_max": config.CONSOLE_TARGET_DURATION_MS_MAX,
        "allowed_video_exts": sorted(ALLOWED_VIDEO_EXTS),
        "ratios": list(ALLOWED_RATIOS),
        "langs": list(ALLOWED_LANGS),
    }


@router.get("/csrf")
def console_csrf(request: Request) -> dict:
    """发放 CSRF token(阶段 0 文档 §2.4 预留,随阶段 3 浏览器客户端启用)。

    跨源请求在 ``_verify_same_origin`` 已被拒;同源浏览器取 token 后随
    ``X-CSRF-Token`` 头提交写请求。token 复用至 TTL 到期。
    """
    _verify_same_origin(request)
    return {"ok": True, "token": _issue_csrf_token(), "ttl_seconds": CSRF_TOKEN_TTL_SEC}


@router.post("/runs")
def create_run(
    request: Request,
    prompt: str = Form(""),
    videos: list[UploadFile] = File(default=[]),
    target_duration_ms: Optional[int] = Form(None),
    ratio: str = Form("9:16"),
    lang: str = Form("zh"),
    llm_backend: str = Form(""),
) -> JSONResponse:
    """创建并启动一次工作流任务(第二期阶段 2 语义)。

    成功返回 202 与 thread_id,`state="starting"`(子进程已创建,执行不等待);
    同步 def 由 FastAPI 放线程池跑,落盘/Popen/PG 预检不阻塞事件循环。
    llm_backend 可选(仅 claude-cli/mcode-cli,浏览器显式选择即生效);缺省
    跟随服务端 CONSOLE_LLM_BACKEND 门禁;stub 不可由浏览器指定。
    """
    # 第二道防线:跨源浏览器请求直接拒绝(curl/同源不带 Origin 时放行)
    _verify_same_origin(request)
    # 第三道防线:同源浏览器(带 Origin)必须携带有效 CSRF token(阶段 3)
    _enforce_csrf(request)

    # --- 参数校验(手动化以便契约给 400,而非 FastAPI 默认 422) ---
    prompt_clean = str(prompt or "").strip()
    if not prompt_clean:
        raise _bad_request("prompt 不能为空")
    if len(prompt_clean) > config.CONSOLE_PROMPT_MAX_CHARS:
        raise _bad_request(
            f"prompt 过长: {len(prompt_clean)} > {config.CONSOLE_PROMPT_MAX_CHARS} 字符"
        )
    if not videos:
        raise _bad_request("至少需要上传一个视频文件")

    duration = (
        target_duration_ms
        if target_duration_ms is not None
        else config.CONSOLE_TARGET_DURATION_MS_DEFAULT
    )
    if duration <= 0 or duration > config.CONSOLE_TARGET_DURATION_MS_MAX:
        raise _bad_request(
            f"target_duration_ms 非法: {duration}(须为 0 < v ≤ "
            f"{config.CONSOLE_TARGET_DURATION_MS_MAX})"
        )
    if ratio not in ALLOWED_RATIOS:
        raise _bad_request(f"ratio 非法: {ratio!r}(允许: {', '.join(ALLOWED_RATIOS)})")
    if lang not in ALLOWED_LANGS:
        raise _bad_request(f"lang 非法: {lang!r}(允许: {', '.join(ALLOWED_LANGS)})")
    backend = str(llm_backend or "").strip().lower()
    if backend and backend not in CONSOLE_FORM_BACKENDS:
        raise _bad_request(
            f"llm_backend 非法: {backend!r}(允许: {', '.join(CONSOLE_FORM_BACKENDS)};"
            "stub 不可由浏览器指定)"
        )

    # --- 落盘 + 构建 State(阶段 1;失败整体回滚任务目录) ---
    try:
        result = prepare_run(
            prompt=prompt_clean,
            uploads=videos,
            target_duration_ms=duration,
            ratio=ratio,
            lang=lang,
        )
    except ConsoleUploadError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    # --- 启动子进程(阶段 2;失败保留任务目录供排障,不伪成功) ---
    try:
        record = start_run(result["task_dir"], llm_backend=backend or None)
    except ConsoleLaunchError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    thread_id = result["thread_id"]
    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "thread_id": thread_id,
            "state": record.get("status", "starting"),
            **_response_urls(thread_id),
        },
    )


# ---------------------------------------------------------------------------
# 阶段 5:历史列表 / 详情 / 人工关卡恢复 / 取消
# ---------------------------------------------------------------------------

def _launch_error(e: ConsoleLaunchError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=str(e))


def _upload_error(e: ConsoleUploadError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=str(e))


@router.get("/runs")
def list_runs(request: Request, limit: int = 50) -> dict:
    """历史列表(阶段 5):扫描任务目录 + 状态调和;顺带触发超时收割。"""
    _verify_same_origin(request)
    console_service.reap_now()
    return {"ok": True, "runs": console_service.list_console_runs(limit=limit)}


@router.get("/runs/{thread_id}")
def run_detail(request: Request, thread_id: str) -> dict:
    """单条详情(record + 调和态);不存在 404。"""
    _verify_same_origin(request)
    console_service.reap_now()
    try:
        data = console_service.get_console_run(thread_id)
    except ConsoleLaunchError as e:
        raise _launch_error(e) from e
    except ConsoleUploadError as e:
        raise _upload_error(e) from e
    return {"ok": True, **data}


def _fetch_gate_data_sync(thread_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读 checkpoint:返回(挂起关卡展示载荷列表, state values)。

    实现收敛在 :mod:`web.gate_state`(阶段 3 起恢复门禁共用同一取数层);
    这里保留同名薄封装,让本模块内 monkeypatch 接缝不变。
    """
    from web.gate_state import fetch_gate_data

    return fetch_gate_data(thread_id)


def _fetch_gate_views_sync(thread_id: str) -> list[dict[str, Any]]:
    """只取展示载荷(gates 端点用;state values 留给 open-draft-dir 复用入口)。"""
    gates, _values = _fetch_gate_data_sync(thread_id)
    return gates


@router.get("/runs/{thread_id}/gates")
async def run_gates(request: Request, thread_id: str) -> dict:
    """挂起人工关卡的展示载荷(2026-11-06 阶段 2;只读)。

    读 LangGraph checkpoint(``aget_state``)提取真实 interrupt payload,
    经 :mod:`web.human_gate_payload` 归一为 UI 展示形态。**不**执行恢复。
    aget_state 在独立线程 + 独立 Selector 事件循环里跑(Windows uvicorn 主循环
    是 Proactor,async psycopg 不兼容 —— 与 resume_workflow 修复同款纪律)。
    """
    _verify_same_origin(request)
    console_service.reap_now()
    try:
        validate_thread_id(thread_id)
        gates = await run_in_threadpool(_fetch_gate_views_sync, thread_id)
    except ConsoleUploadError as e:
        raise _upload_error(e) from e
    except Exception as e:  # noqa: BLE001 — PG 不可达/图构建失败统一 503
        raise HTTPException(
            status_code=503, detail=f"读取挂起关卡失败(checkpoint 不可达?): {e}"
        ) from e
    return {
        "ok": True,
        "thread_id": thread_id,
        "gate_count": len(gates),
        "gates": gates,
        "common_note": COMMON_RESUME_NOTE,
    }


class OpenDraftDirBody(BaseModel):
    """POST /runs/{tid}/open-draft-dir 请求体:打开哪一份草稿目录。"""

    #: zh=中文主草稿;en=英文分支草稿(尚未创建时 409)
    draft: Literal["zh", "en"] = "zh"


def _allowed_open_roots() -> list[Path]:
    """允许"打开目录"的根集合(outputs 任务目录 + 剪映草稿库根),运行时读 config。"""
    roots: list[Path] = []
    for attr in ("CONSOLE_OUTPUTS_ROOT", "JIANYING_DRAFT_ROOT"):
        raw = getattr(config, attr, None)
        if raw:
            try:
                roots.append(Path(raw).resolve())
            except OSError:  # pragma: no cover — 非法路径直接忽略
                pass
    return roots


def _resolve_open_target_sync(thread_id: str, draft: str) -> Path:
    """从当前 Thread 的 checkpoint state 推导要打开的草稿目录(绝不信任前端传路径)。"""
    _gates, values = _fetch_gate_data_sync(thread_id)
    if draft == "en":
        raw = values.get("draft_dir_en_branch")
        if not raw:
            raise ConsoleLaunchError(
                "该任务尚未创建英文分支草稿(关卡③之前的流程位置)", status_code=409
            )
        target = Path(str(raw))
    else:
        raw = values.get("draft_path")
        if not raw:
            raise ConsoleLaunchError("该任务还没有主草稿(generate_draft 未成功?)", status_code=409)
        p = Path(str(raw))
        target = p.parent if p.suffix == ".json" else p
    resolved = target.resolve()
    if not resolved.is_dir():
        raise ConsoleLaunchError(f"草稿目录不存在: {resolved}", status_code=404)
    if not any(
        resolved.is_relative_to(root) for root in _allowed_open_roots() if root.exists()
    ):
        raise ConsoleLaunchError(f"草稿目录越界,拒绝打开: {resolved}", status_code=400)
    return resolved


@router.post("/runs/{thread_id}/open-draft-dir")
async def open_draft_dir(
    request: Request, thread_id: str, body: OpenDraftDirBody
) -> JSONResponse:
    """打开草稿目录(2026-11-06 阶段 2;服务端唤起系统文件管理器)。

    路径**只**来自当前 Thread 的 checkpoint state(计划 §8 阶段 2:
    "路径必须来自当前 Thread 的状态,不能拼接用户输入"),并做白名单根校验。
    """
    _verify_same_origin(request)
    _enforce_csrf(request)
    try:
        validate_thread_id(thread_id)
        target = await run_in_threadpool(_resolve_open_target_sync, thread_id, body.draft)
    except ConsoleLaunchError as e:
        raise _launch_error(e) from e
    except ConsoleUploadError as e:
        raise _upload_error(e) from e
    except Exception as e:  # noqa: BLE001 — checkpoint 不可达统一 503
        raise HTTPException(
            status_code=503, detail=f"读取草稿路径失败(checkpoint 不可达?): {e}"
        ) from e
    try:
        if os.name == "nt":
            os.startfile(str(target))  # noqa: S606 — 路径已过白名单校验
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])  # noqa: S603
        else:
            subprocess.Popen(["xdg-open", str(target)])  # noqa: S603
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"唤起文件管理器失败: {e}") from e
    return JSONResponse(
        status_code=200, content={"ok": True, "thread_id": thread_id, "opened": str(target)}
    )


@router.post("/runs/{thread_id}/resume")
def resume_run(request: Request, thread_id: str, body: ResumeBody) -> JSONResponse:
    """人工关卡恢复(阶段 5):复用 ``scripts/resume_workflow.py``,202 即返回。

    前置:事件库派生状态必须 waiting_human(否则 409);恢复语义与入口 A 一致
    (ADR-2026-11-04),事件仍由统一 runner 落库,既有 SSE 按游标继续补发。
    """
    _verify_same_origin(request)
    _enforce_csrf(request)
    try:
        console_service.resume_run(
            thread_id, checkpoint=body.checkpoint, value=body.value
        )
    except ConsoleLaunchError as e:
        raise _launch_error(e) from e
    except ConsoleUploadError as e:
        raise _upload_error(e) from e
    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "thread_id": thread_id,
            "state": "resuming",
            "events_url": f"/api/workflow/runs/{thread_id}/events",
        },
    )


@router.post("/runs/{thread_id}/cancel")
def cancel_run(request: Request, thread_id: str) -> JSONResponse:
    """取消运行(阶段 5):仅限本服务实例在册的活跃进程(防 PID 复用误杀)。"""
    _verify_same_origin(request)
    _enforce_csrf(request)
    try:
        record = console_service.cancel_run(thread_id)
    except ConsoleLaunchError as e:
        raise _launch_error(e) from e
    except ConsoleUploadError as e:
        raise _upload_error(e) from e
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "thread_id": thread_id,
            "state": record.get("status", "cancelled"),
        },
    )


@router.post("/runs/{thread_id}/delete")
def delete_run(request: Request, thread_id: str) -> JSONResponse:
    """删除历史任务(2026-11-07):任务目录(含成片)+ PG 事件尽力清理,不可恢复。

    仅终态/未启动记录可删(running/waiting_human/unknown 409 拒删,fail-closed);
    ``events_deleted`` 为 ``None`` 表示事件清理降级(如 PG 不可达),目录已删。
    """
    _verify_same_origin(request)
    _enforce_csrf(request)
    try:
        result = console_service.delete_run(thread_id)
    except ConsoleLaunchError as e:
        raise _launch_error(e) from e
    except ConsoleUploadError as e:
        raise _upload_error(e) from e
    return JSONResponse(status_code=200, content={"ok": True, **result})


# ---------------------------------------------------------------------------
# 2026-11-06 补充:清除可再生缓存(维护入口)
# ---------------------------------------------------------------------------

#: 上次成功清理时刻(monotonic 基准);None = 本进程尚未清理过,不限流
_last_cache_clear: Optional[float] = None


@router.post("/cache/clear")
def clear_cache(request: Request) -> JSONResponse:
    """清除可再生缓存(创建台「清除缓存」按钮;清单见 config.py 缓存段)。

    破坏性 IO,三道保护:
    - 同源 + CSRF 门禁(与 cancel 同款);
    - 冷却限流 ``CONSOLE_CACHE_CLEAR_COOLDOWN_SEC``(默认 30s,防误连点风暴;
      旧版 agent_fastapi 双桶限流的轻量替代);
    - 本实例有活跃运行子进程时 409(避免清掉运行中任务正在读写的缓存)。
    单路径删除失败不中断,失败明细随响应返回。
    """
    global _last_cache_clear
    _verify_same_origin(request)
    _enforce_csrf(request)

    cooldown = float(getattr(config, "CONSOLE_CACHE_CLEAR_COOLDOWN_SEC", 0.0) or 0.0)
    if cooldown > 0 and _last_cache_clear is not None:
        remaining = cooldown - (time.monotonic() - _last_cache_clear)
        if remaining > 0:
            raise HTTPException(
                status_code=429,
                detail=f"清除缓存过于频繁,请 {remaining:.0f} 秒后再试",
            )

    active = console_service.active_thread_ids()
    if active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"有 {len(active)} 个运行中的任务({', '.join(active)}),"
                "请先取消或等待完成后再清理缓存"
            ),
        )

    result = cache_cleaner.clean_cache()
    _last_cache_clear = time.monotonic()
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "cleaned_count": len(result["cleaned_paths"]),
            "cleaned_paths": result["cleaned_paths"],
            "errors": result["errors"],
        },
    )
