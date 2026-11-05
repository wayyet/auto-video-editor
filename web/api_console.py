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
"""

from __future__ import annotations

import secrets
import time
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

import config  # 运行时读 config.CONSOLE_*(可 monkeypatch),不在 import 期固化值
from web.api_workflow import _verify_same_origin
from web.console_service import ConsoleLaunchError, prepare_run, start_run
from web.console_uploads import ALLOWED_VIDEO_EXTS, ConsoleUploadError

router = APIRouter(prefix="/api/console")

#: 契约冻结的可选值(阶段 0 文档 §2.1)
ALLOWED_RATIOS: tuple[str, ...] = ("9:16", "16:9", "4:3")
ALLOWED_LANGS: tuple[str, ...] = ("zh", "en")

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
) -> JSONResponse:
    """创建并启动一次工作流任务(第二期阶段 2 语义)。

    成功返回 202 与 thread_id,`state="starting"`(子进程已创建,执行不等待);
    同步 def 由 FastAPI 放线程池跑,落盘/Popen/PG 预检不阻塞事件循环。
    LLM 后端等运行环境只由服务端配置决定,不接受浏览器字段。
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
        record = start_run(result["task_dir"])
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
