"""创建台写 API(第二期阶段 1:上传 + State 构建;阶段 2 起含启动)。

职责边界:
- 本模块是仓库里**唯一**允许"创建工作流运行"的 Web 入口;
- ``web/api_workflow.py`` 的 ``/api/workflow/*`` 保持 GET-only 只读,
  两者的边界由阶段 0 契约文档冻结,不得互相混入对方职责;
- 阶段 1 的 ``POST /runs`` 只做"校验 → 落盘 → initial state",**绝不启动
  工作流、绝不触碰 PostgreSQL**(阶段 2 起由 console_service 接管启动)。

错误码契约(阶段 0 文档 §2.3):400 参数 / 403 Origin / 413 超限 /
415 格式或内容 / 409 并发(阶段 2) / 503 PG 或模型未就绪(阶段 2) / 500。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

import config  # 运行时读 config.CONSOLE_*(可 monkeypatch),不在 import 期固化值
from web.api_workflow import _verify_same_origin
from web.console_service import prepare_run
from web.console_uploads import ConsoleUploadError

router = APIRouter(prefix="/api/console")

#: 契约冻结的可选值(阶段 0 文档 §2.1)
ALLOWED_RATIOS: tuple[str, ...] = ("9:16", "16:9", "4:3")
ALLOWED_LANGS: tuple[str, ...] = ("zh", "en")


def _response_urls(thread_id: str) -> dict[str, str]:
    """契约冻结的三个 URL(相对路径;复用既有只读监控 API,SSE 自动可订阅)。"""
    return {
        "monitor_url": f"/?thread_id={thread_id}",
        "snapshot_url": f"/api/workflow/runs/{thread_id}",
        "events_url": f"/api/workflow/runs/{thread_id}/events",
    }


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


@router.post("/runs")
async def create_run(
    request: Request,
    prompt: str = Form(""),
    videos: list[UploadFile] = File(default=[]),
    target_duration_ms: Optional[int] = Form(None),
    ratio: str = Form("9:16"),
    lang: str = Form("zh"),
) -> JSONResponse:
    """创建一次工作流任务(阶段 1:仅准备;不启动、不建 checkpoint)。

    成功返回 202 与 thread_id;`state` 恒为 "prepared"(阶段 2 起才会出现
    "starting")。LLM 后端等运行环境只由服务端配置决定,不接受浏览器字段。
    """
    # 第二道防线:跨源浏览器请求直接拒绝(curl/同源不带 Origin 时放行)
    _verify_same_origin(request)

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

    # --- 落盘 + 构建 State(不含任何启动语义) ---
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

    thread_id = result["thread_id"]
    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "thread_id": thread_id,
            "state": "prepared",
            **_response_urls(thread_id),
        },
    )
