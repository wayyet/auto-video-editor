"""阶段一单测共享 fixture / 配置。

``SKIP_REAL_FFMPEG``:默认 False;若环境变量 ``ASSEMBLY_TEST_REAL_FFMPEG=0``
或本机没有 ``ffmpeg`` / ``ffprobe``,自动跳过需要真 ffmpeg 的测试。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _has_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


REAL_FFMPEG_ENABLED = (
    os.environ.get("ASSEMBLY_TEST_REAL_FFMPEG", "0").lower().strip() in ("1", "true", "yes")
    and _has_ffmpeg()
    and _has_ffprobe()
)


@pytest.fixture
def require_real_ffmpeg():
    if not REAL_FFMPEG_ENABLED:
        pytest.skip(
            "set ASSEMBLY_TEST_REAL_FFMPEG=1 (and have ffmpeg + ffprobe on PATH) to run"
        )


@pytest.fixture
def run_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """用临时 cwd 构造一个干净的 RunContext(避免污染 cwd / out 目录)。"""
    monkeypatch.chdir(tmp_path)
    return __import__("assembly_capabilities.run_context", fromlist=["RunContext"]).RunContext()
