"""生成实机端到端验收用的测试素材(阶段 5 基础 + 阶段 7 变体全集)。

用途:
- 阶段 5(人工关卡与剪映草稿导入 计划 §8 阶段 5 任务 1):单视频 / 多视频场景;
- 阶段 7(效果缺失修复 计划 §8 阶段 7 任务 1):单镜头、多镜头、横竖屏、
  4K HEVC、无音轨/有音轨的素材矩阵。

本脚本用 ffmpeg lavfi 生成**确定性的**合成素材(纯本机,不碰网络):

======================  ==============================  ==================
变体                     文件                            覆盖点
======================  ==============================  ==================
base                    clip_a.mp4 / clip_b.mp4         多镜头多素材(竖屏,带音轨)
long                    clip_long.mp4                   单镜头(30s 单一源)
landscape               clip_a_16x9.mp4 / clip_b_16x9   横屏 16:9
hevc4k                  clip_4k_hevc.mp4                4K HEVC(libx265)
noaudio                 clip_noaudio.mp4                无音轨
======================  ==============================  ==================

用法(项目根目录;需 ffmpeg 在 PATH):

    .\\.venv\\Scripts\\python.exe scripts\\make_e2e_test_assets.py
    .\\.venv\\Scripts\\python.exe scripts\\make_e2e_test_assets.py --variant base,long
    .\\.venv\\Scripts\\python.exe scripts\\make_e2e_test_assets.py --out-dir D:\\e2e --seconds 8

base 素材默认 9:16(720x1280,与创建台默认画幅一致,避免触发源归一化裁切),
带音轨(正弦音),片头有可辨识差异(testsrc2 vs smptehdbars),便于在剪映
时间轴上肉眼确认镜头顺序调整(关卡①)是否生效。中/英文 narration 不是素材
属性 —— 由创建任务的 lang 参数驱动(实机验收清单见阶段 7 记录文档)。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

#: (文件名, 视频源表达式, 音频频率);两个片段视觉/听觉都可区分
CLIPS = [
    ("clip_a.mp4", "testsrc2=size=720x1280:rate=30", 440),
    ("clip_b.mp4", "smptehdbars=size=720x1280:rate=30", 660),
]

#: 横屏 16:9 变体
CLIPS_LANDSCAPE = [
    ("clip_a_16x9.mp4", "testsrc2=size=1280x720:rate=30", 440),
    ("clip_b_16x9.mp4", "smptehdbars=size=1280x720:rate=30", 660),
]

#: 生成完毕的变体名集合(用于 --variant 选择)
VARIANTS: tuple[str, ...] = ("base", "long", "landscape", "hevc4k", "noaudio")


def make_clip(
    path: Path,
    video_src: str,
    freq: int | None,
    seconds: float,
    *,
    codec: str = "libx264",
    extra: list[str] | None = None,
) -> None:
    """生成一个短视频(覆盖写);``freq=None`` 表示无音轨。"""
    argv = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"{video_src}:duration={seconds}",
    ]
    if freq is not None:
        argv += ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}"]
    argv += ["-c:v", codec, "-pix_fmt", "yuv420p"]
    if freq is not None:
        argv += ["-c:a", "aac"]
    else:
        argv += ["-an"]
    if extra:
        argv += extra
    argv += ["-shortest", str(path)]
    subprocess.run(argv, check=True, capture_output=True, text=True, timeout=600)


def probe_clip(path: Path) -> dict:
    """ffprobe 关键字段(编码/宽高/时长/有无音轨),生成后逐一自证。"""
    argv = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height",
        "-show_entries", "format=duration",
        "-show_entries", "stream=index,codec_type",
        "-of", "json", str(path),
    ]
    out = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=60).stdout
    data = json.loads(out)
    video = (data.get("streams") or [{}])[0]
    return {
        "codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "duration_s": float((data.get("format") or {}).get("duration") or 0),
    }


def has_audio_stream(path: Path) -> bool:
    argv = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
    ]
    return bool(subprocess.run(argv, check=True, capture_output=True, text=True, timeout=60).stdout.strip())


def _generate(variant: str, out_dir: Path, seconds: float) -> list[Path]:
    made: list[Path] = []
    if variant == "base":
        for name, video_src, freq in CLIPS:
            path = out_dir / name
            make_clip(path, video_src, freq, seconds)
            made.append(path)
    elif variant == "long":
        # 单镜头:同一源拉长(30s 或 --seconds 的 5 倍),验证"无剪辑边界"路径
        long_seconds = max(seconds * 5, 30.0)
        path = out_dir / "clip_long.mp4"
        make_clip(path, "testsrc2=size=720x1280:rate=30", 440, long_seconds)
        made.append(path)
    elif variant == "landscape":
        for name, video_src, freq in CLIPS_LANDSCAPE:
            path = out_dir / name
            make_clip(path, video_src, freq, seconds)
            made.append(path)
    elif variant == "hevc4k":
        # 4K HEVC:ultrafast 控制编码耗时;10s 固定(4K 编码慢,不随 --seconds 放大)
        path = out_dir / "clip_4k_hevc.mp4"
        make_clip(
            path,
            "testsrc2=size=3840x2160:rate=24",
            440,
            min(seconds, 10.0),
            codec="libx265",
            extra=["-preset", "ultrafast", "-x265-params", "log-level=error", "-tag:v", "hvc1"],
        )
        made.append(path)
    elif variant == "noaudio":
        path = out_dir / "clip_noaudio.mp4"
        make_clip(path, "smptehdbars=size=720x1280:rate=30", None, seconds)
        made.append(path)
    return made


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parents[1] / "outputs" / "e2e-assets"),
        help="素材输出目录(默认 outputs/e2e-assets/)",
    )
    parser.add_argument("--seconds", type=float, default=6.0, help="每个片段时长(秒)")
    parser.add_argument(
        "--variant",
        default="all",
        help=f"生成哪些变体: all 或逗号组合 {','.join(VARIANTS)}",
    )
    args = parser.parse_args()

    selected = list(VARIANTS) if args.variant.strip().lower() == "all" else [
        v.strip() for v in args.variant.split(",") if v.strip()
    ]
    unknown = [v for v in selected if v not in VARIANTS]
    if unknown:
        print(f"未知变体: {unknown}(可用: all / {','.join(VARIANTS)})", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for variant in selected:
        for path in _generate(variant, out_dir, args.seconds):
            try:
                info = probe_clip(path)
                info["audio"] = has_audio_stream(path)
            except (subprocess.SubprocessError, ValueError, KeyError) as exc:
                failures.append(f"{path}: probe 失败: {exc}")
                continue
            print(f"已生成[{variant}]: {path.name}  {info['width']}x{info['height']} "
                  f"{info['codec']} {info['duration_s']:.1f}s audio={info['audio']} "
                  f"({path.stat().st_size} bytes)")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1

    print(
        "\n实机验收用法(阶段 7 矩阵;每场景新建 Thread,不复用 checkpoint):\n"
        "  单镜头: 只上传 clip_long.mp4\n"
        "  多镜头多素材: 同时上传 clip_a.mp4 + clip_b.mp4\n"
        "  横屏: 上传 clip_a_16x9.mp4 + clip_b_16x9.mp4(创建时画幅选 16:9)\n"
        "  4K HEVC: 上传 clip_4k_hevc.mp4(验证转码/抽帧耗时与草稿生成)\n"
        "  无音轨: 上传 clip_noaudio.mp4(验证无源音轨路径)\n"
        "  中/英文 narration: 同一素材,创建时输出语言分别选 中文 / 英文"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
