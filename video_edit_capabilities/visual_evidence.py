"""视觉证据工具 —— ``video_watch_segment`` + ``video_read_frames``。

设计依据(``docs/integration/video-agent-kit九个MCP工具迁移至auto-video-editor设计执行计划.md``
§3.2 / §5 / §6.1 / 决策③):

- 原源端 ``mcp/ve_tools/video_observe.py``(1011 行)与 ``frame_zoom.py``(263 行)
  复用具名调用约定 ``(args: dict, ctx: RunContext) -> ToolResult``,但:
  - 大量记忆字段(``ctx.active_video_path`` / ``active_video_changed`` /
    ``remember_video`` / ``video_was_visually_ingested`` 等)在精简版
    ``RunContext`` 中**不存在**,因此 video_watch_segment 的 "implicit-active"
    分支被完全移除 —— 调用方必须传 ``video_path``,否则 ``[ERROR] video_path is required``。
  - 联系表(contact sheet)流水线(``build_video_observation`` ->
    ``reset_frames_dir`` / ``sample_video_frames`` /
    ``build_contact_sheets`` / ``observation_signature`` / ``read_cached_observation``)
    与 ``video_metadata`` 的 nvidia ``vali_video_metadata`` 等 helper 体量很大
    (合计 800+ 行),本仓库阶段一只保留 **per-frame 原始采样**,不下沉到
    contact sheet;阶段五再按需接入``assembly_capabilities.visual_observe``
    的 contact sheet 实现。
  - ``video_read_frames`` 全量搬运(``frame_zoom.py`` 263 行),它本身不依赖
    active video,可独立 import、独立单测。

依赖:
- ``from assembly_capabilities.run_context import RunContext`` —— 复用现有精简版
- ``from assembly_capabilities.result import ToolResult`` —— 复用现有 dataclass
- ``from . import ffproc as _ffproc`` —— 共享 subprocess 封装
- ``PIL.Image`` lazy import(帧解码路径才需要)
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ffproc as _ffproc
from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext

# ---------------------------------------------------------------------------
# video_watch_segment constants (kept from source for behaviour parity)
# ---------------------------------------------------------------------------
MAX_SEGMENTS = 8
MAX_SEGMENT_SECONDS = 60.0
MAX_TOTAL_SECONDS = 180.0
LEDGER_DUP_TOLERANCE_SECONDS = 0.2
LEDGER_FPS_TOLERANCE = 1e-6


# ---------------------------------------------------------------------------
# video_read_frames — full port of mcp/ve_tools/frame_zoom.py
# ---------------------------------------------------------------------------
REGION_PRESETS: dict[str, tuple[float, float, float, float]] = {
    "full": (0.0, 0.0, 1.0, 1.0),
    # Broadcast name plate / chyron: bottom-left corner, where CN/EN TV puts it.
    "name_plate": (0.0, 0.72, 0.55, 1.0),
    # The whole bottom band — lower-thirds, burned-in subtitles, tickers.
    "lower_third": (0.0, 0.66, 1.0, 1.0),
    "bottom_left": (0.0, 0.66, 0.5, 1.0),
    "bottom_right": (0.5, 0.66, 1.0, 1.0),
    # Webcam-grid captions and show bugs live in the corners.
    "top_left": (0.0, 0.0, 0.5, 0.34),
    "top_right": (0.5, 0.0, 1.0, 0.34),
    "center": (0.25, 0.25, 0.75, 0.75),
}

# A cap, not a target. The point of this tool is a few precise looks; someone asking
# for hundreds of native-resolution frames wants video_ingest instead.
MAX_FRAMES = 48
DEFAULT_MAX_FRAMES = 8
# Below this the JPEG artefacts start eating the glyphs we came here to read.
JPEG_QUALITY = 95


def _coerce_times(args: dict) -> list[float]:
    """Explicit timestamps, or a window sampled evenly."""
    raw = args.get("timestamps")
    if raw is not None:
        if not isinstance(raw, list) or not raw:
            raise ValueError("timestamps must be a non-empty array of seconds")
        times = []
        for value in raw:
            try:
                times.append(float(value))
            except (TypeError, ValueError):
                raise ValueError(f"timestamps entries must be numbers; got {value!r}")
        return times

    start, end = args.get("start_time"), args.get("end_time")
    if start is None or end is None:
        raise ValueError("pass either timestamps=[...] or start_time+end_time")
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError):
        raise ValueError("start_time and end_time must be numbers")
    if end < start:
        raise ValueError(f"end_time ({end}) is before start_time ({start})")
    count = int(args.get("count") or 3)
    if count < 1:
        raise ValueError("count must be >= 1")
    if count == 1:
        return [(start + end) / 2.0]
    step = (end - start) / (count - 1)
    return [start + step * i for i in range(count)]


def _resolve_region(args: dict) -> tuple[float, float, float, float] | None:
    region = args.get("region")
    if region in (None, "", "full"):
        return None
    if isinstance(region, str):
        key = region.strip().lower()
        if key not in REGION_PRESETS:
            raise ValueError(
                f"unknown region preset {region!r}; use one of {sorted(REGION_PRESETS)} "
                "or pass {left,top,right,bottom} fractions"
            )
        box = REGION_PRESETS[key]
        return None if box == REGION_PRESETS["full"] else box
    if isinstance(region, dict):
        try:
            box = (
                float(region["left"]), float(region["top"]),
                float(region["right"]), float(region["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("region object needs numeric left, top, right, bottom fractions in [0,1]")
        left, top, right, bottom = box
        if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
            raise ValueError(f"region fractions must satisfy 0<=left<right<=1 and 0<=top<bottom<=1; got {box}")
        return box
    raise ValueError("region must be a preset name or an object of fractions")


def _grab_frame(video_path: Path, timestamp: float):
    """One exact frame, decoded at full source resolution."""
    from PIL import Image

    cmd = [
        "ffmpeg", "-v", "error", "-ss", f"{max(0.0, timestamp):.6f}", "-i", str(video_path),
        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
    ]
    proc = _ffproc.run_proc(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not proc.stdout:
        raise ValueError(f"ffmpeg returned no frame at {timestamp:.2f}s from {video_path}")
    with Image.open(__import__("io").BytesIO(proc.stdout)) as image:
        return image.convert("RGB")


def video_read_frames(args: dict, ctx: RunContext) -> ToolResult:
    """Return original-resolution frames, one image per timestamp, for reading detail."""
    from PIL import Image

    video_arg = args.get("video_path")
    if not video_arg:
        return ToolResult(text="[ERROR] video_path is required")
    video_path = ctx.resolve(video_arg)
    if not video_path.is_file():
        return ToolResult(text=f"[ERROR] video not found: {video_path}")

    try:
        times = _coerce_times(args)
        region = _resolve_region(args)
    except ValueError as exc:
        return ToolResult(text=f"[ERROR] {exc}")

    max_frames = int(args.get("max_frames") or DEFAULT_MAX_FRAMES)
    max_frames = max(1, min(max_frames, MAX_FRAMES))
    dropped = max(0, len(times) - max_frames)
    times = sorted(times)[:max_frames]

    try:
        upscale = float(args.get("upscale") or 1.0)
    except (TypeError, ValueError):
        return ToolResult(text="[ERROR] upscale must be a number")
    if not (1.0 <= upscale <= 4.0):
        return ToolResult(text="[ERROR] upscale must be between 1 and 4")

    max_width = args.get("max_width")
    if max_width is not None:
        try:
            max_width = int(max_width)
        except (TypeError, ValueError):
            return ToolResult(text="[ERROR] max_width must be an integer")
        if max_width < 64:
            return ToolResult(text="[ERROR] max_width must be >= 64 (this tool exists to preserve detail)")

    out_dir = ctx.resolve(args.get("output_dir") or f".video_agent/frame_zoom/{video_path.stem}")
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[dict[str, Any]] = []
    failures: list[str] = []
    for timestamp in times:
        try:
            frame = _grab_frame(video_path, timestamp)
        except Exception as exc:  # ffmpeg seek past EOF, unreadable file, ...
            failures.append(f"{timestamp:.2f}s: {exc}")
            continue
        source_size = frame.size
        if region:
            w, h = frame.size
            left, top, right, bottom = region
            box = (int(left * w), int(top * h), max(int(left * w) + 1, int(right * w)),
                   max(int(top * h) + 1, int(bottom * h)))
            frame = frame.crop(box)
        if upscale > 1.0:
            frame = frame.resize(
                (int(frame.width * upscale), int(frame.height * upscale)), Image.LANCZOS
            )
        if max_width and frame.width > max_width:
            ratio = max_width / frame.width
            frame = frame.resize((max_width, max(1, int(frame.height * ratio))), Image.LANCZOS)

        name = f"t{timestamp:09.3f}".replace(".", "_")
        if region:
            name += "_crop"
        path = out_dir / f"{name}.jpg"
        frame.save(path, quality=JPEG_QUALITY, subsampling=0)
        written.append({
            "timestamp": round(timestamp, 3),
            "path": str(path),
            "size": list(frame.size),
            "source_frame_size": list(source_size),
        })

    if not written:
        detail = "; ".join(failures) or "no frames produced"
        return ToolResult(text=f"[ERROR] could not read any frame: {detail}")

    region_note = (
        f"cropped to {args.get('region')!r} " if region else "full frame "
    )
    lines = "\n".join(
        f"- {item['timestamp']:.2f}s  {item['size'][0]}x{item['size'][1]}px  {ctx.virtualize(Path(item['path']))}"
        for item in written
    )
    warn = ""
    if failures:
        warn += f"\n[warn] {len(failures)} timestamp(s) failed: {'; '.join(failures[:3])}"
    if dropped:
        warn += f"\n[warn] {dropped} timestamp(s) beyond max_frames={max_frames} were not read."

    source_w = written[0]["source_frame_size"][0]
    delivered_w = written[0]["size"][0]
    return ToolResult(
        text=(
            f"Read {len(written)} frame(s) at original resolution ({region_note}"
            f"source frame {source_w}px wide, delivered {delivered_w}px). "
            "Each frame is a separate image below — no contact-sheet tiling, so small text "
            "keeps the pixels it had in the source.\n"
            f"{lines}{warn}\n\n"
            "Use this to READ (name plates, lower-thirds, slides, jersey numbers, UI text). "
            "For judging motion, cuts, or who is speaking across a span, video_watch_segment's "
            "sheets are cheaper and better suited.\n"
            "If a name is still not legible here, say so and fall back to a neutral label — "
            "an honest 'speaker A' beats a guessed name."
        ),
        data={
            "tool": "video_read_frames",
            "video_path": str(video_path),
            "region": args.get("region") or "full",
            "frames": written,
            "failed": failures,
        },
        artifacts=[str(out_dir)],
        image_paths=[item["path"] for item in written],
    )


# ---------------------------------------------------------------------------
# video_watch_segment — explicit-path-only simplified port
# ---------------------------------------------------------------------------
# Source: mcp/ve_tools/video_observe.py:117-477 (full version with contact sheets
# + active-video memos). This simplified version keeps:
#   - arg validation (video_path REQUIRED, fps > 0, segments valid)
#   - ledger dedup (a node can rewatch the same window at the same fps without
#     resampling, but a different fps / window produces new frames)
#   - per-frame ffmpeg sampling at the requested fps into .video_agent/...
#
# Removed (per decision ③ in the plan):
#   - the `else: ctx.active_video_path is None ...` branch (RunContext has no
#     active_video_* attrs in this project — caller MUST pass video_path)
#   - `ctx.remember_video(...)` (no session memory)
#   - contact sheet pipelines (reset_frames_dir / build_video_observation /
#     sample_video_frames / build_contact_sheets / observation_signature /
#     read_cached_observation / video_metadata etc., ~600 LOC of helper that
#     lives in assembly_capabilities.visual_observe if needed in phase 5)
#
# Behaviour parity: ToolResult.text still mentions "[window ledger] ..." notes,
# image_paths lists the per-frame JPGs, and ledger JSON lives at
# ``.video_agent/video_watch_ledger/<ledger_id>/covered.json``.


def video_watch_segment(args: dict, ctx: RunContext) -> ToolResult:
    """Sample one local segment at higher FPS; return per-frame image paths.

    Implementation note: this is the **simplified** port (per-frame direct
    ffmpeg sampling). Phase 5 can layer in the contact-sheet pipeline from
    ``assembly_capabilities.visual_observe`` if the orchestrator needs it.
    """
    # ---- arg validation ----------------------------------------------------
    video_arg = args.get("video_path")
    if not video_arg:
        # Per decision ③: the implicit-active video branch is gone. Caller
        # MUST pass video_path explicitly.
        return ToolResult(text="[ERROR] video_path is required (implicit active-video path removed)")
    video_path = ctx.resolve(video_arg)
    if not video_path.is_file():
        return ToolResult(text=f"[ERROR] video not found: {video_path}")

    fps = _coerce_finite_number(args.get("fps"))
    if fps is None:
        return ToolResult(text="[ERROR] fps is required and must be numeric")
    if fps <= 0:
        return ToolResult(text="[ERROR] fps must be a finite number > 0")
    if not shutil.which("ffmpeg"):
        return ToolResult(text="[ERROR] ffmpeg not found on PATH")

    force = args.get("force", False)
    if not isinstance(force, bool):
        return ToolResult(text="[ERROR] force must be a boolean")

    try:
        segments = _parse_watch_segments(args)
    except ValueError as exc:
        return ToolResult(text=f"[ERROR] {exc}")

    # ---- ledger dedup ------------------------------------------------------
    ledger_dir = ctx.work_dir / "video_watch_ledger" / _video_ledger_id(video_path)
    kept, ledger_notes = _filter_covered_segments(segments, ledger_dir, fps=fps, force=force)
    if not kept:
        return ToolResult(
            text="[window ledger] all requested segments duplicate prior watch windows at the same fps; "
            "no new visual observation was produced. Pass force=true to rewatch anyway.\n"
            + "\n".join(ledger_notes),
            data={
                "tool": "video_watch_segment",
                "status": "skipped_duplicate",
                "segments": [{"start_time": s, "end_time": e} for s, e in segments],
                "ledger_notes": ledger_notes,
                "ledger_path": str(ledger_dir / "covered.json"),
            },
        )

    # ---- sampling ----------------------------------------------------------
    base_frames_dir = ctx.resolve(
        args.get("save_frames_dir")
        or f".video_agent/video_frames/{_video_frames_dir_id(video_path)}_watch"
    )
    explicit_frames = bool(args.get("save_frames_dir"))
    multi = len(kept) > 1

    results: list[ToolResult] = []
    for idx, (start_time, end_time) in enumerate(kept):
        suffix = f"seg{idx:02d}_{start_time:.3f}_{end_time:.3f}_fps{fps:g}"
        frames_dir = base_frames_dir / suffix if (multi or not explicit_frames) else base_frames_dir
        result = _sample_segment_to_frames(
            ctx=ctx,
            video_path=video_path,
            start_time=start_time,
            end_time=end_time,
            fps=fps,
            frames_dir=frames_dir,
            extra={"source_video": str(video_path), "start_time": start_time, "end_time": end_time, "fps": fps},
        )
        results.append(result)

    if any(r.text.startswith("[ERROR]") for r in results):
        text = "\n\n".join(r.text for r in results)
        text = (
            f"[ERROR] {sum(1 for r in results if r.text.startswith('[ERROR]'))}/{len(results)} "
            "watch segment(s) failed; no ledger update.\n\n" + text
        )
        return ToolResult(text=text, data={
            "tool": "video_watch_segment",
            "status": "error",
            "segments": [{"start_time": s, "end_time": e} for s, e in kept],
        })

    _update_segment_ledger(ledger_dir, kept, fps=fps)
    return _merge_watch_results(results, ledger_notes, kept)


# ---------------------------------------------------------------------------
# video_watch_segment helpers
# ---------------------------------------------------------------------------
def _coerce_finite_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _coerce_segment_time(value, field: str) -> float:
    parsed = _coerce_finite_number(value)
    if parsed is None:
        raise ValueError(f"{field} must be numeric")
    return parsed


def _parse_watch_segments(args: dict) -> list[tuple[float, float]]:
    """Mirror of source parser; lifted so test_visual_evidence can exercise."""
    raw_segments = args.get("segments")
    if raw_segments is not None:
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ValueError("segments must be a non-empty list of {start, end} objects")
        if len(raw_segments) > MAX_SEGMENTS:
            raise ValueError(f"segments can include at most {MAX_SEGMENTS} windows")
        segments: list[tuple[float, float]] = []
        for idx, item in enumerate(raw_segments):
            if not isinstance(item, dict) or "start" not in item or "end" not in item:
                raise ValueError(f"segments[{idx}] must contain start and end")
            segments.append((
                _coerce_segment_time(item["start"], f"segments[{idx}].start"),
                _coerce_segment_time(item["end"], f"segments[{idx}].end"),
            ))
    else:
        if "start_time" not in args or "end_time" not in args:
            raise ValueError("provide start_time/end_time or segments")
        segments = [(
            _coerce_segment_time(args["start_time"], "start_time"),
            _coerce_segment_time(args["end_time"], "end_time"),
        )]

    total = 0.0
    parsed: list[tuple[float, float]] = []
    for idx, (start, end) in enumerate(segments):
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError(f"segment {idx} start/end must be finite numbers")
        if start < 0 or end < 0:
            raise ValueError(f"segment {idx} start/end must be >= 0")
        if end <= start:
            raise ValueError(f"segment {idx} end_time must be greater than start_time")
        duration = end - start
        if duration > MAX_SEGMENT_SECONDS:
            raise ValueError(f"segment {idx} is {duration:.1f}s, above {MAX_SEGMENT_SECONDS:.0f}s")
        total += duration
        parsed.append((start, end))
    if total > MAX_TOTAL_SECONDS:
        raise ValueError(f"total watch duration is {total:.1f}s, above {MAX_TOTAL_SECONDS:.0f}s")
    return parsed


def _video_ledger_id(video_path: Path) -> str:
    """Identifies a video by path+size+mtime. Returns ``<stem>_<sha[:16]>``."""
    stat = video_path.stat()
    key = f"{video_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    import hashlib
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{video_path.stem}_{digest}"


def _video_frames_dir_id(video_path: Path) -> str:
    """Default frames directory id: ``<stem>_<sha[:8]>`` (matches source)."""
    import hashlib
    digest = hashlib.sha256(str(video_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{video_path.stem}_{digest}"


def _read_segment_ledger(ledger_dir: Path) -> list[tuple[float, float, float | None]]:
    ledger_path = ledger_dir / "covered.json"
    try:
        value = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else []
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    cleaned: list[tuple[float, float, float | None]] = []
    for item in value:
        try:
            if not isinstance(item, (list, tuple)) or len(item) not in (2, 3):
                continue
            start = float(item[0])
            end = float(item[1])
            fps_v = float(item[2]) if len(item) == 3 and item[2] is not None else None
        except Exception:
            continue
        if not (math.isfinite(start) and math.isfinite(end) and start >= 0 and end > start):
            continue
        if fps_v is not None and (not math.isfinite(fps_v) or fps_v <= 0):
            fps_v = None
        cleaned.append((start, end, fps_v))
    return cleaned


def _filter_covered_segments(
    segments: list[tuple[float, float]],
    ledger_dir: Path,
    *,
    fps: float,
    force: bool,
) -> tuple[list[tuple[float, float]], list[str]]:
    ledger = _read_segment_ledger(ledger_dir)
    kept: list[tuple[float, float]] = []
    notes: list[str] = []
    for start, end in segments:
        duplicate = None
        if not force:
            duplicate = next((
                item for item in ledger
                if abs(item[0] - start) <= LEDGER_DUP_TOLERANCE_SECONDS
                and abs(item[1] - end) <= LEDGER_DUP_TOLERANCE_SECONDS
                and item[2] is not None
                and abs(item[2] - fps) <= LEDGER_FPS_TOLERANCE
            ), None)
        if duplicate is not None:
            notes.append(
                f"- {start:g}-{end:g}s duplicates prior watch window "
                f"{duplicate[0]:g}-{duplicate[1]:g}s at fps={duplicate[2]:g}; skipped. "
                "Use a different fps, a tighter window, or force=true to rewatch."
            )
            continue
        kept.append((start, end))
    return kept, notes


def _update_segment_ledger(ledger_dir: Path, segments: list[tuple[float, float]], *, fps: float) -> None:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / "covered.json"
    ledger = _read_segment_ledger(ledger_dir)
    known = {(start, end, entry_fps) for start, end, entry_fps in ledger}
    for start, end in segments:
        if (start, end, fps) not in known:
            ledger.append((start, end, fps))
            known.add((start, end, fps))
    ledger_path.write_text(
        json.dumps([list(entry) for entry in ledger], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _sample_segment_to_frames(
    *,
    ctx: RunContext,
    video_path: Path,
    start_time: float,
    end_time: float,
    fps: float,
    frames_dir: Path,
    extra: dict[str, Any],
) -> ToolResult:
    """Run ffmpeg ``fps=fps`` between (start, end), save JPGs into ``frames_dir``."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    pattern = frames_dir / "f%04d.jpg"
    duration = end_time - start_time
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{start_time:.6f}",
        "-t", f"{duration:.6f}",
        "-i", str(video_path),
        "-vf", f"fps={fps:g}",
        "-q:v", "2",
        str(pattern),
    ]
    proc = _ffproc.run_proc(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        return ToolResult(text=f"[ERROR] segment {start_time:.3f}-{end_time:.3f}s ffmpeg failed: "
                                    f"{(proc.stderr or '').strip()}")

    written = sorted(frames_dir.glob("f*.jpg"))
    if not written:
        return ToolResult(text=f"[ERROR] segment {start_time:.3f}-{end_time:.3f}s produced 0 frames")

    image_paths = [str(p) for p in written]
    return ToolResult(
        text=(f"Watched {start_time:.3f}-{end_time:.3f}s at fps={fps:g}; "
              f"sampled {len(written)} frame(s)."),
        data={
            "tool": "video_watch_segment",
            "start_time": start_time,
            "end_time": end_time,
            "fps": fps,
            "frame_paths": image_paths,
            **extra,
        },
        artifacts=[str(frames_dir)],
        image_paths=image_paths,
    )


def _merge_watch_results(
    results: list[ToolResult],
    ledger_notes: list[str],
    segments: list[tuple[float, float]],
) -> ToolResult:
    image_paths = [p for r in results for p in r.image_paths]
    artifacts = [a for r in results for a in r.artifacts]
    text = "\n\n".join(r.text for r in results)
    if ledger_notes:
        text += "\n\n[window ledger]\n" + "\n".join(ledger_notes)
    text += f"\n\n[window ledger] recorded {len(segments)} watched segment(s)."
    return ToolResult(
        text=text,
        data={
            "tool": "video_watch_segment",
            "segments": [{"start_time": s, "end_time": e} for s, e in segments],
            "results": [r.data for r in results],
            "ledger_notes": ledger_notes,
        },
        artifacts=artifacts,
        image_paths=image_paths,
    )
