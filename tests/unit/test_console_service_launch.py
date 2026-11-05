"""web/console_service.start_run 单元测试(第二期阶段 2)。

全部 mock Popen / PG 预检,无真机依赖。覆盖计划 §11 阶段 2 验收行:
argv 数组 + shell=False + 独立草稿目录 env + PID/日志落盘 + PG 不可达 503 +
stub 未放行 503 + Popen 失败 500 + 重复/超并发 409 + 不等待执行完成。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config  # noqa: E402
import web.console_service as cs  # noqa: E402
from web.console_service import (  # noqa: E402
    ConsoleLaunchError,
    prepare_run,
    start_run,
)


class FakeProc:
    def __init__(self, pid: int = 4321, alive: bool = True):
        self.pid = pid
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def _ok_probe(path) -> dict:
    return {"width": 640, "height": 360, "duration": 30.0}


@pytest.fixture()
def prepared(tmp_path, monkeypatch):
    """准备一个已 prepared 的任务 + 启动门禁放行环境。"""
    monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "stub")
    monkeypatch.setattr(config, "CONSOLE_ALLOW_STUB", True)
    result = prepare_run(
        prompt="剪一条短片", uploads=[_upload("a.mp4", b"abc")],
        target_duration_ms=30000, ratio="9:16", lang="zh",
        outputs_root=tmp_path, probe=_ok_probe,
    )
    yield result
    cs._ACTIVE_PROCS.clear()


class TestStartRunHappyPath:
    def test_launches_run_workflow_subprocess(self, prepared, monkeypatch):
        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return FakeProc(4321)

        record = start_run(
            prepared["task_dir"], popen=fake_popen, pg_check=lambda: None,
            venv_python="py-exe",
        )
        argv = captured["argv"]
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert argv[0] == "py-exe"
        assert "scripts/run_workflow.py" in argv[1]
        assert "--production" in argv and "--skip-preflight" in argv
        assert argv[argv.index("--thread-id") + 1] == record["thread_id"]
        state_flag = argv[argv.index("--initial-state-json") + 1]
        assert Path(state_flag).is_absolute()
        assert argv[argv.index("--llm-backend") + 1] == "stub"
        # 提示词只进 JSON,不进命令行
        assert all("短片" not in str(a) for a in argv)

        kwargs = captured["kwargs"]
        assert kwargs["shell"] is False
        assert Path(kwargs["cwd"]) == Path(cs.__file__).resolve().parents[1]
        env = kwargs["env"]
        assert env["WORKFLOW_ENV"] == "production"
        draft_dir = Path(env["AUTO_VIDEO_EDITOR_DRAFT_DIR"])
        assert draft_dir.name == "drafts"
        assert draft_dir.parent.name == record["thread_id"]
        assert kwargs["stdout"] is not None
        assert kwargs["stderr"] == subprocess.STDOUT  # stderr 并入 stdout → 同一日志

        # 记录与日志落盘
        task_dir = Path(prepared["task_dir"])
        assert (task_dir / "console_run.log").exists()
        stored = json.loads(
            (task_dir / "console_run.json").read_text(encoding="utf-8")
        )
        assert stored["status"] == "starting"
        assert stored["pid"] == 4321
        assert stored["llm_backend"] == "stub"
        assert "started_at" in stored and "log_path" in stored
        assert record["status"] == "starting"

    def test_registered_active_and_api_does_not_wait(self, prepared):
        """启动后立即返回(Popen 不 wait);注册表记录活跃任务。"""
        record = start_run(
            prepared["task_dir"], popen=lambda a, **k: FakeProc(7),
            pg_check=lambda: None,
        )
        assert record["status"] == "starting"
        assert record["thread_id"] in cs._ACTIVE_PROCS


class TestLaunchGates:
    def test_pg_down_503_and_no_popen(self, prepared, monkeypatch):
        called = {"popen": False}

        def fake_popen(argv, **kwargs):
            called["popen"] = True
            return FakeProc()

        def pg_down():
            raise ConsoleLaunchError("PostgreSQL 不可达", status_code=503)

        with pytest.raises(ConsoleLaunchError) as ei:
            start_run(prepared["task_dir"], popen=fake_popen, pg_check=pg_down)
        assert ei.value.status_code == 503
        assert called["popen"] is False  # fail-closed,不伪启动
        stored = json.loads(
            cs.console_run_json_path(Path(prepared["task_dir"])).read_text("utf-8")
        )
        assert stored["status"] == "prepared"  # 记录未被伪改成已启动

    def test_backend_unconfigured_503(self, prepared, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "")
        with pytest.raises(ConsoleLaunchError) as ei:
            start_run(
                prepared["task_dir"], popen=lambda a, **k: FakeProc(),
                pg_check=lambda: None,
            )
        assert ei.value.status_code == 503

    def test_stub_not_allowed_503(self, prepared, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_ALLOW_STUB", False)
        with pytest.raises(ConsoleLaunchError) as ei:
            start_run(
                prepared["task_dir"], popen=lambda a, **k: FakeProc(),
                pg_check=lambda: None,
            )
        assert ei.value.status_code == 503
        assert "CONSOLE_ALLOW_STUB" in str(ei.value)

    def test_unknown_backend_503(self, prepared, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "gpt-4")
        with pytest.raises(ConsoleLaunchError) as ei:
            start_run(
                prepared["task_dir"], popen=lambda a, **k: FakeProc(),
                pg_check=lambda: None,
            )
        assert ei.value.status_code == 503


class TestLaunchFailuresAndConflicts:
    def test_popen_failure_500_and_record_failed(self, prepared):
        def broken_popen(argv, **kwargs):
            raise OSError("spawn failed")

        with pytest.raises(ConsoleLaunchError) as ei:
            start_run(
                prepared["task_dir"], popen=broken_popen, pg_check=lambda: None
            )
        assert ei.value.status_code == 500
        stored = json.loads(
            cs.console_run_json_path(Path(prepared["task_dir"])).read_text("utf-8")
        )
        assert stored["status"] == "failed"
        assert "spawn failed" in stored["error"]

    def test_duplicate_start_409(self, prepared):
        def fake_popen(argv, **kwargs):
            return FakeProc()

        start_run(prepared["task_dir"], popen=fake_popen, pg_check=lambda: None)
        with pytest.raises(ConsoleLaunchError) as ei:
            start_run(prepared["task_dir"], popen=fake_popen, pg_check=lambda: None)
        assert ei.value.status_code == 409

    def test_status_not_prepared_409(self, prepared):
        task_dir = Path(prepared["task_dir"])
        stored = json.loads(cs.console_run_json_path(task_dir).read_text("utf-8"))
        stored["status"] = "starting"
        cs.console_run_json_path(task_dir).write_text(
            json.dumps(stored, ensure_ascii=False), encoding="utf-8"
        )
        with pytest.raises(ConsoleLaunchError) as ei:
            start_run(task_dir, popen=lambda a, **k: FakeProc(), pg_check=lambda: None)
        assert ei.value.status_code == 409

    def test_concurrent_limit_409(self, prepared, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_MAX_CONCURRENT_RUNS", 1)
        cs._ACTIVE_PROCS["console-" + "f" * 12] = FakeProc(999, alive=True)
        with pytest.raises(ConsoleLaunchError) as ei:
            start_run(
                prepared["task_dir"], popen=lambda a, **k: FakeProc(),
                pg_check=lambda: None,
            )
        assert ei.value.status_code == 409

    def test_dead_procs_do_not_count_toward_limit(self, prepared, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_MAX_CONCURRENT_RUNS", 1)
        cs._ACTIVE_PROCS["console-" + "e" * 12] = FakeProc(998, alive=False)
        record = start_run(
            prepared["task_dir"], popen=lambda a, **k: FakeProc(),
            pg_check=lambda: None,
        )
        assert record["status"] == "starting"  # 已退出的进程被清除,不占配额

    def test_missing_task_404(self, tmp_path):
        with pytest.raises(ConsoleLaunchError) as ei:
            start_run(
                tmp_path / ("console-" + "d" * 12),
                popen=lambda a, **k: FakeProc(),
                pg_check=lambda: None,
            )
        assert ei.value.status_code == 404
