"""本地化 split_shots(plan_v4 §5 阶段 1 / A 类;阶段 5 TransNetV2 已接入)。

检测器两级(2026-11-08 选项C 治本升级):
- **TransNetV2**(``transnetv2_split_shots``):主 venv 进程内推理(用户决定
  主 venv 直接装 CPU torch,不再走独立 venv 子进程),ffmpeg rawvideo 管道
  48×27 抽帧 + soCzech 官方权重(``.storyline/models/``)。
- **帧差版**(``frame_difference_split_shots``):ffmpeg 抽帧 + 帧间像素平均
  差阈值,无 torch 环境的退化替代,也是 TransNetV2 失败时的自动回退。

检测器选择见 ``STORYLINE_SPLIT_SHOTS_DETECTOR``(config.py):auto 默认——
torch 可导入且权重存在才走 TransNetV2,否则/失败时回退帧差,不静默(结果带
``detector_fallback`` 原因,节点写 error_log)。

两种检测器共享同一套 min_shot 合并 / max_shot 强切 / clip_id 重编号管线
(``_clips_from_cuts``),clip 契约与 vendored ``split_shots.py`` 对齐。

重要:plan §1.3 案例一(170s vs 35s)就是因为切分/规划过于细碎导致总时长超
预算;本地实现仍把 min_shot_ms / max_shot_ms 作为硬约束,与原版
``DEFAULT_MIN_SHOT_DURATION_MILLISECONDS``(1000) / ``DEFAULT_MAX_SHOT_DURATION_..``
(30000)对齐。
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


DEFAULT_MIN_SHOT_MS: int = 1000
DEFAULT_MAX_SHOT_MS: int = 30000
DEFAULT_FRAMES_PER_SECOND: int = 5
DEFAULT_DIFF_THRESHOLD: float = 12.0  # 0~255 像素平均差阈值
# 帧差分析抽帧宽度:帧差只需要看"画面变没变",480 宽足够且把抽帧耗时/磁盘
# 占用压低 1-2 个数量级(1080p 10bit HDR 源全尺寸 PNG 单帧 ~9MB)。
DEFAULT_ANALYSIS_WIDTH: int = 480
# ffmpeg 抽帧超时:60s 曾在 69s 4K HEVC 源上超时且静默返回空 → 0 镜头级联。
DEFAULT_EXTRACT_TIMEOUT_S: int = 300

# TransNetV2(选项C):参考实现口径 —— 48×27 RGB 输入、25fps、滑窗 100 步 50、
# sigmoid ≥0.5 记切点。
TRANSTNV2_INPUT_SIZE = (27, 48)  # (H, W)
DEFAULT_TRANSTNV2_FPS: int = 25
DEFAULT_TRANSTNV2_THRESHOLD: float = 0.5

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_TRANSTNV2_WEIGHTS = _REPO_ROOT / ".storyline" / "models" / "transnetv2-pytorch-weights.pth"
_VENDORED_MODEL_PY = _REPO_ROOT / "vendor" / "transnetv2" / "transnetv2_pytorch.py"


def _transnetv2_weights_path() -> Path:
    """权重路径;``STORYLINE_TRANSNETV2_WEIGHTS`` 可覆盖。"""
    env = os.environ.get("STORYLINE_TRANSNETV2_WEIGHTS", "").strip()
    return Path(env) if env else _DEFAULT_TRANSTNV2_WEIGHTS


def _detector_choice() -> str:
    """``STORYLINE_SPLIT_SHOTS_DETECTOR``:auto | transnetv2 | frame_diff。

    读 ``config.STORYLINE_SPLIT_SHOTS_DETECTOR``(导入时从 env 固化,与
    ``STORYLINE_ENABLE_*`` 开关同模式);单测用 monkeypatch.setattr 改。
    """
    from config import STORYLINE_SPLIT_SHOTS_DETECTOR

    raw = str(STORYLINE_SPLIT_SHOTS_DETECTOR or "auto").strip().lower()
    return raw if raw in ("auto", "transnetv2", "frame_diff") else "auto"


def _transnetv2_available() -> tuple[bool, str]:
    """(可用, 不可用原因)。torch 可导入 + vendored 代码 + 权重文件齐才可用。"""
    try:
        import torch  # noqa: F401
    except Exception as e:  # noqa: BLE001 —— ImportError 之外也可能是二进制损坏
        return False, f"torch_import_failed: {e!r}"
    if not _VENDORED_MODEL_PY.exists():
        return False, f"vendored_model_missing: {_VENDORED_MODEL_PY}"
    weights = _transnetv2_weights_path()
    if not weights.exists():
        return False, f"weights_missing: {weights}"
    return True, ""


# ---------------------------------------------------------------------------
# clip 契约(vendored ``open_storyline.nodes.core_nodes.split_shots`` 对齐)
# ---------------------------------------------------------------------------
# 上游 split_shots 产出的每个 clip 必带:
#   kind      —— "video" / "image",``understand_clips`` 靠它决定 media 形态
#   path      —— 素材路径
#   source_ref —— {media_id, start, end, duration}(毫秒)
# 本地帧差版此前**只产出 clip_id / source_in_ms / source_out_ms / timeline_* /
# split_method**,缺 kind 与 source_ref → understand_clips 走到
# "Error: Clip kind not supported: "(集成文档 D5),整条素材链路拿不到画面。
def _make_clip(
    clip_id: str,
    start_ms: int,
    end_ms: int,
    *,
    method: str,
    video_path: str,
) -> dict[str, Any]:
    """构造一个符合 vendored 契约的 clip dict。"""
    return {
        "clip_id": clip_id,
        "kind": "video",
        "path": video_path,
        "source_in_ms": start_ms,
        "source_out_ms": end_ms,
        "timeline_in_ms": start_ms,
        "timeline_out_ms": end_ms,
        "split_method": method,
        "source_ref": {
            "start": start_ms,
            "end": end_ms,
            "duration": max(0, end_ms - start_ms),
        },
    }


# ---------------------------------------------------------------------------
# 简单帧差切分(plan §5 阶段 1 决策:本地版 / 无 torch)
# ---------------------------------------------------------------------------
def _run_ffmpeg_extract_frames(
    video: Path,
    *,
    fps: int,
    out_dir: Path,
    analysis_width: int = DEFAULT_ANALYSIS_WIDTH,
    timeout_s: int = DEFAULT_EXTRACT_TIMEOUT_S,
) -> list[Path]:
    """用 ffmpeg 抽帧到 out_dir,返回 frame 路径列表(按时间顺序)。

    帧差分析不需要原分辨率:统一缩到 ``analysis_width`` 宽(等比、偶数高),
    否则 1080p 10bit 源全尺寸抽帧又慢又占盘,60s 超时曾导致"0 帧静默返回"。
    超时**不再静默放弃**:已写出的帧继续参与切分(部分帧好过空结果);
    只有 ffmpeg 二进制缺失(FileNotFoundError)才返回空,由上层报错。
    """
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
                f"fps={fps},scale={analysis_width}:-2",
                "-q:v",
                "2",
                str(pattern),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError:
        return []
    except subprocess.TimeoutExpired:
        pass  # 超时不放弃:落盘的帧照常用
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


# ---------------------------------------------------------------------------
# 共享后处理:切点毫秒序列 → clips(两种检测器共用)
# ---------------------------------------------------------------------------
def _balanced_max_shot_spans(
    span_ms: int, *, max_shot_ms: int, min_shot_ms: int
) -> list[tuple[int, int]]:
    """把超长片段**平衡切分**成不超过 max_shot 的若干段(相对偏移)。

    2026-11-11 模糊修复阶段3(设计文档 §3.4):旧的"固定步长 max_shot 顺切"
    会把尾料重新切出极短残尾(实机:30.883s 素材 → 30000ms + 883ms,883ms 段
    再被拉长成 11.593s 停帧段)。平衡分段先按总时长算出需要的段数,再把切点
    均匀摊开 —— 每段都接近 ``span/n``,既满足 max_shot 上限,又不产生可避免的
    短尾;仅当总时长本身不足以容纳两段 min_shot 时才允许短段(标记由调用方
    的 method 承载)。

    Returns:
        ``[(start, end), ...]`` 相对 span 起点的偏移,有序、无缝、覆盖全 span。
    """
    if span_ms <= 0:
        return []
    n = max(1, math.ceil(span_ms / max_shot_ms))
    # 段数回退:确保均分后每段不低于 min_shot(能少一段就少一段)
    while n > 1 and math.ceil(span_ms / n) < min_shot_ms:
        n -= 1
    base = math.ceil(span_ms / n)
    cuts = [i * base for i in range(n)] + [span_ms]
    return [(cuts[i], cuts[i + 1]) for i in range(n)]


def _clips_from_cuts(
    cuts_ms: list[int],
    *,
    video_path: str,
    method: str,
    method_max_split: str,
    min_shot_ms: int,
    max_shot_ms: int,
) -> list[dict[str, Any]]:
    """把升序切点毫秒序列(含首 0 与末 duration)按契约加工成 clips。

    步骤与原帧差版实现一致:
    1. 相邻切点成段,短于 ``min_shot_ms`` 并入前段;
    2. 超过 ``max_shot_ms`` 逐段强制切;
    3. 最小长度回填(首段过短并入下一段);
    4. max_shot **后置兜底强切**(2026-11-07 实机回归:连续运动素材帧帧超
       阈值 → 全部相邻段被 min_shot 合并成 1 个巨镜头,违反 max_shot 契约;
       2026-11-11 阶段3 起为**平衡分段**,不再顺切出 <min_shot 的极短残尾);
    5. clip_id 重排。
    """
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
            # 强制切(2026-11-11 阶段3:平衡分段,不再顺切出极短残尾)
            for sub_s, sub_e in _balanced_max_shot_spans(
                e - s, max_shot_ms=max_shot_ms, min_shot_ms=min_shot_ms
            ):
                clips.append(
                    _make_clip(
                        f"clip_{len(clips) + 1:04d}",
                        s + sub_s,
                        s + sub_e,
                        method=method_max_split,
                        video_path=video_path,
                    )
                )
            continue
        clips.append(
            _make_clip(
                f"clip_{len(clips) + 1:04d}",
                s,
                e,
                method=method,
                video_path=video_path,
            )
        )
    # 最小长度回填(把第一个 <min_shot_ms 的 clip 短片合并到下一个)
    final_clips: list[dict[str, Any]] = []
    for c in clips:
        if (
            c["timeline_out_ms"] - c["timeline_in_ms"] < min_shot_ms
            and final_clips
        ):
            merged = final_clips[-1]
            merged["timeline_out_ms"] = c["timeline_out_ms"]
            merged["source_out_ms"] = c["timeline_out_ms"]
            # source_ref 必须同步,否则画面抽取区间与 clip 时间对不上(D5)
            ref = merged.get("source_ref") or {}
            ref["end"] = merged["source_out_ms"]
            ref["duration"] = max(0, merged["source_out_ms"] - merged["source_in_ms"])
            merged["source_ref"] = ref
        else:
            copied = dict(c)
            # source_ref 是嵌套 dict,浅拷贝会与原 clip 共享引用 → 单独复制一份
            copied["source_ref"] = dict(c.get("source_ref") or {})
            final_clips.append(copied)
    # max_shot 后置强制切(2026-11-07 实机回归):连续运动素材(手持旅游片)
    # 帧帧超阈值 → 全部相邻段被 min_shot 合并进同一个 clip,上面的逐对强制切
    # 分支永远轮不到,最终产出 1 个 69s 巨镜头(违反"超过 max_shot 强制切"
    # 的文档契约)。这里对合并后的最终 clip 再兜底切一遍。
    # 2026-11-11 阶段3:切分方式改为平衡分段 —— 旧的固定步长顺切会把尾料
    # 重新切成极短残尾(实机:30.883s → 30000+883ms)。
    enforced: list[dict[str, Any]] = []
    for c in final_clips:
        start = c["timeline_in_ms"]
        end = c["timeline_out_ms"]
        span = end - start
        if span <= max_shot_ms:
            enforced.append(c)
            continue
        for sub_s, sub_e in _balanced_max_shot_spans(
            span, max_shot_ms=max_shot_ms, min_shot_ms=min_shot_ms
        ):
            enforced.append(
                _make_clip(
                    f"clip_{len(enforced) + 1:04d}",
                    start + sub_s,
                    start + sub_e,
                    method=method_max_split,
                    video_path=str(c.get("path") or video_path),
                )
            )
    final_clips = enforced
    # 重排 clip_id
    for i, c in enumerate(final_clips, start=1):
        c["clip_id"] = f"clip_{i:04d}"
    return final_clips


def _probe_duration_ms(video: Path) -> tuple[int, bool]:
    """ffprobe 取真实时长毫秒;(duration_ms, 是否取到)。失败退 (0, False)。"""
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
            return int(round(float(result.stdout.strip()) * 1000)), True
    except Exception:  # noqa: BLE001
        pass
    return 0, False


def frame_difference_split_shots(
    video: Path,
    *,
    fps: int = DEFAULT_FRAMES_PER_SECOND,
    diff_threshold: float = DEFAULT_DIFF_THRESHOLD,
    min_shot_ms: int = DEFAULT_MIN_SHOT_MS,
    max_shot_ms: int = DEFAULT_MAX_SHOT_MS,
    output_dir: Optional[Path] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    analysis_width: int = DEFAULT_ANALYSIS_WIDTH,
    extract_timeout_s: int = DEFAULT_EXTRACT_TIMEOUT_S,
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
        analysis_width: 帧差分析抽帧宽度(降分辨率提速),默认 480。
        extract_timeout_s: ffmpeg 抽帧超时秒数,默认 300。

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
    frames = _run_ffmpeg_extract_frames(
        video,
        fps=fps,
        out_dir=output_dir,
        analysis_width=analysis_width,
        timeout_s=extract_timeout_s,
    )
    if len(frames) < 2:
        return {
            "clips": [],
            "method": "frame_diff",
            "frames_total": len(frames),
            "error": "no_frames",
        }

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
    probed, ok = _probe_duration_ms(video)
    if ok:
        duration_ms = probed

    cuts_ms.append(duration_ms)

    # 4. 边界 → clips;min/max_ms 约束与 TransNetV2 路径共用同一后处理
    final_clips = _clips_from_cuts(
        cuts_ms,
        video_path=str(video),
        method="frame_diff",
        method_max_split="frame_diff_max_split",
        min_shot_ms=min_shot_ms,
        max_shot_ms=max_shot_ms,
    )

    return {
        "clips": final_clips,
        "method": "frame_diff",
        "video_duration_ms": duration_ms,
        "frames_total": len(frames),
        "cut_count": len(cuts_ms) - 2,  # 扣除首尾
    }


# ---------------------------------------------------------------------------
# TransNetV2 路径(阶段 5 / 选项C 治本;主 venv 进程内推理)
# ---------------------------------------------------------------------------
class TransNetV2Unavailable(RuntimeError):
    """TransNetV2 运行时不可用(torch 缺失 / vendored 代码 / 权重缺失)。"""


def _extract_frames_rgb24(
    video: Path,
    *,
    fps: int,
    height: int,
    width: int,
    timeout_s: int,
) -> Any:
    """ffmpeg rawvideo 管道抽帧 → frames uint8 [T,h,w,3]。

    免 PNG 落盘:48×27×3=3888 字节/帧,34s@25fps ≈ 3.3MB,分钟级视频也在
    几十 MB 量级;比帧差版的 PNG 抽帧快一个数量级且无磁盘垃圾。
    """
    import numpy as np

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-i",
        str(video),
        "-vf",
        f"fps={fps},scale={width}:{height}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as e:
        raise TransNetV2Unavailable(f"ffmpeg_not_found: {e!r}") from e
    except subprocess.TimeoutExpired as e:
        # 超时不静默:与帧差版口径一致,已产出的部分字节照常解析
        raw = e.stdout or b""
    else:
        raw = result.stdout
    n_bytes = int(height * width * 3)
    n_frames = len(raw) // n_bytes
    if n_frames < 1:
        return np.zeros((0, height, width, 3), dtype=np.uint8)
    frames = np.frombuffer(raw[: n_frames * n_bytes], dtype=np.uint8).reshape(
        [n_frames, height, width, 3]
    )
    return frames


def _load_transnetv2_model() -> tuple[Any, Any]:
    """加载 vendored TransNetV2 + 官方权重;(model, torch)。"""
    import torch

    spec = importlib.util.spec_from_file_location(
        "transnetv2_pytorch_vendored", _VENDORED_MODEL_PY
    )
    if spec is None or spec.loader is None:  # pragma: no cover — 路径存在性已在 available 检查
        raise TransNetV2Unavailable(f"vendored_model_unloadable: {_VENDORED_MODEL_PY}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    weights = _transnetv2_weights_path()
    model = module.TransNetV2()
    state_dict = torch.load(str(weights), map_location="cpu")
    model.load_state_dict(state_dict)
    model.eval()
    return model, torch


def _transnetv2_cut_frames(
    frames: Any,
    *,
    model: Any,
    torch_mod: Any,
    threshold: float,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> list[int]:
    """滑窗推理 → 切点帧号列表(该帧为**新镜头第一帧**,即 0→1 跳变处)。

    口径与官方 ``predict_frames`` 一致:窗口 100 帧、步进 50、首尾各补 25 帧
    (重复首帧/末帧),每窗取 [25:75] 的有效输出;sigmoid ≥ threshold 记切。
    """
    total = int(frames.shape[0])
    pad_start = 25
    pad_end = 25 + 50 - (total % 50 if total % 50 != 0 else 50)
    import numpy as np

    first = frames[0:1]
    last = frames[total - 1 : total]
    padded = np.concatenate([first] * pad_start + [frames] + [last] * pad_end, axis=0)

    preds: list[Any] = []
    ptr = 0
    windows = 0
    with torch_mod.no_grad():
        while ptr + 100 <= padded.shape[0]:
            window = torch_mod.from_numpy(padded[ptr : ptr + 100]).unsqueeze(0)
            one_hot, _many_hot = model(window)
            prob = torch_mod.sigmoid(one_hot)[0, 25:75, 0].numpy()
            preds.append(prob)
            ptr += 50
            windows += 1
            if progress_cb:
                progress_cb(min(windows * 50, total), total)
    if not preds:
        return []
    all_pred = np.concatenate(preds)[:total]
    return [int(i) for i in range(1, total) if all_pred[i] > threshold]


def transnetv2_split_shots(
    video: Path,
    *,
    fps: int = DEFAULT_TRANSTNV2_FPS,
    threshold: float = DEFAULT_TRANSTNV2_THRESHOLD,
    min_shot_ms: int = DEFAULT_MIN_SHOT_MS,
    max_shot_ms: int = DEFAULT_MAX_SHOT_MS,
    extract_timeout_s: int = DEFAULT_EXTRACT_TIMEOUT_S,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """TransNetV2 镜头切分(选项C 治本;主 venv torch 进程内推理)。

    Returns:
        与 ``frame_difference_split_shots`` 同构:
        ``{"clips": [...], "method": "transnetv2", "video_duration_ms", ...}``
        不可用时抛 ``TransNetV2Unavailable``(由上层决定回退,不静默)。
    """
    import numpy as np

    video = Path(video)
    if not video.exists():
        return {"clips": [], "method": "transnetv2", "error": "video_not_found"}

    available, reason = _transnetv2_available()
    if not available:
        raise TransNetV2Unavailable(reason)

    height, width = TRANSTNV2_INPUT_SIZE
    frames = _extract_frames_rgb24(
        video,
        fps=fps,
        height=height,
        width=width,
        timeout_s=extract_timeout_s,
    )
    if frames.shape[0] < 2:
        return {
            "clips": [],
            "method": "transnetv2",
            "frames_total": int(frames.shape[0]),
            "error": "no_frames",
        }

    model, torch_mod = _load_transnetv2_model()
    cut_indices = _transnetv2_cut_frames(
        frames,
        model=model,
        torch_mod=torch_mod,
        threshold=threshold,
        progress_cb=progress_cb,
    )

    duration_ms = (frames.shape[0]) * int(round(1000.0 / fps))
    probed, ok = _probe_duration_ms(video)
    if ok:
        duration_ms = probed

    cuts_ms = [0] + [int(round(i * 1000.0 / fps)) for i in cut_indices]
    cuts_ms.append(duration_ms)

    final_clips = _clips_from_cuts(
        cuts_ms,
        video_path=str(video),
        method="transnetv2",
        method_max_split="transnetv2_max_split",
        min_shot_ms=min_shot_ms,
        max_shot_ms=max_shot_ms,
    )
    return {
        "clips": final_clips,
        "method": "transnetv2",
        "video_duration_ms": duration_ms,
        "frames_total": int(frames.shape[0]),
        "cut_count": len(cut_indices),
        "threshold": threshold,
        "fps": fps,
    }


def _split_one_video(
    video_path: Path,
    media_id: str,
    *,
    detector: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """单视频检测分派:transnetv2(可用/失败回退)或 frame_diff。

    返回帧差版/TransNet 版结果 dict;回退时带 ``detector_fallback`` 原因
    (节点据此写 error_log,不静默)。media_id 接线在本函数内完成。
    """
    fallback: dict[str, Any] | None = None
    if detector in ("auto", "transnetv2"):
        available, reason = _transnetv2_available()
        if detector == "auto" and not available:
            fallback = {"requested": "auto", "reason": reason}
        else:
            try:
                result = transnetv2_split_shots(video_path, **{
                    "fps": kwargs.get("fps", DEFAULT_FRAMES_PER_SECOND),
                    "threshold": kwargs.get("transnetv2_threshold", DEFAULT_TRANSTNV2_THRESHOLD),
                    "min_shot_ms": kwargs.get("min_shot_ms", DEFAULT_MIN_SHOT_MS),
                    "max_shot_ms": kwargs.get("max_shot_ms", DEFAULT_MAX_SHOT_MS),
                    "extract_timeout_s": kwargs.get("extract_timeout_s", DEFAULT_EXTRACT_TIMEOUT_S),
                })
            except Exception as e:  # noqa: BLE001 —— 推理任何失败都回退,原因上报
                fallback = {"requested": detector, "reason": f"transnetv2_failed: {e!r}"}
            else:
                if result.get("error"):  # video_not_found / no_frames 走帧差同款错误通道
                    return {**result, "media_id": media_id, "detector_used": "transnetv2"}
                for c in result.get("clips", []):
                    c["media_id"] = media_id
                    ref = c.get("source_ref") or {}
                    ref["media_id"] = media_id
                    c["source_ref"] = ref
                result["media_id"] = media_id
                result["detector_used"] = "transnetv2"
                return result
    result = frame_difference_split_shots(
        video_path,
        **{
            k: v
            for k, v in kwargs.items()
            if k in ("fps", "diff_threshold", "min_shot_ms", "max_shot_ms", "analysis_width", "extract_timeout_s")
        },
    )
    if fallback:
        result["detector_fallback"] = fallback
    result["detector_used"] = "frame_diff"
    for c in result.get("clips", []):
        c["media_id"] = media_id
        c["path"] = str(video_path)
        ref = c.get("source_ref") or {}
        ref["media_id"] = media_id
        c["source_ref"] = ref
    result["media_id"] = media_id
    return result


# ---------------------------------------------------------------------------
# 对外主入口 — 检测器选择(auto:TransNetV2 优先,失败回退帧差)
# ---------------------------------------------------------------------------
def split_shots(
    media_artifact: dict[str, Any] | str | Path | list[dict[str, Any]],
    *,
    fps: int = DEFAULT_FRAMES_PER_SECOND,
    diff_threshold: float = DEFAULT_DIFF_THRESHOLD,
    min_shot_ms: int = DEFAULT_MIN_SHOT_MS,
    max_shot_ms: int = DEFAULT_MAX_SHOT_MS,
    analysis_width: int = DEFAULT_ANALYSIS_WIDTH,
    extract_timeout_s: int = DEFAULT_EXTRACT_TIMEOUT_S,
    detector: str | None = None,
    transnetv2_threshold: float = DEFAULT_TRANSTNV2_THRESHOLD,
) -> dict[str, Any]:
    """对单个 video 做镜头切分;或对 list 输入 → 调 ``split_shots_for_media_list``。

    ``media_artifact`` 接受:
    - dict ``{"path": "..."}`` (load_media 输出格式)
    - str / Path 直接当 video_path
    - **list[dict]**(``load_media`` 输出的 ``{"media": [...]}`` 风格的 media 列表)
      → 自动转 ``split_shots_for_media_list``,返回 ``{"shots": ..., "method": ...}``
      与下面 understand_clips 消费方兼容。

    ``detector``:None 时读 ``STORYLINE_SPLIT_SHOTS_DETECTOR``(默认 auto)。
    auto/显式 transnetv2 时用 TransNetV2 主 venv 推理;不可用或推理失败回退
    frame_diff,结果带 ``detector_fallback`` 原因(节点写 error_log,不静默)。

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
            analysis_width=analysis_width,
            extract_timeout_s=extract_timeout_s,
            detector=detector,
            transnetv2_threshold=transnetv2_threshold,
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

    chosen = detector or _detector_choice()
    result = _split_one_video(
        video_path,
        media_id,
        detector=chosen,
        fps=fps,
        diff_threshold=diff_threshold,
        min_shot_ms=min_shot_ms,
        max_shot_ms=max_shot_ms,
        analysis_width=analysis_width,
        extract_timeout_s=extract_timeout_s,
        transnetv2_threshold=transnetv2_threshold,
    )
    return result


def split_shots_for_media_list(
    media_list: Iterable[dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any]:
    """对一组 media(``load_media`` 输出的 ``{"media": [...]}`` 风格)依次切分。

    Returns dict 带顶层 ``shots``(vendored 兼容),内含每个 video 的 ``clips``。

    **clip_id 全局唯一**:单视频内部会把 clip 重编号为
    ``clip_0001, clip_0002, ...``;N 个视频 → N 组同号 clip,下游
    ``filter_clips`` 用 ``clip_id`` 当 LLM 决策标识符时会歧义(误保留/误丢弃)。
    故在合并阶段按 media 顺序做**全局连续重编号**。
    单视频时结果与改动前**完全一致**(``clip_0001..clip_000N``)。

    ``method`` 取第一个视频实际使用的检测器(frame_diff / transnetv2)。
    """
    shots: dict[str, list[dict[str, Any]]] = {}
    fallbacks: list[dict[str, Any]] = []
    detector_used = "frame_diff"
    chosen = kwargs.pop("detector", None) or _detector_choice()
    for m in media_list or []:
        if m.get("media_type") != "video":
            continue
        # 走模块级 split_shots(单视频入口)保持既有桩接缝(单测 monkeypatch)
        result = split_shots(m, detector=chosen, **kwargs)
        if result.get("detector_fallback"):
            fallbacks.append(
                {"media_id": m.get("media_id"), **result["detector_fallback"]}
            )
        detector_used = result.get("detector_used") or detector_used
        shots[m.get("media_id", "")] = result.get("clips", [])

    # 全局连续重编号(跨源唯一)
    counter = 0
    for clips in shots.values():
        for c in clips:
            counter += 1
            c["clip_id"] = f"clip_{counter:04d}"
    out = {"shots": shots, "method": detector_used}
    if fallbacks:
        out["detector_fallbacks"] = fallbacks
    return out


__all__ = [
    "DEFAULT_MIN_SHOT_MS",
    "DEFAULT_MAX_SHOT_MS",
    "DEFAULT_FRAMES_PER_SECOND",
    "DEFAULT_DIFF_THRESHOLD",
    "DEFAULT_ANALYSIS_WIDTH",
    "DEFAULT_EXTRACT_TIMEOUT_S",
    "DEFAULT_TRANSTNV2_FPS",
    "DEFAULT_TRANSTNV2_THRESHOLD",
    "TransNetV2Unavailable",
    "frame_difference_split_shots",
    "transnetv2_split_shots",
    "split_shots",
    "split_shots_for_media_list",
]
