"""本地化 split_shots(plan_v4 §5 阶段 1 / A 类)。

策略(plan §5 阶段 1 第 5 行):
- **TransNetV2 走 vendored venv**(本机主 venv 不装 torch)。
- **本地实现**简化版 ``frame_difference_split_shots``:用 ffmpeg 抽帧,
  用帧间像素平均差作阈值,产出 scene-cut 候选。
  这是 vendored 主版本``detect_scenes_with_transnetv2_without_proxy``的退化
  替代,适合本地 CI 烟测与无 torch 环境的"快速版"。

重要:plan §1.3 案例一(170s vs 35s)就是因为切分/规划过于细碎导致总时长超
预算;本地实现仍把 min_shot_ms / max_shot_ms 作为硬约束,与 vendored
``DEFAULT_MIN_SHOT_DURATION_MILLISECONDS``(1000) / ``DEFAULT_MAX_SHOT_DURATION_..``
(30000)对齐。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


DEFAULT_MIN_SHOT_MS: int = 1000
DEFAULT_MAX_SHOT_MS: int = 30000
DEFAULT_FRAMES_PER_SECOND: int = 5
DEFAULT_DIFF_THRESHOLD: float = 12.0  # 0~255 像素平均差阈值


# ---------------------------------------------------------------------------
# 简单帧差切分(plan §5 阶段 1 决策:本地版 / 无 torch)
# ---------------------------------------------------------------------------
def _run_ffmpeg_extract_frames(
    video: Path,
    *,
    fps: int,
    out_dir: Path,
) -> list[Path]:
    """用 ffmpeg 抽帧到 out_dir,返回 frame 路径列表(按时间顺序)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "frame_%05d.png"
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video),
                "-vf",
                f"fps={fps}",
                "-q:v",
                "2",
                str(pattern),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return sorted(out_dir.glob("frame_*.png"))


def _frame_avg_diff(a: Path, b: Path) -> float:
    """两帧之间的像素平均差(RGB)。无 Pillow 时返回 0 — 视为不变。"""
    try:
        from PIL import Image  # type: ignore[import-untyped]
    except ImportError:
        return 0.0
    try:
        ia = Image.open(a).convert("L")
        ib = Image.open(b).convert("L")
        w, h = ia.size
        if (w, h) != ib.size:
            ib = ib.resize((w, h))
        ba = ia.tobytes()
        bb = ib.tobytes()
        if len(ba) != len(bb) or not ba:
            return 0.0
        # 平均像素差 = sum(|a[i]-b[i]|) / N / 255 * 100
        total = 0
        # 取样比较快;每 16 像素取一次
        step = 16
        n = 0
        for i in range(0, len(ba) - 1, step):
            total += abs(ba[i] - bb[i])
            n += 1
        if n == 0:
            return 0.0
        return total / n
    except Exception:  # noqa: BLE001
        return 0.0


def frame_difference_split_shots(
    video: Path,
    *,
    fps: int = DEFAULT_FRAMES_PER_SECOND,
    diff_threshold: float = DEFAULT_DIFF_THRESHOLD,
    min_shot_ms: int = DEFAULT_MIN_SHOT_MS,
    max_shot_ms: int = DEFAULT_MAX_SHOT_MS,
    output_dir: Optional[Path] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """帧差版镜头切分(本地;无 torch / 无 TransNetV2)。

    Args:
        video: 输入视频路径。
        fps: 抽帧频率(每秒几帧),默认 5 fps。
        diff_threshold: 帧差阈值(0~255 平均像素差),默认 12.0。
        min_shot_ms: 最小时长;短于此值的相邻段会被合并。
        max_shot_ms: 最大时长;超过此值的镜头会被强制切(保留断点)。
        output_dir: 抽帧临时目录;None 时用 ``<video>.frames/``。
        progress_cb: 进度回调 ``(done, total)``,用于 UI 进度条。

    Returns:
        ``{"clips": [{clip_id, source_in_ms, source_out_ms, ...}, ...], "method": "frame_diff", ...}``
    """
    video = Path(video)
    if not video.exists():
        return {"clips": [], "method": "frame_diff", "error": "video_not_found"}

    if output_dir is None:
        output_dir = video.with_suffix(".frames")
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. ffmpeg 抽帧
    frames = _run_ffmpeg_extract_frames(video, fps=fps, out_dir=output_dir)
    if len(frames) < 2:
        return {"clips": [], "method": "frame_diff", "frames_total": len(frames)}

    # 2. 帧间差,生成边界候选
    cuts_ms: list[int] = [0]  # 第一帧的位置 = 0 ms
    interval_ms = int(round(1000.0 / fps))
    for i in range(1, len(frames)):
        if progress_cb and i % 20 == 0:
            progress_cb(i, len(frames))
        diff = _frame_avg_diff(frames[i - 1], frames[i])
        if diff >= diff_threshold:
            ts_ms = i * interval_ms
            cuts_ms.append(ts_ms)

    # 3. 末帧位置:用 ffmpeg 查 duration(退化:等于 (n-1)*interval)
    duration_ms = (len(frames) - 1) * interval_ms
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            duration_ms = int(round(float(result.stdout.strip()) * 1000))
    except Exception:  # noqa: BLE001
        pass

    cuts_ms.append(duration_ms)

    # 4. 边界 → clips;应用 min/max_ms 约束
    clips: list[dict[str, Any]] = []
    for i in range(len(cuts_ms) - 1):
        s = cuts_ms[i]
        e = cuts_ms[i + 1]
        if e - s < min_shot_ms and clips:
            # 与上一段合并
            clips[-1]["timeline_out_ms"] = e
            clips[-1]["source_out_ms"] = e
            continue
        if e - s > max_shot_ms:
            # 强制切
            cur = s
            while cur + max_shot_ms < e:
                nxt = cur + max_shot_ms
                clips.append(
                    {
                        "clip_id": f"clip_{len(clips) + 1:04d}",
                        "source_in_ms": cur,
                        "source_out_ms": nxt,
                        "timeline_in_ms": cur,
                        "timeline_out_ms": nxt,
                        "split_method": "frame_diff_max_split",
                    }
                )
                cur = nxt
            if e - cur > 0:
                clips.append(
                    {
                        "clip_id": f"clip_{len(clips) + 1:04d}",
                        "source_in_ms": cur,
                        "source_out_ms": e,
                        "timeline_in_ms": cur,
                        "timeline_out_ms": e,
                        "split_method": "frame_diff_max_split",
                    }
                )
            continue
        clips.append(
            {
                "clip_id": f"clip_{len(clips) + 1:04d}",
                "source_in_ms": s,
                "source_out_ms": e,
                "timeline_in_ms": s,
                "timeline_out_ms": e,
                "split_method": "frame_diff",
            }
        )
    # 最小长度回填(把第一个 <min_shot_ms 的 clip 短片合并到下一个)
    final_clips: list[dict[str, Any]] = []
    for c in clips:
        if (
            c["timeline_out_ms"] - c["timeline_in_ms"] < min_shot_ms
            and final_clips
        ):
            final_clips[-1]["timeline_out_ms"] = c["timeline_out_ms"]
            final_clips[-1]["source_out_ms"] = c["timeline_out_ms"]
        else:
            final_clips.append(dict(c))
    # 重排 clip_id
    for i, c in enumerate(final_clips, start=1):
        c["clip_id"] = f"clip_{i:04d}"

    return {
        "clips": final_clips,
        "method": "frame_diff",
        "video_duration_ms": duration_ms,
        "frames_total": len(frames),
        "cut_count": len(cuts_ms) - 2,  # 扣除首尾
    }


# ---------------------------------------------------------------------------
# TransNetV2 路径(仍走 vendored venv)
# ---------------------------------------------------------------------------
def split_shots_via_vendored(
    video: Path,
    *,
    vendored_venv_python: Path,
    script_path: Optional[Path] = None,
    timeout_s: int = 600,
) -> dict[str, Any]:
    """TransNetV2 / 完整 split_shots 走 vendored venv 子进程。

    **不在阶段 1 实现**(plan §6.4 + §5 阶段 5):vendored OpenStoryline copy 的
    runway/main entry 还没建;阶段 1 仅提供此 stub + 注释,阶段 5 才补 `_mcp_passthrough`
    的 vendored 调用路径。
    """
    raise NotImplementedError(
        "split_shots_via_vendored 是阶段 5 工作;阶段 1 用 frame_difference_split_shots"
    )


# ---------------------------------------------------------------------------
# 对外主入口 — 阶段 1 默认 frame_difference,TransNetV2 阶段 5 接入
# ---------------------------------------------------------------------------
def split_shots(
    media_artifact: dict[str, Any] | str | Path | list[dict[str, Any]],
    *,
    fps: int = DEFAULT_FRAMES_PER_SECOND,
    diff_threshold: float = DEFAULT_DIFF_THRESHOLD,
    min_shot_ms: int = DEFAULT_MIN_SHOT_MS,
    max_shot_ms: int = DEFAULT_MAX_SHOT_MS,
) -> dict[str, Any]:
    """对单个 video 做镜头切分;或对 list 输入 → 调 ``split_shots_for_media_list``。

    ``media_artifact`` 接受:
    - dict ``{"path": "..."}`` (load_media 输出格式)
    - str / Path 直接当 video_path
    - **list[dict]**(``load_media`` 输出的 ``{"media": [...]}`` 风格的 media 列表)
      → 自动转 ``split_shots_for_media_list``,返回 ``{"shots": ..., "method": ...}``
      与下面 understand_clips 消费方兼容。

    Returns dict key ``clips`` 与 vendored ``split_shots.py`` 对齐;list 输入时
    返回 ``{"shots": {media_id: [clip_dict]}}``。
    """
    if isinstance(media_artifact, list):
        return split_shots_for_media_list(
            media_artifact,
            fps=fps,
            diff_threshold=diff_threshold,
            min_shot_ms=min_shot_ms,
            max_shot_ms=max_shot_ms,
        )
    if isinstance(media_artifact, (str, Path)):
        video_path = Path(media_artifact)
        media_id = str(media_artifact)
    elif isinstance(media_artifact, dict):
        # 优先用 path 字段;否则取 media_id 当唯一标识
        video_path = Path(media_artifact.get("path") or "")
        media_id = str(media_artifact.get("media_id") or "")
    else:
        return {"clips": [], "method": "frame_diff", "error": "invalid_artifact"}

    if not video_path.exists():
        return {"clips": [], "method": "frame_diff", "error": "video_not_found"}

    result = frame_difference_split_shots(
        video_path,
        fps=fps,
        diff_threshold=diff_threshold,
        min_shot_ms=min_shot_ms,
        max_shot_ms=max_shot_ms,
    )
    # 关联 media_id
    for c in result.get("clips", []):
        c["media_id"] = media_id
    result["media_id"] = media_id
    return result


def split_shots_for_media_list(
    media_list: Iterable[dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any]:
    """对一组 media(``load_media`` 输出的 ``{"media": [...]}`` 风格)依次切分。

    Returns dict 带顶层 ``shots``(vendored 兼容),内含每个 video 的 ``clips``。
    """
    shots: dict[str, list[dict[str, Any]]] = {}
    for m in media_list or []:
        if m.get("media_type") != "video":
            continue
        result = split_shots(m, **kwargs)
        shots[m.get("media_id", "")] = result.get("clips", [])
    return {"shots": shots, "method": "frame_diff"}


__all__ = [
    "DEFAULT_MIN_SHOT_MS",
    "DEFAULT_MAX_SHOT_MS",
    "DEFAULT_FRAMES_PER_SECOND",
    "DEFAULT_DIFF_THRESHOLD",
    "frame_difference_split_shots",
    "split_shots_via_vendored",
    "split_shots",
    "split_shots_for_media_list",
]
