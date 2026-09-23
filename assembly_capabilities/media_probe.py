"""``media_probe.py``(源自 video-agent-kit 0.4.3 ``mcp/ve_tools/media.py``)。

搬入的工具:
- ``inspect_media``:用 ffprobe 取媒体元数据,生成 ``media.json``。
- ``analyze_media``:在 ``inspect_media`` 基础上做场景切换/黑帧/静音扫描,
  生成 ``media_analysis.json`` 给出"按场景边界切段"的候选段列表。

裁剪:
- 删除 ``transcribe`` / ``speech_transcribe`` / ``remote_speech_transcribe`` /
  ``local_speech_transcribe`` / ``cloud_asr_*`` / ``zcode_speech_*`` 等云端
  ASR 链路(详见 ``speech_asr.py``,本仓库只放本地透传 + 清晰错误)。
- 删除 ``prepare_asr_audio`` / ``sanitize_transcript_payload`` 等 ASR-only helper。
- 保留 ``safe_float`` / ``safe_int`` / ``coerce_threshold`` / ``run_json`` 等
  本地 ffprobe + ffmpeg scan 用的纯 helper。
- 保留 ``scan_scene_changes`` / ``scan_ranges`` / ``segments_from_boundaries`` /
  ``media_summary`` / ``media_technical_risks`` / ``first_stream`` /
  ``stream_fps`` / ``stream_rotation``。
"""
from __future__ import annotations

import json
import math
import shutil
import time
from pathlib import Path
from typing import Any

from .ffproc import run_proc
from .result import ToolResult
from .run_context import RunContext


ASR_AUDIO_BITRATE = "64k"
ASR_SAMPLE_RATE = "16000"


def run_json(cmd: list[str], timeout: int = 60) -> dict:
    proc = run_proc(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"command failed: {cmd}")
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# inspect_media
# ---------------------------------------------------------------------------
def inspect_media(args: dict, ctx: RunContext) -> ToolResult:
    if not args.get("input_path"):
        return ToolResult(text="[ERROR] input_path is required")
    input_path = ctx.resolve(args["input_path"])
    if not input_path.is_file():
        return ToolResult(text=f"[ERROR] File not found: {input_path}")
    if not shutil.which("ffprobe"):
        return ToolResult(text="[ERROR] ffprobe not found on PATH")

    started = time.time()
    try:
        data = run_json([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(input_path)
        ])
    except Exception as exc:
        return ToolResult(text=f"[ERROR] ffprobe failed: {exc}")
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    duration_seconds = safe_float(fmt.get("duration"))
    if duration_seconds is None:
        stream_durations = [safe_float(s.get("duration")) for s in streams if isinstance(s, dict)]
        stream_durations = [value for value in stream_durations if value is not None]
        duration_seconds = max(stream_durations) if stream_durations else None
    out = {
        "input_path": str(input_path),
        "duration_seconds": duration_seconds,
        "size_bytes": safe_int(fmt.get("size")) or 0,
        "format_name": fmt.get("format_name"),
        "bit_rate": safe_int(fmt.get("bit_rate")),
        "streams": streams,
        "video_streams": [s for s in streams if s.get("codec_type") == "video"],
        "audio_streams": [s for s in streams if s.get("codec_type") == "audio"],
        "elapsed_seconds": round(time.time() - started, 3),
    }
    out["summary"] = media_summary(out)
    out["technical_risks"] = media_technical_risks(out)
    output_json = args.get("output_json")
    artifacts = []
    if output_json:
        p = ctx.resolve(output_json)
    else:
        p = ctx.output_dir / "media.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append(str(p))
    return ToolResult(
        text=f"Inspected media: {ctx.virtualize(input_path)}",
        data=out,
        artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# analyze_media
# ---------------------------------------------------------------------------
def analyze_media(args: dict, ctx: RunContext) -> ToolResult:
    """轻量级确定性源素材分析(给选段用),不做语义判断。"""
    if not args.get("input_path"):
        return ToolResult(text="[ERROR] input_path is required")
    input_path = ctx.resolve(args["input_path"])
    if not input_path.is_file():
        return ToolResult(text=f"[ERROR] File not found: {input_path}")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return ToolResult(text="[ERROR] ffmpeg/ffprobe not found on PATH")
    scene_threshold = coerce_threshold(args.get("scene_threshold"), default=0.30)
    if isinstance(scene_threshold, ToolResult):
        return scene_threshold
    started = time.time()
    probe_sidecar = ctx.work_dir / "analysis" / f"{input_path.stem}_media.json"
    probe = inspect_media({"input_path": str(input_path), "output_json": str(probe_sidecar)}, ctx)
    if probe.text.startswith("[ERROR]"):
        return ToolResult(text=f"[ERROR] inspect failed during analyze_media: {probe.text}", data=probe.data)
    duration = safe_float((probe.data.get("summary") or {}).get("duration_seconds"))
    scenes, scene_log = scan_scene_changes(input_path, scene_threshold)
    black_ranges, black_log = scan_ranges(input_path, "blackdetect=d=0.2:pix_th=0.10", "black")
    silence_ranges, silence_log = scan_ranges(input_path, "silencedetect=n=-35dB:d=0.5", "silence")
    candidate_segments = segments_from_boundaries(duration, scenes)
    report = {
        "input_path": str(input_path),
        "duration_seconds": duration,
        "scene_threshold": scene_threshold,
        "scene_change_times": scenes,
        "candidate_segments": candidate_segments,
        "black_ranges": black_ranges,
        "silence_ranges": silence_ranges,
        "notes": [
            "candidate_segments are deterministic scene-boundary hints, not final editorial choices",
            "use video_ingest to visually verify any candidate before timeline decisions",
        ],
        "logs": {
            "scene_tail": scene_log[-2000:],
            "black_tail": black_log[-2000:],
            "silence_tail": silence_log[-2000:],
        },
        "elapsed_seconds": round(time.time() - started, 3),
    }
    output_json = ctx.resolve(args.get("output_json") or "out/media_analysis.json")
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "input_path": report["input_path"],
        "duration_seconds": duration,
        "scene_threshold": scene_threshold,
        "scene_change_count": len(scenes),
        "candidate_segment_count": len(candidate_segments),
        "black_range_count": len(black_ranges),
        "silence_range_count": len(silence_ranges),
        "notes": report["notes"],
        "elapsed_seconds": report["elapsed_seconds"],
    }
    return ToolResult(
        text=(
            f"Media analysis written: {ctx.virtualize(output_json)} "
            f"({len(scenes)} scene changes, {len(candidate_segments)} candidate segments). "
            f"Read {ctx.virtualize(output_json)} for the full boundary list before timeline decisions."
        ),
        data=summary,
        artifacts=[str(output_json)],
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    if not math.isfinite(parsed):
        return None
    return round(parsed, 6)


def safe_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def coerce_threshold(value: object, *, default: float) -> float | ToolResult:
    if value is None:
        return default
    if isinstance(value, bool):
        return ToolResult(text="[ERROR] scene_threshold must be numeric")
    try:
        parsed = float(value)
    except Exception:
        return ToolResult(text="[ERROR] scene_threshold must be numeric")
    if not math.isfinite(parsed) or parsed <= 0 or parsed >= 1:
        return ToolResult(text="[ERROR] scene_threshold must be in (0, 1)")
    return parsed


def scan_scene_changes(path: Path, threshold: float) -> tuple[list[float], str]:
    expr = f"select='gt(scene,{threshold:.6f})',showinfo"
    cmd = ["ffmpeg", "-v", "info", "-i", str(path), "-vf", expr, "-f", "null", "-"]
    try:
        proc = run_proc(cmd, capture_output=True, text=True, timeout=900)
    except Exception as exc:
        return [], str(exc)
    output = (proc.stderr or "") + (proc.stdout or "")
    times = []
    for line in output.splitlines():
        marker = "pts_time:"
        if marker not in line:
            continue
        tail = line.split(marker, 1)[1].split()[0]
        value = safe_float(tail)
        if value is not None:
            times.append(value)
    return sorted(set(round(t, 3) for t in times if t >= 0)), output


def scan_ranges(path: Path, filter_expr: str, kind: str) -> tuple[list[dict], str]:
    filter_arg = "-af" if kind == "silence" else "-vf"
    cmd = ["ffmpeg", "-v", "info", "-i", str(path), filter_arg, filter_expr, "-f", "null", "-"]
    try:
        proc = run_proc(cmd, capture_output=True, text=True, timeout=900)
    except Exception as exc:
        return [], str(exc)
    output = (proc.stderr or "") + (proc.stdout or "")
    starts: list[float] = []
    ranges = []
    start_marker = f"{kind}_start:"
    end_marker = f"{kind}_end:"
    duration_marker = f"{kind}_duration:"
    for line in output.splitlines():
        if start_marker in line:
            value = safe_float(line.split(start_marker, 1)[1].split()[0])
            if value is not None:
                starts.append(value)
        if end_marker in line:
            end = safe_float(line.split(end_marker, 1)[1].split()[0])
            duration = safe_float(line.split(duration_marker, 1)[1].split()[0]) if duration_marker in line else None
            start = starts.pop(0) if starts else ((end - duration) if end is not None and duration is not None else None)
            if start is not None and end is not None:
                ranges.append({"start": round(start, 3), "end": round(end, 3), "duration": round(end - start, 3)})
    return ranges, output


def segments_from_boundaries(duration: float | None, boundaries: list[float]) -> list[dict]:
    if duration is None or duration <= 0:
        return []
    points = [0.0] + [t for t in boundaries if 0.1 < t < duration - 0.1] + [duration]
    points = sorted(set(round(t, 3) for t in points))
    segments = []
    for idx, (start, end) in enumerate(zip(points, points[1:])):
        if end - start < 0.3:
            continue
        segments.append({
            "index": idx,
            "start": start,
            "end": end,
            "duration": round(end - start, 3),
            "source": "scene_boundary",
        })
    return segments


def media_summary(probe: dict) -> dict:
    video = first_stream(probe.get("video_streams"))
    audio = first_stream(probe.get("audio_streams"))
    return {
        "duration_seconds": probe.get("duration_seconds"),
        "has_video": bool(video),
        "has_audio": bool(audio),
        "width": safe_int(video.get("width")) if video else None,
        "height": safe_int(video.get("height")) if video else None,
        "fps": stream_fps(video) if video else None,
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "audio_channels": safe_int(audio.get("channels")) if audio else None,
        "audio_sample_rate": safe_int(audio.get("sample_rate")) if audio else None,
        "pix_fmt": video.get("pix_fmt") if video else None,
        "color_space": video.get("color_space") if video else None,
        "color_transfer": video.get("color_transfer") if video else None,
        "rotation": stream_rotation(video) if video else None,
    }


def media_technical_risks(probe: dict) -> list[dict]:
    risks = []
    summary = probe.get("summary") or {}
    if not summary.get("has_video") and not summary.get("has_audio"):
        risks.append({"severity": "error", "message": "no video or audio stream detected"})
    elif not summary.get("has_video"):
        risks.append({"severity": "warning", "message": "media has no video stream"})
    if summary.get("has_video") and not summary.get("has_audio"):
        risks.append({"severity": "warning", "message": "video has no audio stream"})
    width = summary.get("width") or 0
    height = summary.get("height") or 0
    if width and height and (width % 2 or height % 2):
        risks.append({"severity": "warning", "message": "odd video dimensions may require padding before H.264 render"})
    if width and height and width * height >= 3840 * 2160:
        risks.append({"severity": "info", "message": "4K-or-larger source may be expensive to sample/render"})
    video = first_stream(probe.get("video_streams"))
    if video:
        avg = stream_fps(video, "avg_frame_rate")
        nominal = stream_fps(video, "r_frame_rate")
        if avg and nominal and abs(avg - nominal) > 0.5:
            risks.append({
                "severity": "warning",
                "message": "possible variable-frame-rate source",
                "evidence": f"avg_frame_rate={avg:.3f}, r_frame_rate={nominal:.3f}",
            })
        if stream_rotation(video):
            risks.append({"severity": "info", "message": f"rotation metadata present: {stream_rotation(video)}"})
        transfer = str(video.get("color_transfer") or "").lower()
        if transfer in {"smpte2084", "arib-std-b67"}:
            risks.append({"severity": "warning", "message": f"HDR transfer detected ({transfer}); SDR preview may need tonemapping"})
    return risks


def first_stream(streams: object) -> dict:
    if isinstance(streams, list) and streams and isinstance(streams[0], dict):
        return streams[0]
    return {}


def stream_fps(stream: dict, key: str | None = None) -> float | None:
    keys = [key] if key else ["avg_frame_rate", "r_frame_rate"]
    for item in keys:
        value = stream.get(item)
        if not value or value == "0/0":
            continue
        try:
            if "/" in str(value):
                num, den = str(value).split("/", 1)
                den_f = float(den)
                if den_f == 0:
                    continue
                fps = float(num) / den_f
            else:
                fps = float(value)
        except Exception:
            continue
        if math.isfinite(fps) and fps > 0:
            return round(fps, 6)
    return None


def stream_rotation(stream: dict) -> str | None:
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    if tags.get("rotate"):
        return str(tags["rotate"])
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, dict) and item.get("rotation") not in (None, 0, "0"):
                return str(item["rotation"])
    return None


def media_duration_seconds(path: Path) -> float | None:
    """对单文件 ffprobe 一次,只取 duration(给 ``speech_asr`` 等其它 helper 用)。"""
    try:
        data = run_json([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", str(path),
        ])
    except Exception:
        return None
    fmt = data.get("format", {}) if isinstance(data.get("format"), dict) else {}
    return safe_float(fmt.get("duration"))
