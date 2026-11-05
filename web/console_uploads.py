"""创建台上传处理:流式保存 + 安全校验 + ffprobe 探测(第二期阶段 1)。

职责边界(计划 §6 文件存储设计 / §10.2 文件安全):
- 只负责"把浏览器上传的视频安全落盘到 ``<outputs_root>/<thread_id>/inputs/``";
- 不构建 initial state(``web/console_state.py``),不启动工作流
  (``web/console_service.py``);
- 不依赖 PostgreSQL —— 阶段 1 的 API 在无数据库环境完全可用。

安全纪律:
- thread_id 先过 ``^console-[0-9a-f]{12}$`` 再拼路径(服务端生成,防御性复验);
- 磁盘文件名全部服务端生成(``0001.mp4`` …),用户原始文件名不落盘;
- 扩展名白名单 {.mp4, .mov};数量 / 单文件大小在**流式写入过程中**强制;
- 临时文件 + ``os.replace`` 原子落盘;失败只清理本次任务的文件;
- ``Path.resolve()`` 归属校验,防路径穿越;
- ffprobe 校验真实视频流(带超时;``assembly_capabilities.visual_observe.ffprobe_metadata``
  无超时参数,故此处按同命令形自实现)。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Callable

import config  # 运行时读 config.CONSOLE_*(可 monkeypatch),不在 import 期固化值

#: 服务端生成的 thread_id 形状(三处既有校验中最严规则的子集,且自带前缀防撞)
CONSOLE_THREAD_ID_RE = re.compile(r"^console-[0-9a-f]{12}$")

#: 扩展名白名单(计划 §6.3:首期只验证 MP4、MOV)
ALLOWED_VIDEO_EXTS: frozenset[str] = frozenset({".mp4", ".mov"})

#: 流式写入分块大小(1 MiB;计划 §6.2"使用分块方式保存大文件")
UPLOAD_CHUNK_SIZE: int = 1024 * 1024


class ConsoleUploadError(Exception):
    """上传/校验失败;``status_code`` 直接映射 API 错误码(413/415/400)。"""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def validate_thread_id(thread_id: str) -> str:
    """复验服务端生成的 thread_id 形状;非法即拒(防路径穿越第一道闸)。"""
    value = str(thread_id or "").strip()
    if not CONSOLE_THREAD_ID_RE.match(value):
        raise ConsoleUploadError(f"thread_id 形状非法: {value!r}", status_code=400)
    return value


def _ensure_within(root: Path, candidate: Path) -> Path:
    """目录归属校验:candidate 必须 resolve 后仍位于 root 内(计划 §6.2 规则 4/8)。"""
    root_resolved = Path(root).resolve()
    candidate_resolved = Path(candidate).resolve()
    if candidate_resolved != root_resolved and not candidate_resolved.is_relative_to(
        root_resolved
    ):
        raise ConsoleUploadError(
            f"路径越界: {candidate_resolved} 不在 {root_resolved} 内", status_code=400
        )
    return candidate_resolved


def probe_video_file(path: Path, *, timeout: float | None = None) -> dict[str, Any]:
    """ffprobe 校验真实视频流(计划 §6.2 规则 6)。

    命令形对齐 ``assembly_capabilities.visual_observe.ffprobe_metadata``
    (select_streams v:0),但补上 subprocess 超时(计划 §10.4"ffprobe 超时")。
    任何失败(超时/无视频流/元数据无效)都抛 :class:`ConsoleUploadError`(415)。
    """
    timeout_s = float(
        timeout if timeout is not None else config.CONSOLE_FFPROBE_TIMEOUT_SEC
    )
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate:format=duration",
        "-of", "json", str(path),
    ]
    try:
        proc = subprocess.run(  # noqa: S603 — 固定参数数组,无 shell
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        raise ConsoleUploadError(
            f"ffprobe 探测超时(>{timeout_s:.0f}s): {path.name}", status_code=415
        ) from e
    except OSError as e:  # ffprobe 不在 PATH 等
        raise ConsoleUploadError(
            f"ffprobe 不可用,无法校验视频: {e}", status_code=415
        ) from e

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise ConsoleUploadError(
            "ffprobe 校验失败: " + (detail[-1] if detail else f"exit={proc.returncode}"),
            status_code=415,
        )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        raise ConsoleUploadError("ffprobe 输出不是合法 JSON", status_code=415) from e

    streams = payload.get("streams") or []
    if not streams:
        raise ConsoleUploadError("文件中没有视频流(select_streams v:0 为空)", status_code=415)
    stream = streams[0] if isinstance(streams[0], dict) else {}
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "r_frame_rate": str(stream.get("r_frame_rate") or ""),
        "duration": float((payload.get("format") or {}).get("duration") or 0.0),
    }


def _original_filename(upload: Any) -> str:
    """取上传文件的原始文件名(仅用于扩展名判断,不落盘)。"""
    name = getattr(upload, "filename", None) or ""
    return Path(str(name)).name


def _ext_of(filename: str) -> str:
    return Path(filename).suffix.lower()


def save_uploaded_videos(
    thread_id: str,
    uploads: list[Any],
    *,
    outputs_root: Path | None = None,
    max_videos: int | None = None,
    max_bytes: int | None = None,
    probe: Callable[[Path], dict[str, Any]] | None = None,
    chunk_size: int = UPLOAD_CHUNK_SIZE,
) -> list[dict[str, Any]]:
    """把上传文件流式保存到 ``<outputs_root>/<thread_id>/inputs/``。

    Args:
        thread_id: 服务端生成的任务 ID(先过 :func:`validate_thread_id`)。
        uploads: ``starlette.datastructures.UploadFile`` 列表(有 ``file``/``filename``)。
        outputs_root: 任务根目录;缺省读 ``config.CONSOLE_OUTPUTS_ROOT``(运行时读,
            可 monkeypatch)。
        max_videos / max_bytes: 上限;缺省读 config 同名常量。
        probe: 视频探测函数(默认 :func:`probe_video_file`;单测注入假探测)。
        chunk_size: 流式分块大小。

    Returns:
        已保存文件元数据列表::

            [{"path": "<abs>/inputs/0001.mp4", "original_name": "a.mp4",
              "bytes": 12345, "probe": {...}}, …]

    Raises:
        ConsoleUploadError: 数量/扩展名/大小/探测任一失败;失败时已写入的
            本次文件与临时文件全部清理(计划 §6.2 规则 7)。
    """
    tid = validate_thread_id(thread_id)
    limit_videos = int(
        max_videos if max_videos is not None else config.CONSOLE_MAX_VIDEOS
    )
    limit_bytes = int(
        max_bytes if max_bytes is not None else config.CONSOLE_MAX_VIDEO_BYTES
    )
    root = (
        Path(outputs_root) if outputs_root is not None else Path(config.CONSOLE_OUTPUTS_ROOT)
    )

    # --- 数量与扩展名先验(不写任何字节就拒掉的错误) ---
    if not uploads:
        raise ConsoleUploadError("至少需要一个视频文件", status_code=400)
    if len(uploads) > limit_videos:
        raise ConsoleUploadError(
            f"视频数量超限: {len(uploads)} > {limit_videos}", status_code=400
        )
    for upload in uploads:
        ext = _ext_of(_original_filename(upload))
        if ext not in ALLOWED_VIDEO_EXTS:
            raise ConsoleUploadError(
                f"不支持的文件类型: {_original_filename(upload)!r}(允许: "
                f"{', '.join(sorted(ALLOWED_VIDEO_EXTS))})",
                status_code=415,
            )

    task_dir = _ensure_within(root, Path(root) / tid)
    inputs_dir = task_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict[str, Any]] = []
    tmp_files: list[Path] = []
    written_files: list[Path] = []  # 已 replace 落盘但尚未通过探测的文件(清理注册表)
    try:
        for index, upload in enumerate(uploads, start=1):
            ext = _ext_of(_original_filename(upload))
            final_path = inputs_dir / f"{index:04d}{ext}"
            _ensure_within(inputs_dir, final_path)  # 归属复验(index 由我们生成,防御性)
            tmp_path = inputs_dir / f".tmp-{uuid.uuid4().hex}{ext}"
            tmp_files.append(tmp_path)
            written = 0
            fileobj: BinaryIO = upload.file
            with open(tmp_path, "wb") as out:
                while True:
                    chunk = fileobj.read(chunk_size)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit_bytes:
                        raise ConsoleUploadError(
                            f"文件过大(>{limit_bytes} 字节): "
                            f"{_original_filename(upload)!r}",
                            status_code=413,
                        )
                    out.write(chunk)
            if written == 0:
                raise ConsoleUploadError(
                    f"空文件: {_original_filename(upload)!r}", status_code=415
                )
            # 原子落盘;落盘即注册进清理清单(探测失败也要删掉,不留半成品)
            os.replace(tmp_path, final_path)
            tmp_files.pop()  # 已被 replace,不再需要清理
            written_files.append(final_path)
            probe_fn = probe if probe is not None else probe_video_file
            probe_result = probe_fn(final_path)
            saved.append(
                {
                    "path": str(final_path.resolve()),
                    "original_name": _original_filename(upload),
                    "bytes": written,
                    "probe": probe_result,
                }
            )
        return saved
    except BaseException:
        # 失败清理:本次已落盘文件 + 残留临时文件(计划 §6.2 规则 7)
        for path in written_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        for tmp in tmp_files:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        raise
