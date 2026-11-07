"""split_shots 抽帧健壮性(2026-11-07 实机回归)。

背景:69s 4K HEVC 10bit 源在 ``_run_ffmpeg_extract_frames`` 旧实现下
(全分辨率 PNG + timeout=60)抽帧超时 → **静默返回空列表** → 0 镜头 →
下游 storyline 全部级联为空 → 发布 00:00 空草稿挂关卡①。

回归点:
1. 抽帧命令必须降分辨率(scale),且默认超时 300s;
2. 超时**不静默放弃** —— 已落盘的帧继续参与切分;
3. ffmpeg 缺失 / 真的 0 帧时,结果带 ``error=no_frames`` 标记(可诊断)。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import storyline_capabilities.split_shots as ss


def _write_frames(out_dir: Path, count: int) -> None:
    """预写 count 张 8x8 交替黑白 PNG(模拟 ffmpeg 已写出的部分帧)。"""
    pytest.importorskip("PIL")
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        Image.new("L", (8, 8), 255 if i % 2 else 0).save(out_dir / f"frame_{i:05d}.png")


def test_timeout_keeps_partial_frames_and_scales(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抽帧超时:已落盘的帧继续切分(不再静默返回空),命令带 scale + 300s。"""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00")  # 只需存在;ffmpeg 被桩掉
    out_dir = tmp_path / "v.frames"
    _write_frames(out_dir, 10)

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured[cmd[0]] = (cmd, kwargs)
        if cmd[0] == "ffmpeg":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
        # ffprobe duration 查询
        return subprocess.CompletedProcess(cmd, 0, stdout="4.0\n", stderr="")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)

    result = ss.frame_difference_split_shots(video)

    assert result["clips"], "超时也必须用已落盘的帧切出镜头,不得静默返回空"
    assert "error" not in result, f"部分帧成功时不应带 error 标记: {result.get('error')}"
    ffmpeg_cmd, ffmpeg_kwargs = captured["ffmpeg"]
    assert "scale=480:-2" in " ".join(ffmpeg_cmd), "抽帧必须降分辨率(480 宽)"
    assert ffmpeg_kwargs.get("timeout") == 300, "默认抽帧超时应为 300s"


def test_missing_ffmpeg_yields_no_frames_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg 二进制缺失:空结果必须带 error=no_frames(可诊断),而非无声空。"""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00")

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("ffmpeg not found")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)

    result = ss.frame_difference_split_shots(video)

    assert result["clips"] == []
    assert result["error"] == "no_frames"


def test_max_shot_enforced_after_min_shot_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续运动素材回归:帧帧超阈值 → 相邻段全被 min_shot 合并成巨镜头,
    max_shot 后置强制切必须把它切开(40s → 30s + 10s,且时间轴无缝)。"""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00")
    out_dir = tmp_path / "v.frames"
    _write_frames(out_dir, 200)  # 5fps × 200 帧,交替黑白 → 每对相邻帧都是"切点"

    def fake_run(cmd, **kwargs):
        if cmd[0] == "ffmpeg":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
        return subprocess.CompletedProcess(cmd, 0, stdout="40.0\n", stderr="")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)

    result = ss.frame_difference_split_shots(video)
    clips = result["clips"]
    assert len(clips) >= 2, f"40s 巨镜头必须被 max_shot 强制切,实际 {len(clips)} 个"
    for c in clips:
        assert c["source_out_ms"] - c["source_in_ms"] <= ss.DEFAULT_MAX_SHOT_MS
    # 时间轴无缝覆盖
    assert clips[0]["source_in_ms"] == 0
    assert clips[-1]["source_out_ms"] == 40000
    for a, b in zip(clips, clips[1:]):
        assert a["source_out_ms"] == b["source_in_ms"], "强制切段之间不得有空隙"
    ids = [c["clip_id"] for c in clips]
    assert ids == [f"clip_{i:04d}" for i in range(1, len(clips) + 1)]


def test_scaled_extraction_still_detects_cuts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真机回归:降分辨率抽帧不得杀死硬切检测(4s 两段合成视频 → ≥2 镜头)。"""
    if not shutil.which("ffmpeg"):
        fallback = Path(r"C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin")
        if not (fallback / "ffmpeg.exe").exists():
            pytest.skip("本机没有 ffmpeg,跳过需要真实抽帧的用例")
        monkeypatch.setenv("PATH", f"{fallback}{os.pathsep}{os.environ.get('PATH', '')}")
    video = tmp_path / "clip.mp4"
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
        "-f", "lavfi", "-i", "smptebars=size=320x240:rate=10:duration=2",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
        "-pix_fmt", "yuv420p",
        str(video),
    ]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if cp.returncode != 0 or not video.exists():
        pytest.skip(f"生成测试视频失败: {cp.stderr[:200]}")

    result = ss.frame_difference_split_shots(video)
    assert len(result["clips"]) >= 2, (
        "降分辨率抽帧后硬切仍应被检出,实际: "
        f"{[(c['source_in_ms'], c['source_out_ms']) for c in result['clips']]}"
    )
