"""AI 转场媒体工具:首尾帧抽取 / 产物 ffprobe 复检 / 宽高比守卫 / 结果缓存
(2026-11-12 noop 治理阶段2,设计文档 §2.2/§2.3)。

- 抽帧走本机 ffmpeg(``-ss`` 精确 seek + ``-frames:v 1``),**不引入 MoviePy**
  (ADR-001 禁止);图片素材走 Pillow(exif 转正)。
- 生成产物必须经 ffprobe 复检(视频流存在、时长/宽高/fps 实测),sha256 取实测;
  任何一步失败显式抛错,由节点层降级为硬切 —— 不产生假成功。
- 缓存(qa 重试防重复付费):键由调用方组装(from/to clip、素材 hash、取帧时间、
  prompt hash、provider/model、duration、resolution 的 sha256),命中即复用文件。
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

#: 宽高比守卫阈值(FireRed generate_ai_transition.MAX_ASPECT_RATIO_FACTOR)
MAX_ASPECT_RATIO_FACTOR = 1.1
#: "末帧"取帧回退量(毫秒):source_out_ms 恰为片尾时避免 seek 越界,约一帧周期
_END_FRAME_EPSILON_MS = 40

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _tool(name: str) -> str:
    """解析 ffmpeg / ffprobe 可执行文件(环境变量 > PATH > 裸名,响亮失败)。"""
    from agent_gate.agent_bin import resolve_executable

    return resolve_executable(name) or shutil.which(name) or name


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


# ---------------------------------------------------------------------------
# 首尾帧抽取
# ---------------------------------------------------------------------------
def extract_frame(
    media_path: str | Path,
    *,
    at_ms: int,
    output_path: str | Path,
    position: str = "exact",
    timeout: float = 60.0,
) -> Path:
    """从素材取一帧存为 png。

    - 视频:``ffmpeg -ss <sec> -i <media> -frames:v 1 -y <out>``(精确 seek);
      ``position="end"`` 时回退一帧周期,避免 source_out_ms 恰为片尾时越界取空。
    - 图片:Pillow 转正后另存 png(at_ms 对图片无意义,保留参数统一口径)。

    失败显式抛 RuntimeError(节点层把该 pair 降级为硬切并留痕)。
    """
    src = Path(media_path)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if is_image(src):
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.save(out, format="PNG")
        return out
    at = max(0, (int(at_ms) - _END_FRAME_EPSILON_MS)) if position == "end" else max(0, int(at_ms))
    cp = subprocess.run(
        [
            _tool("ffmpeg"), "-y", "-v", "error",
            "-ss", f"{at / 1000:.3f}", "-i", str(src),
            "-frames:v", "1", str(out),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if cp.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(
            f"抽帧失败({src.name} @{at}ms): {(cp.stderr or '').strip()[:200]}"
        )
    return out


# ---------------------------------------------------------------------------
# 产物复检(ffprobe)
# ---------------------------------------------------------------------------
def describe_video_file(path: str | Path, *, timeout: float = 30.0) -> dict[str, Any]:
    """ffprobe 实测视频文件:duration_ms / width / height / fps / sha256。

    视频流缺失或不可解析 → RuntimeError(调用方把该 pair 降级,绝不拿提交参数
    冒充实测值 —— 设计文档 §2.2 任务7)。
    """
    p = Path(path)
    cp = subprocess.run(
        [
            _tool("ffprobe"), "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(p),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if cp.returncode != 0:
        raise RuntimeError(f"ffprobe 失败({p.name}): {(cp.stderr or '').strip()[:200]}")
    data = json.loads(cp.stdout or "{}")
    streams = [s for s in (data.get("streams") or []) if s.get("codec_type") == "video"]
    if not streams:
        raise RuntimeError(f"生成文件没有视频流({p.name}),判为无效产物")
    stream = streams[0]
    duration_s = stream.get("duration") or (data.get("format") or {}).get("duration")
    if duration_s is None:
        raise RuntimeError(f"ffprobe 读不到时长({p.name})")
    num, _, den = str(stream.get("avg_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    return {
        "duration_ms": int(round(float(duration_s) * 1000)),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": round(fps, 3),
        "sha256": sha,
    }


def check_aspect_compatible(
    size_a: tuple[int, int],
    size_b: tuple[int, int],
    *,
    factor: float = MAX_ASPECT_RATIO_FACTOR,
) -> bool:
    """两侧宽高比之比 ≤ factor(宽高为 0 视为未知,放行由产物复检兜底)。"""
    w1, h1 = size_a
    w2, h2 = size_b
    if not (w1 and h1 and w2 and h2):
        return True
    ratio_a, ratio_b = w1 / h1, w2 / h2
    if ratio_a <= 0 or ratio_b <= 0:
        return True
    return max(ratio_a, ratio_b) / min(ratio_a, ratio_b) <= factor


# ---------------------------------------------------------------------------
# 结果缓存(qa 重试防重复付费)
# ---------------------------------------------------------------------------
class TransitionFileCache:
    """job 级 JSON 缓存(``outputs/<job>/storyline/ai_transitions/cache.json``)。

    ``get`` 未命中返回 ``None``;``put`` 原地写回。命中判据由调用方保证条目内
    ``video_path`` 仍存在(文件被清则视为未命中)。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, Any]] = {}
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("转场缓存文件损坏,按空缓存继续: %r", e)

    def get(self, key: str) -> Optional[dict[str, Any]]:
        entry = self._data.get(key)
        if not entry:
            return None
        video_path = entry.get("video_path")
        if not video_path or not Path(str(video_path)).is_file():
            return None
        return dict(entry)

    def put(self, key: str, entry: dict[str, Any]) -> None:
        self._data[key] = dict(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )


def build_pair_cache_key(
    *,
    from_clip_id: str,
    to_clip_id: str,
    from_media_sha: str,
    to_media_sha: str,
    from_at_ms: int,
    to_at_ms: int,
    prompt: str,
    provider: str,
    model: str,
    duration_ms: int,
    resolution: tuple[int, int],
) -> str:
    """缓存键(设计文档 §2.3):pair + 两侧素材 hash + 取帧时间 + prompt hash
    + provider/model + duration + resolution 的 sha256;任一输入变化即失效。"""
    payload = "|".join(
        str(x)
        for x in [
            from_clip_id,
            to_clip_id,
            from_media_sha,
            to_media_sha,
            from_at_ms,
            to_at_ms,
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            provider,
            model,
            duration_ms,
            f"{resolution[0]}x{resolution[1]}",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_ASPECT_RATIO_FACTOR",
    "is_image",
    "extract_frame",
    "describe_video_file",
    "check_aspect_compatible",
    "TransitionFileCache",
    "build_pair_cache_key",
]
