"""生成阶段 5 实机端到端验收用的短视频测试素材(单视频 / 多视频两种场景)。

用途(人工关卡与剪映草稿导入 计划 §8 阶段 5 任务 1):
- 实机验收需要"从创建台上传视频 → 剪映显示草稿 → 人工编辑 → 恢复"的全链路
  素材。本脚本用 ffmpeg lavfi 生成**确定性的**合成素材(纯本机,不碰网络),
  覆盖:
  - 单视频场景:只上传 clip_a.mp4;
  - 多视频场景:同时上传 clip_a.mp4 + clip_b.mp4(两个源都会进草稿)。

用法(项目根目录;需 ffmpeg 在 PATH):

    .\\.venv\\Scripts\\python.exe scripts\\make_e2e_test_assets.py
    .\\.venv\\Scripts\\python.exe scripts\\make_e2e_test_assets.py --out-dir D:\\e2e --seconds 8

素材默认 9:16(720x1280,与创建台默认画幅一致,避免触发源归一化裁切),
带音轨(正弦音),片头有可辨识差异(testsrc2 vs smptehdbars),便于在剪映
时间轴上肉眼确认镜头顺序调整(关卡①)是否生效。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

#: (文件名, 视频源表达式, 音频频率);两个片段视觉/听觉都可区分
CLIPS = [
    ("clip_a.mp4", "testsrc2=size=720x1280:rate=30", 440),
    ("clip_b.mp4", "smptehdbars=size=720x1280:rate=30", 660),
]


def make_clip(path: Path, video_src: str, freq: int, seconds: float) -> None:
    """生成一个带音轨的短视频(覆盖写)。"""
    argv = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"{video_src}:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        str(path),
    ]
    subprocess.run(argv, check=True, capture_output=True, text=True, timeout=300)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parents[1] / "outputs" / "e2e-assets"),
        help="素材输出目录(默认 outputs/e2e-assets/)",
    )
    parser.add_argument("--seconds", type=float, default=6.0, help="每个片段时长(秒)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, video_src, freq in CLIPS:
        path = out_dir / name
        make_clip(path, video_src, freq, args.seconds)
        print(f"已生成: {path} ({path.stat().st_size} bytes)")

    print(
        "\n实机验收用法:\n"
        "  单视频场景:创建台只上传 clip_a.mp4\n"
        "  多视频场景:同时上传 clip_a.mp4 + clip_b.mp4\n"
        "  (两个片段画面/音调不同,便于核对关卡① 调序与关卡② BGM 镜像)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
