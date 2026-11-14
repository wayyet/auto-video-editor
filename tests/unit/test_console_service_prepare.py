"""web/console_service.py prepare_run 单元测试(第二期阶段 1)。

覆盖计划 §11 阶段 1 验收行:
- 上传成功 → console_run.json(status=prepared)+ initial_state.json + drafts 目录;
- 任意失败 → 任务目录整体回滚,"不生成可运行任务"。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from web.console_service import (  # noqa: E402
    console_run_json_path,
    prepare_run,
)
from web.console_uploads import ConsoleUploadError  # noqa: E402


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def _ok_probe(path: Path) -> dict:
    return {"width": 1280, "height": 720, "duration": 30.0}


class TestPrepareRunHappyPath:
    def test_single_video_prepared(self, tmp_path):
        result = prepare_run(
            prompt="剪一条 30 秒旅行回顾",
            uploads=[_upload("a.mp4", b"abc")],
            target_duration_ms=30000, ratio="9:16", lang="zh",
            outputs_root=tmp_path, probe=_ok_probe,
        )
        tid = result["thread_id"]
        task_dir = tmp_path / tid
        assert task_dir.is_dir()
        assert (task_dir / "inputs" / "0001.mp4").read_bytes() == b"abc"
        assert (task_dir / "drafts").is_dir()
        assert (task_dir / "initial_state.json").exists()

        record = json.loads(console_run_json_path(task_dir).read_text(encoding="utf-8"))
        assert record["thread_id"] == tid
        assert record["status"] == "prepared"
        assert record["files"][0]["original_name"] == "a.mp4"
        assert record["initial_state_path"] == str(
            (task_dir / "initial_state.json").resolve()
        )
        assert result["initial_state"]["session_id"] == tid
        assert "created_at" in record

    def test_multi_video_prepared(self, tmp_path):
        result = prepare_run(
            prompt="p", uploads=[_upload("a.mp4", b"1"), _upload("b.mov", b"22")],
            target_duration_ms=35000, ratio="16:9", lang="zh",
            outputs_root=tmp_path, probe=_ok_probe,
        )
        task_dir = tmp_path / result["thread_id"]
        assert (task_dir / "inputs" / "0001.mp4").exists()
        assert (task_dir / "inputs" / "0002.mov").exists()
        assert len(result["record"]["files"]) == 2

    def test_speed_multiplier_passthrough(self, tmp_path):
        """2026-11-14 快进速度:透传进 storyline_targets(API 层已校验范围)。"""
        result = prepare_run(
            prompt="p", uploads=[_upload("a.mp4", b"1")],
            target_duration_ms=35000, ratio="9:16", lang="zh",
            outputs_root=tmp_path, probe=_ok_probe, speed_multiplier=2.0,
        )
        targets = result["initial_state"]["storyline_targets"]
        assert targets["speed_multiplier"] == 2.0


class TestPrepareRunRollback:
    def test_upload_error_rolls_back_task_dir(self, tmp_path):
        with pytest.raises(ConsoleUploadError):
            prepare_run(
                prompt="p", uploads=[_upload("evil.txt", b"x")],
                target_duration_ms=35000, ratio="9:16", lang="zh",
                outputs_root=tmp_path, probe=_ok_probe,
            )
        assert list(tmp_path.iterdir()) == []  # 无孤儿任务目录

    def test_probe_error_rolls_back_task_dir(self, tmp_path):
        def _bad_probe(path: Path) -> dict:
            raise ConsoleUploadError("损坏", status_code=415)

        with pytest.raises(ConsoleUploadError):
            prepare_run(
                prompt="p", uploads=[_upload("a.mp4", b"x")],
                target_duration_ms=35000, ratio="9:16", lang="zh",
                outputs_root=tmp_path, probe=_bad_probe,
            )
        assert list(tmp_path.iterdir()) == []
