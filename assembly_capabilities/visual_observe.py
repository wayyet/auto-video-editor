"""``visual_observe.py``(源自 video-agent-kit 0.4.3 ``mcp/ve_tools/video_observe.py``)。

搬入的工具:
- ``video_ingest``:对整段视频按时间轴抽帧 + 生成 timestamped contact sheet,
  返回 ``ToolResult(image_paths=[...])``,供后续节点(``assembly_build_timeline``)
  直接读盘做视觉判断。

裁剪:
- 删除 ``video_watch_segment``(局部重看,video-edit-agent 专属,不在 assembly
  范围,plan §3.2 明确排除)。
- 删除 ``parse_watch_segments`` / ``filter_covered_segments`` /
  ``read_segment_ledger`` / ``update_segment_ledger`` / ``video_ledger_id`` /
  ``merge_watch_results`` 等 watch_segment 配套 helper。
- 删除 ``append_transcribe_nudge`` / ``resolve_transcript_args`` /
  ``guarded_transcript_text`` 中对 ``ctx.active_video_path`` 的依赖
  (本仓库精简版 RunContext 没有 active_video 字段)。
- 删除 ``cloud_asr_available`` 探测(MCP 服务专用,本仓库无云 ASR)。
- 删除 ``vali_video_metadata``(依赖 nvidia vali)。
- 保留 ``build_video_observation`` / ``sample_video_frames`` /
  ``build_contact_sheets`` / ``normalize_video_frame`` /
  ``reset_frames_dir`` / ``transcript_for_inline`` /
  ``validate_defaults`` / ``observation_signature`` /
  ``read_cached_observation`` / ``video_metadata`` / ``ffprobe_metadata``
  / ``parse_rate`` / ``ffprobe_duration`` /
  ``align_frame_count_to_t_patch`` / ``resolve_sample_count`` /
  ``prod_dynamic_fps_indices`` / ``uniform_indices`` /
  ``read_frame_with_ffmpeg`` / ``with_suffix_before_ext`` /
  ``default_full_prompt`` 等核心 helper。
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ffproc import run_proc
from .fonts import find_cjk_font
from .result import ToolResult
from .run_context import RunContext
from .transcript import read_transcript_text

MAX_INLINE_IMAGES = 600
INLINE_TRANSCRIPT_CHAR_LIMIT = 40000


@dataclass
class VideoSamplingDefaults:
    """``video_ingest`` 默认采样参数;本仓库管线固定用这套。"""
    video_frame_count: int | None = None
    video_fps: float = 2.0
    video_t_patch_size: int = 2
    video_sampling_mode: str = "prod"
    max_video_frames: int = 600
    video_frame_normalize_jpeg_quality: int = 90
    video_label_mode: str = "timestamp"
    sheet_cols: int = 4
    sheet_max_cells: int = 24
    sheet_width: int = 1568
    jpeg_quality: int = 85


@dataclass
class SampledFrame:
    label: str
    timestamp: float
    frame_index: int
    image: Any
    path: Path | None = None


# ---------------------------------------------------------------------------
# video_ingest 主入口
# ---------------------------------------------------------------------------
def video_ingest(args: dict, ctx: RunContext) -> ToolResult:
    """对整段视频抽帧并生成 timestamped contact sheet。

    标准流程(对应 plan §7.2):
    1. 校验 ``video_path`` 存在(否则 ``[ERROR] video not found``)。
    2. 若给了 ``transcript_path`` 则读其全文;否则 transcript_text 为空(仍可
       跑通整条链路,只是 contact sheet 内联无对应文字)。
    3. 调用 ``build_video_observation`` 抽帧 + 生成 contact sheet + 写
       ``video_ingest.json``。
    """
    if not args.get("video_path"):
        return ToolResult(text="[ERROR] video_path is required")
    video_path = ctx.resolve(args["video_path"])
    if not video_path.is_file():
        return ToolResult(text=f"[ERROR] video not found: {video_path}")

    transcript_path_arg = args.get("transcript_path")
    inline_text = args.get("transcript_text") or args.get("inline_text")
    transcript_path: Path | None = None
    transcript_text: str = ""
    if transcript_path_arg:
        transcript_path = ctx.resolve(transcript_path_arg)
        if not transcript_path.is_file():
            return ToolResult(text=f"[ERROR] transcript not found: {transcript_path}")
        transcript_text = read_transcript_text(transcript_path)
    elif inline_text:
        transcript_text = inline_text

    output_json = ctx.resolve(args.get("output_json") or "out/video_ingest.json")
    frames_dir = ctx.resolve(
        args.get("save_frames_dir")
        or f".video_agent/video_frames/{video_frames_dir_id(video_path)}_full"
    )
    defaults = VideoSamplingDefaults()
    prompt = args.get("prompt") or default_full_prompt()
    return build_video_observation(
        ctx=ctx,
        defaults=defaults,
        video_path=video_path,
        output_json=output_json,
        frames_dir=frames_dir,
        label="video_ingest",
        prompt=prompt,
        transcript_text=transcript_text,
        original_time_range=None,
    )


# ---------------------------------------------------------------------------
# build_video_observation(原 file: video_observe.py:480-579)
# ---------------------------------------------------------------------------
def build_video_observation(
    *,
    ctx: RunContext,
    defaults: VideoSamplingDefaults,
    video_path: Path,
    output_json: Path,
    frames_dir: Path,
    label: str,
    prompt: str,
    transcript_text: str,
    original_time_range: tuple[float, float] | None,
    extra_data: dict | None = None,
) -> ToolResult:
    validate_defaults(defaults)
    started = time.perf_counter()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    signature = observation_signature(video_path, defaults, prompt, transcript_text,
                                      original_time_range, extra_data, frames_dir)
    reused = False
    cached = read_cached_observation(output_json, signature)
    if cached is not None:
        sheets = [Path(path) for path in cached.get("sheet_paths", [])]
        frames = []
        media = cached.get("media", {})
        sampled_frame_count = int(media.get("sampled_frames") or len(cached.get("frame_paths", [])) or 0)
        reused = True
    else:
        reset_error = reset_frames_dir(frames_dir, ctx)
        if reset_error:
            return ToolResult(text=f"[ERROR] {label}: {reset_error}")

        try:
            frames, media = sample_video_frames(video_path, defaults, frames_dir / "frames", source_time_range=original_time_range)
            sheets = build_contact_sheets(frames, frames_dir / "sheets", defaults)
            sampled_frame_count = len(frames)
        except Exception as exc:
            return ToolResult(text=f"[ERROR] {label} frame sampling failed: {exc}")

        payload = {
            "tool": label,
            "video_path": str(video_path),
            "original_time_range": original_time_range,
            "used_time_range": media.get("source_time_range"),
            "prompt_for_main_agent": prompt,
            "transcript_text": transcript_text,
            "media": media,
            "frame_paths": [str(frame.path) for frame in frames if frame.path],
            "sheet_paths": [str(path) for path in sheets],
            "cache_signature": signature,
            "notes": [
                "No model API was called by this tool.",
                "An upstream model must inspect the returned timestamped images directly.",
                "Every visual observation must be evaluated together with the matching transcript text.",
            ],
            "elapsed_seconds": round(time.perf_counter() - started, 4),
            **(extra_data or {}),
        }
        output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    transcript_block = transcript_for_inline(transcript_text)
    sheet_lines = "\n".join(f"- {ctx.virtualize(path)}" for path in sheets)
    frame_note = (
        "Frame timestamps are source-video timestamps within original_time_range."
        if original_time_range else
        "Frame timestamps are source-video timestamps."
    )
    text = (
        f"{label} completed: sampled {sampled_frame_count} frame(s) and built {len(sheets)} timestamped "
        f"contact sheet(s). The sheets are inlined below for the main model to inspect; "
        "this tool did not call any model API.\n\n"
        f"Observation JSON: {ctx.virtualize(output_json)}\n"
        f"Sheets:\n{sheet_lines}\n\n"
        f"{frame_note}\n"
        "Read sheet cells left-to-right and top-to-bottom. Adjacent timestamp jumps mean the sampler "
        "did not include intermediate frames.\n\n"
        f"Task prompt for the main model:\n{prompt}\n\n"
        "Matching transcript for these images:\n"
        "<transcript>\n"
        f"{transcript_block}\n"
        "</transcript>"
    )
    if reused:
        text += "\n\n[note] Reused previously generated contact sheet(s) for the same video, time range, prompt, transcript, and sampling settings."
    return ToolResult(
        text=text,
        data={
            "tool": label,
            "output_json": str(output_json),
            "video_path": str(video_path),
            "used_time_range": media.get("source_time_range"),
            "image_count": len(sheets),
            "sampled_frames": sampled_frame_count,
            "media": media,
            "reused": reused,
            **(extra_data or {}),
        },
        artifacts=[str(output_json), str(frames_dir)],
        image_paths=[str(path) for path in sheets],
    )


def reset_frames_dir(frames_dir: Path, ctx: RunContext) -> str | None:
    """准备一个干净的 frames_dir。仅当目录在 work_dir 内才允许删除旧内容。"""
    resolved = frames_dir.resolve()
    work_dir = ctx.work_dir.resolve()
    if resolved != work_dir and _is_relative_to(resolved, work_dir):
        if resolved.exists():
            shutil.rmtree(resolved)
        resolved.mkdir(parents=True, exist_ok=True)
        return None
    if resolved.exists() and any(resolved.iterdir()):
        return (
            f"save_frames_dir {resolved} already exists, is not empty, and is outside the plugin work dir "
            f"({work_dir}); refusing to delete it. Pass an empty/new directory, or a directory under "
            f"{work_dir} to allow automatic cleanup."
        )
    resolved.mkdir(parents=True, exist_ok=True)
    return None


def _is_relative_to(child: Path, parent: Path) -> bool:
    """``Path.is_relative_to`` 是 Python 3.9+,为兼容旧版本提供等价实现。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def transcript_for_inline(transcript_text: str) -> str:
    if not transcript_text:
        return "(No transcript available.)"
    if len(transcript_text) <= INLINE_TRANSCRIPT_CHAR_LIMIT:
        return transcript_text
    return (
        transcript_text[:INLINE_TRANSCRIPT_CHAR_LIMIT]
        + f"\n\n[TRUNCATED in tool text at {INLINE_TRANSCRIPT_CHAR_LIMIT} chars; full transcript is saved in observation JSON.]"
    )


def validate_defaults(defaults: VideoSamplingDefaults) -> None:
    if not math.isfinite(defaults.video_fps) or defaults.video_fps <= 0:
        raise ValueError("video_fps must be a finite number > 0")
    if defaults.video_t_patch_size <= 0:
        raise ValueError("video_t_patch_size must be > 0")
    if defaults.max_video_frames > MAX_INLINE_IMAGES:
        raise ValueError(f"max_video_frames must be <= {MAX_INLINE_IMAGES}")
    if defaults.sheet_cols <= 0 or defaults.sheet_max_cells <= 0:
        raise ValueError("sheet layout settings must be > 0")
    if defaults.sheet_width <= 0:
        raise ValueError("sheet_width must be > 0")
    if not (1 <= defaults.jpeg_quality <= 100):
        raise ValueError("jpeg_quality must be in [1, 100]")


def observation_signature(
    video_path: Path,
    defaults: VideoSamplingDefaults,
    prompt: str,
    transcript_text: str,
    original_time_range: tuple[float, float] | None,
    extra_data: dict | None,
    frames_dir: Path,
) -> dict[str, Any]:
    stat = video_path.stat()
    transcript_hash = hashlib.sha256(transcript_text.encode("utf-8")).hexdigest()
    return {
        "video_path": str(video_path.resolve()),
        "video_size": stat.st_size,
        "video_mtime_ns": stat.st_mtime_ns,
        "sampling": defaults.__dict__,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "original_time_range": list(original_time_range) if original_time_range else None,
        "transcript_sha256": transcript_hash,
        "frames_dir": str(frames_dir.resolve()),
        "extra": extra_data or {},
    }


def read_cached_observation(output_json: Path, signature: dict[str, Any]) -> dict | None:
    if not output_json.is_file():
        return None
    try:
        payload = json.loads(output_json.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("cache_signature") != signature:
        return None
    sheets = [Path(path) for path in payload.get("sheet_paths", [])]
    if not sheets or not all(path.is_file() for path in sheets):
        return None
    return payload


# ---------------------------------------------------------------------------
# 抽帧
# ---------------------------------------------------------------------------
def video_metadata(video_path: Path) -> dict[str, Any]:
    """优先 opencv,失败回退 ffprobe。"""
    try:
        import cv2

        capture = cv2.VideoCapture(str(video_path))
        if capture.isOpened():
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            duration = frame_count / fps if fps > 0 and frame_count > 0 else None
            capture.release()
            if frame_count > 0 and fps > 0:
                return {
                    "frame_count": frame_count,
                    "fps": fps,
                    "width": width,
                    "height": height,
                    "duration": duration,
                    "reader": "opencv",
                }
        capture.release()
    except Exception:
        pass
    return ffprobe_metadata(video_path)


def parse_rate(rate: str | None) -> float:
    if not rate or rate == "0/0":
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        den_f = float(den)
        return float(num) / den_f if den_f else 0.0
    return float(rate)


def ffprobe_metadata(video_path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,nb_frames,r_frame_rate,duration:format=duration",
        "-of", "json", str(video_path),
    ]
    proc = run_proc(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    payload = json.loads(proc.stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise ValueError(f"Could not read video metadata: {video_path}")
    stream = streams[0]
    fps = parse_rate(stream.get("r_frame_rate"))
    duration = ffprobe_duration(stream.get("duration"))
    if duration is None:
        fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
        duration = ffprobe_duration(fmt.get("duration")) if isinstance(fmt, dict) else None
    frame_count_raw = stream.get("nb_frames")
    frame_count = int(frame_count_raw) if frame_count_raw and str(frame_count_raw).isdigit() else int((duration or 0.0) * fps)
    if frame_count <= 0 or fps <= 0 or duration is None:
        raise ValueError(f"Invalid video metadata from ffprobe: {video_path}")
    return {
        "frame_count": frame_count,
        "fps": fps,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": duration,
        "reader": "ffmpeg",
    }


def ffprobe_duration(value: object) -> float | None:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def align_frame_count_to_t_patch(num_frames: int, max_frame_count: int, t_patch: int) -> int:
    max_frame_count = int(max_frame_count)
    num_frames = min(max(int(num_frames), 1), max_frame_count)
    if t_patch > 1 and num_frames % t_patch != 0:
        max_aligned_frames = (max_frame_count // t_patch) * t_patch
        if max_aligned_frames <= 0:
            raise ValueError(f"max_video_frames={max_frame_count} must be at least video_t_patch_size={t_patch}")
        num_frames = ((num_frames + t_patch - 1) // t_patch) * t_patch
        num_frames = min(num_frames, max_aligned_frames)
    return num_frames


def resolve_sample_count(meta: dict[str, Any], defaults: VideoSamplingDefaults) -> int:
    if defaults.video_frame_count is not None:
        requested = defaults.video_frame_count
    else:
        duration = meta.get("duration") or (meta["frame_count"] / meta["fps"] if meta.get("fps") else 0)
        if defaults.video_sampling_mode == "prod":
            requested = int(float(duration) * defaults.video_fps)
        else:
            requested = math.ceil(float(duration) * defaults.video_fps)
    if defaults.video_sampling_mode == "prod":
        return align_frame_count_to_t_patch(requested, defaults.max_video_frames, defaults.video_t_patch_size)
    requested = max(1, requested)
    return min(requested, defaults.max_video_frames, meta["frame_count"])


def prod_dynamic_fps_indices(
    total_frames: int,
    fps: float,
    num_frames: int,
    target_fps: float,
    t_patch_size: int,
) -> tuple[list[int], list[float]]:
    import numpy as np

    if total_frames <= 0:
        raise ValueError("Video has no readable frames.")
    if fps <= 0:
        raise ValueError(f"Video FPS must be positive, got {fps}.")
    if target_fps <= 0:
        raise ValueError(f"video_fps must be positive, got {target_fps}.")
    if num_frames == -1:
        return [0], [0.0]

    duration_per_frame = 1 / fps
    timestamps = [i * duration_per_frame for i in range(total_frames)]
    duration = timestamps[-1]
    if total_frames < num_frames:
        frame_indices = [math.floor(i * total_frames / num_frames) for i in range(num_frames)]
    else:
        frame_indices = []
        current_second = 0
        threshold_idx = 1
        inv_fps = 1 / target_fps
        for frame_index in range(total_frames):
            if timestamps[frame_index] >= current_second:
                current_second = threshold_idx * inv_fps
                frame_indices.append(frame_index)
                threshold_idx += 1
                if current_second > duration - inv_fps:
                    break
    if len(frame_indices) < 3:
        frame_indices = np.linspace(0, total_frames - 1, num_frames, dtype=int).tolist()
    if len(frame_indices) < num_frames:
        frame_indices = np.linspace(frame_indices[0], frame_indices[-1], num_frames, dtype=int).tolist()
    elif len(frame_indices) > num_frames:
        frame_indices = np.linspace(0, total_frames - 1, num_frames, dtype=int).tolist()
    while len(frame_indices) % t_patch_size != 0:
        frame_indices.append(frame_indices[-1])
    return frame_indices, timestamps


def uniform_indices(total: int, count: int) -> list[int]:
    if total <= 0:
        raise ValueError("Video has no readable frames.")
    count = min(max(1, count), total)
    if count == 1:
        return [0]
    return sorted({round(i * (total - 1) / (count - 1)) for i in range(count)})


def sample_video_frames(
    video_path: Path,
    defaults: VideoSamplingDefaults,
    save_dir: Path | None = None,
    source_time_range: tuple[float, float] | None = None,
) -> tuple[list[SampledFrame], dict]:
    from PIL import Image

    meta = video_metadata(video_path)
    if source_time_range is None:
        range_start = 0.0
        range_end = float(meta.get("duration") or (meta["frame_count"] / meta["fps"]))
        used_time_range = None
        range_frame_offset = 0
        range_frame_count = meta["frame_count"]
        sample_meta = meta
    else:
        range_start, range_end = source_time_range
        if range_start < 0 or range_end < 0:
            raise ValueError("source_time_range start/end must be >= 0")
        if range_end <= range_start:
            raise ValueError("source_time_range end must be greater than start")
        duration = float(meta.get("duration") or (meta["frame_count"] / meta["fps"]))
        if range_start >= duration:
            raise ValueError(f"source_time_range start {range_start:.3f}s is beyond video duration {duration:.3f}s")
        if range_end > duration + 0.05:
            raise ValueError(f"source_time_range end {range_end:.3f}s exceeds video duration {duration:.3f}s")
        range_end = min(range_end, duration)
        used_time_range = (range_start, range_end)
        range_frame_offset = max(0, int(math.ceil(range_start * meta["fps"] - 1e-9)))
        range_last_frame = min(meta["frame_count"] - 1, int(math.floor(range_end * meta["fps"] + 1e-9)))
        if range_frame_offset > range_last_frame:
            midpoint_frame = int(round(((range_start + range_end) / 2) * meta["fps"]))
            midpoint_frame = min(meta["frame_count"] - 1, max(0, midpoint_frame))
            range_frame_offset = midpoint_frame
            range_last_frame = midpoint_frame
        range_frame_count = max(1, range_last_frame - range_frame_offset + 1)
        sample_meta = {**meta, "frame_count": range_frame_count, "duration": range_end - range_start}

    sample_count = resolve_sample_count(sample_meta, defaults)
    if defaults.video_sampling_mode == "prod":
        indices, _ = prod_dynamic_fps_indices(
            range_frame_count, meta["fps"], sample_count, defaults.video_fps, defaults.video_t_patch_size
        )
    else:
        indices = uniform_indices(range_frame_count, sample_count)

    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)

    capture = None
    try:
        import cv2

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            capture = None
    except Exception:
        capture = None

    frames: list[SampledFrame] = []
    for out_idx, local_frame_idx in enumerate(indices):
        frame_idx = range_frame_offset + local_frame_idx
        timestamp = frame_idx / meta["fps"]
        if capture is not None:
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = capture.read()
            if not ok:
                continue
            pos_ms = capture.get(cv2.CAP_PROP_POS_MSEC)
            if pos_ms and pos_ms > 0:
                timestamp = pos_ms / 1000.0
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb)
        else:
            image = read_frame_with_ffmpeg(video_path, timestamp)
        image = normalize_video_frame(image, defaults.video_frame_normalize_jpeg_quality)
        path = None
        if save_dir is not None:
            path = save_dir / f"{video_path.stem}_{out_idx:03d}_frame{frame_idx}.jpg"
            image.save(path, quality=90)
        frames.append(SampledFrame(label="", timestamp=float(timestamp), frame_index=int(frame_idx), image=image, path=path))
    if capture is not None:
        capture.release()
    if not frames:
        raise ValueError(f"No frames could be sampled from video: {video_path}")
    for seq, frame in enumerate(frames, start=1):
        if defaults.video_label_mode == "none":
            frame.label = ""
        elif defaults.video_label_mode == "frame":
            frame.label = f"Frame {seq:03d}/{len(frames):03d}"
        else:
            frame.label = f"Frame {seq:03d}/{len(frames):03d}; timestamp={frame.timestamp:.6f}s"
    formatted_indices = [
        f"{round(frame.timestamp, 1)} seconds"
        for frame in frames[::defaults.video_t_patch_size if defaults.video_sampling_mode == "prod" else 1]
    ]
    return frames, {
        **meta,
        "sample_fps": defaults.video_fps,
        "sampled_frames": len(frames),
        "sampling_mode": defaults.video_sampling_mode,
        "duration_for_sampling": sample_meta.get("duration"),
        "source_time_range": used_time_range,
        "t_patch_size": defaults.video_t_patch_size,
        "raw_frame_indices": [frame.frame_index for frame in frames],
        "raw_frame_timestamps_sec": [round(frame.timestamp, 6) for frame in frames],
        "prod_frame_indices": formatted_indices,
    }


def read_frame_with_ffmpeg(video_path: Path, timestamp: float):
    from PIL import Image

    cmd = [
        "ffmpeg", "-v", "error", "-ss", f"{timestamp:.6f}", "-i", str(video_path),
        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
    ]
    proc = run_proc(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not proc.stdout:
        raise ValueError(f"ffmpeg did not return a frame at {timestamp:.2f}s from {video_path}")
    with Image.open(io.BytesIO(proc.stdout)) as image:
        return image.convert("RGB")


def normalize_video_frame(image, quality: int):
    from PIL import Image

    if quality <= 0:
        return image
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    buffer.seek(0)
    with Image.open(buffer) as normalized:
        return normalized.convert("RGB")


def build_contact_sheets(frames: list[SampledFrame], out_dir: Path, defaults: VideoSamplingDefaults) -> list[Path]:
    from PIL import Image, ImageDraw, ImageFont

    out_dir.mkdir(parents=True, exist_ok=True)
    cols = defaults.sheet_cols
    max_cells = defaults.sheet_max_cells
    cell_w = max(1, defaults.sheet_width // cols)
    label_font_size = max(20, min(34, cell_w // 12))
    font = None
    font_paths: list[str] = []
    font_dirs = (
        os.environ.get("VE_FONT_DIRS")
        or os.environ.get("VIDEO_EDIT_FONT_DIRS")
        or ""
    )
    for raw_dir in [p for p in font_dirs.split(os.pathsep) if p.strip()]:
        font_dir = Path(raw_dir)
        if font_dir.is_dir():
            files = [
                p for p in sorted(font_dir.iterdir())
                if p.is_file() and p.suffix.lower() in {".ttf", ".ttc", ".otf"}
            ]
            bold = [p for p in files if "bold" in p.name.lower()]
            font_paths.extend(str(p) for p in [*bold, *files])
    font_paths.extend([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ])
    platform_font = find_cjk_font("bold")
    if platform_font:
        font_paths.append(platform_font)
    for font_path in font_paths:
        try:
            font = ImageFont.truetype(font_path, label_font_size)
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=label_font_size)
        except TypeError:
            font = ImageFont.load_default()
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    probe_bbox = probe.textbbox((0, 0), "00.00s", font=font)
    label_h = max(30, probe_bbox[3] - probe_bbox[1] + 14)
    paths: list[Path] = []

    for chunk_idx, chunk_start in enumerate(range(0, len(frames), max_cells)):
        chunk = frames[chunk_start:chunk_start + max_cells]
        cells = []
        for frame in chunk:
            image = frame.image.convert("RGB")
            w, h = image.size
            image_h = max(1, min(int(h * cell_w / max(1, w)), max(1, cell_w * 4 - label_h)))
            resized = image.resize((cell_w, image_h), Image.Resampling.LANCZOS)
            cell = Image.new("RGB", (cell_w, label_h + image_h), (0, 0, 0))
            draw = ImageDraw.Draw(cell)
            label = f"{frame.timestamp:.2f}s"
            bbox = draw.textbbox((0, 0), label, font=font)
            text_h = bbox[3] - bbox[1]
            text_y = max(0, (label_h - text_h) // 2 - bbox[1])
            draw.rectangle((0, 0, cell_w, label_h), fill=(12, 15, 20))
            draw.text((12, text_y), label, fill=(255, 255, 255), font=font)
            draw.line((0, label_h - 1, cell_w, label_h - 1), fill=(56, 64, 75))
            cell.paste(resized, (0, label_h))
            cells.append(cell)

        cell_h = max(cell.height for cell in cells)
        padded = []
        for cell in cells:
            if cell.height == cell_h:
                padded.append(cell)
                continue
            canvas = Image.new("RGB", (cell_w, cell_h), (0, 0, 0))
            canvas.paste(cell, (0, 0))
            padded.append(canvas)
        while len(padded) % cols:
            padded.append(Image.new("RGB", (cell_w, cell_h), (0, 0, 0)))

        rows = []
        for row_start in range(0, len(padded), cols):
            row = Image.new("RGB", (cell_w * cols, cell_h), (0, 0, 0))
            for i, cell in enumerate(padded[row_start:row_start + cols]):
                row.paste(cell, (i * cell_w, 0))
            rows.append(row)
        sheet = Image.new("RGB", (cell_w * cols, cell_h * len(rows)), (0, 0, 0))
        for i, row in enumerate(rows):
            sheet.paste(row, (0, i * cell_h))

        start_ms = int(round(chunk[0].timestamp * 1000))
        end_ms = int(round(chunk[-1].timestamp * 1000))
        path = out_dir / f"video_grid_{start_ms}ms_{end_ms}ms_p{chunk_idx:02d}.jpg"
        sheet.save(path, quality=defaults.jpeg_quality)
        paths.append(path)
    return paths


def video_frames_dir_id(video_path: Path) -> str:
    digest = hashlib.sha256(str(video_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{video_path.stem}_{digest}"


def with_suffix_before_ext(path: Path, suffix: str) -> Path:
    return path.with_name(f"{path.stem}_{suffix}{path.suffix or '.json'}")


def default_full_prompt() -> str:
    return (
        "Inspect the complete video directly from the timestamped sheets. Identify people, scenes, "
        "actions, narrative/content structure, visual quality, candidate usable ranges, and ranges "
        "that need local high-FPS rewatch before editing."
    )
