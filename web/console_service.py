"""创建台运行编排(第二期阶段 1:只准备,不启动)。

阶段 1 边界(计划 §11 阶段 1 验收"不启动工作流,不创建 Checkpoint"):
- :func:`prepare_run` = 上传落盘 → 构建 initial state → 写 ``console_run.json``;
- 本模块**不 import** graph / LangGraph,不连接 PostgreSQL —— 那是阶段 2
  ``start_run()``(子进程启动 + LLM 后端门禁)的事;
- 任何一步失败:删除本次新建的任务目录,不留"可运行"的半成品(计划 §6.2 规则 7、
  测试矩阵"上传失败时不生成可运行任务")。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import config  # 运行时读 config.CONSOLE_*(可 monkeypatch),不在 import 期固化值
from web.console_uploads import ConsoleUploadError, save_uploaded_videos, validate_thread_id
from web.console_state import build_initial_state, new_thread_id, write_initial_state_json


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
    "ConsoleUploadError",
    "console_run_json_path",
    "prepare_run",
]
