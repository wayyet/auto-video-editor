#!/usr/bin/env python3
"""合成 assembly 重构的测试素材(P2-5;真 ffmpeg 门控)。

用途:为 P1-2~P1-6 的素材发现/候选/观察链与 P2-5 质量基线提供可复现的
真实编码素材(纯零字节假文件过不了 ffprobe/抽帧)。覆盖计划文档列出的
素材类型:黑帧段、静音、无音轨、旋转元数据、损坏截断、同名双目录、
色彩标记(近似 HDR 核验)、变帧率(VFR)。

用法:
    python scripts/make_assembly_fixtures.py [--out <dir>]
    # 默认输出 tests/fixtures/assembly/media/(gitignore,不入库)

门控:本机 PATH 无 ffmpeg/ffprobe 时打印说明并 exit 1(不静默生成空壳)。
回归测试用 ``ASSEMBLY_TEST_REAL_FFMPEG=1`` 显式开启真编码用例
(tests/unit/assembly_capabilities/conftest.py 既有门禁口径)。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "assembly" / "media"

W, H, FPS, DUR = 320, 240, 12, 4


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 失败({' '.join(cmd[:6])}...):\n{proc.stderr[-800:]}"
        )


def make_black_head(out: Path) -> None:
    """前 1.2 秒纯黑 + 后续 testsrc2(黑帧检测/确定性 trim 回归用)。"""
    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=black:s={W}x{H}:d=1.2:r={FPS}",
        "-f", "lavfi",
        "-i", f"testsrc2=s={W}x{H}:d={DUR - 1.2}:r={FPS}",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[outv]",
        "-map", "[outv]", "-pix_fmt", "yuv420p",
        str(out / "black_head.mp4"),
    ])


def make_silent_audio(out: Path) -> None:
    """全程静音音轨(silencedetect / 音轨先于视频结束回归用)。"""
    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc2=s={W}x{H}:d={DUR}:r={FPS}",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={DUR}",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(out / "silent_audio.mp4"),
    ])


def make_no_audio(out: Path) -> None:
    """纯视频无音轨(无音轨降级路径回归用)。"""
    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc2=s={W}x{H}:d={DUR}:r={FPS}",
        "-pix_fmt", "yuv420p", str(out / "no_audio.mp4"),
    ])


def make_rotated(out: Path) -> None:
    """旋转元数据(旋转视频回归用:display matrix 90°)。"""
    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc2=s={W}x{H}:d={DUR}:r={FPS}",
        "-vf", "transpose=1", "-pix_fmt", "yuv420p",
        str(out / "rotated_90.mp4"),
    ])


def make_color_tagged(out: Path) -> None:
    """bt2020/HDR 色彩标记近似(元数据核验用;真 HDR 源需操作员提供)。"""
    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc2=s={W}x{H}:d={DUR}:r={FPS}",
        "-pix_fmt", "yuv420p",
        "-color_primaries", "bt2020", "-color_trc", "smpte2084",
        "-colorspace", "bt2020nc", str(out / "hdr_tagged.mp4"),
    ])


def make_vfr(out: Path) -> None:
    """变帧率近似:两段不同时间基拼接(帧时间戳不均匀)。"""
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:d=2:r=6",
        "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:d=2:r=24",
        "-filter_complex",
        "[0:v]settb=AVTB,fps=12[a];[1:v]settb=AVTB,fps=12[b];[a][b]concat=n=2:v=1:a=0[outv]",
        "-map", "[outv]", "-pix_fmt", "yuv420p", "-vsync", "vfr",
        str(out / "vfr_mixed.mp4"),
    ])


def make_same_name_pair(out: Path) -> None:
    """两个不同目录里的同名 clip.mp4(P1-2 sidecar 覆盖回归用)。"""
    for sub, hue in (("dir_a", "0"), ("dir_b", "160")):
        d = out / f"same_name_{sub}"
        d.mkdir(parents=True, exist_ok=True)
        _run([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc2=s={W}x{H}:d=2:r={FPS}:hue={hue}",
            "-pix_fmt", "yuv420p", str(d / "clip.mp4"),
        ])


def make_corrupted(out: Path) -> None:
    """截断损坏文件(损坏素材 fail/skip/degraded 回归用)。"""
    good = out / "_tmp_good.mp4"
    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc2=s={W}x{H}:d={DUR}:r={FPS}",
        "-pix_fmt", "yuv420p", str(good),
    ])
    data = good.read_bytes()
    (out / "corrupted.mp4").write_bytes(data[: max(1024, len(data) // 20)])
    good.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print(
            "[make_assembly_fixtures] PATH 上没有 ffmpeg/ffprobe,无法合成真编码素材。\n"
            "  安装 ffmpeg 后重跑;单元回归请继续用 tmp 零字节占位 + 工具 stub(既有口径)。",
            file=sys.stderr,
        )
        return 1

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    makers = [
        make_black_head,
        make_silent_audio,
        make_no_audio,
        make_rotated,
        make_color_tagged,
        make_vfr,
        make_same_name_pair,
        make_corrupted,
    ]
    for maker in makers:
        maker(out)
        print(f"[ok] {maker.__name__}")
    print(f"[done] 素材目录:{out}(共 {len(makers)} 组;不入库,由本脚本按需再生)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
