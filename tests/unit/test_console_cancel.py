"""web/console_service.cancel_run / 运行超时收割 单元测试(第二期阶段 5)。

全部注入假实现(Popen / 终止函数),无真机依赖。
覆盖:取消仅限本实例在册进程(重启后 409 拒绝盲杀)、终止函数注入、
record 落盘(cancelled)、超时收割(树杀 + failed 记录 + 注册表清理)、
超时默认关闭、API 级 CSRF 门禁。
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config  # noqa: E402
import web.api_console as api_console  # noqa: E402
import web.console_service as cs  # noqa: E402
from monitor_fastapi import app  # noqa: E402
from web.console_service import (  # noqa: E402
    ConsoleLaunchError,
    cancel_run,
    prepare_run,
    start_run,
)

client = TestClient(app)


class FakeProc:
    def __init__(self, pid: int = 4321, alive: bool = True):
        self.pid = pid
        self._alive = alive

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def _ok_probe(path) -> dict:
    return {"width": 640, "height": 360, "duration": 30.0}


@pytest.fixture()
def started(tmp_path, monkeypatch):
    """一个已启动(假子进程存活)的任务。"""
    monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
    monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "stub")
    monkeypatch.setattr(config, "CONSOLE_ALLOW_STUB", True)
    result = prepare_run(
        prompt="p", uploads=[_upload("a.mp4", b"x")],
        target_duration_ms=30000, ratio="9:16", lang="zh",
        outputs_root=tmp_path, probe=_ok_probe,
    )
    start_run(
        result["task_dir"], popen=lambda a, **k: FakeProc(4321),
        pg_check=lambda: None, venv_python="py-exe",
    )
    yield result, tmp_path
    cs._ACTIVE_PROCS.clear()
    cs._STARTED_AT.clear()


def _record_of(task_dir):
    return json.loads(cs.console_run_json_path(task_dir).read_text("utf-8"))


class TestCancelRun:
    def test_cancel_marks_record_and_unregisters(self, started, monkeypatch):
        result, root = started
        killed: list[int] = []
        monkeypatch.setattr(cs, "_terminate_process_tree", lambda p: killed.append(p.pid))
        record = cancel_run(result["thread_id"])
        assert killed == [4321]
        assert record["status"] == "cancelled"
        assert record["cancelled_at"]
        assert result["thread_id"] not in cs._ACTIVE_PROCS
        assert result["thread_id"] not in cs._STARTED_AT
        assert _record_of(result["task_dir"])["status"] == "cancelled"

    def test_unknown_task_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
        with pytest.raises(ConsoleLaunchError) as ei:
            cancel_run("console-abcdef123456")
        assert ei.value.status_code == 404

    def test_not_registered_409_no_pid_kill(self, started):
        """重启后注册表为空:拒绝按记录 PID 盲杀(PID 复用风险)。"""
        result, root = started
        cs._ACTIVE_PROCS.clear()
        cs._STARTED_AT.clear()
        with pytest.raises(ConsoleLaunchError) as ei:
            cancel_run(result["thread_id"])
        assert ei.value.status_code == 409
        assert "误杀" in str(ei.value)
        # 记录不被改成 cancelled(运行可能还在别处活着)
        assert _record_of(result["task_dir"])["status"] == "starting"

    def test_dead_proc_in_registry_409(self, started):
        result, root = started
        cs._ACTIVE_PROCS[result["thread_id"]] = FakeProc(4321, alive=False)
        with pytest.raises(ConsoleLaunchError) as ei:
            cancel_run(result["thread_id"])
        assert ei.value.status_code == 409


class TestTimeoutReaper:
    def test_config_default_disabled(self):
        assert float(config.CONSOLE_RUN_TIMEOUT_SEC) == 0.0

    def test_timeout_kills_and_marks_failed(self, started, monkeypatch):
        result, root = started
        monkeypatch.setattr(config, "CONSOLE_RUN_TIMEOUT_SEC", 50.0)
        killed: list[int] = []
        monkeypatch.setattr(
            cs, "_terminate_process_tree",
            lambda p: (killed.append(p.pid), setattr(p, "_alive", False)),
        )
        # 启动时刻拨回 100s 前(> 50s 超时)
        cs._STARTED_AT[result["thread_id"]] = time.monotonic() - 100.0

        cs.reap_now()

        assert killed == [4321]
        assert result["thread_id"] not in cs._ACTIVE_PROCS
        record = _record_of(result["task_dir"])
        assert record["status"] == "failed"
        assert "超时" in record["error"]

    def test_no_timeout_when_disabled(self, started, monkeypatch):
        result, root = started
        monkeypatch.setattr(config, "CONSOLE_RUN_TIMEOUT_SEC", 0.0)
        killed: list[int] = []
        monkeypatch.setattr(cs, "_terminate_process_tree", lambda p: killed.append(p.pid))
        cs._STARTED_AT[result["thread_id"]] = time.monotonic() - 10_000.0

        cs.reap_now()

        assert killed == []
        assert result["thread_id"] in cs._ACTIVE_PROCS  # 仍存活,不受影响
        assert _record_of(result["task_dir"])["status"] == "starting"

    def test_within_timeout_not_killed(self, started, monkeypatch):
        result, root = started
        monkeypatch.setattr(config, "CONSOLE_RUN_TIMEOUT_SEC", 3600.0)
        killed: list[int] = []
        monkeypatch.setattr(cs, "_terminate_process_tree", lambda p: killed.append(p.pid))

        cs.reap_now()

        assert killed == []
        assert result["thread_id"] in cs._ACTIVE_PROCS


class TestCancelApi:
    def test_api_cancel_200_with_csrf(self, started, monkeypatch):
        result, root = started
        monkeypatch.setattr(cs, "_terminate_process_tree", lambda p: None)
        token = client.get("/api/console/csrf").json()["token"]
        resp = client.post(
            f"/api/console/runs/{result['thread_id']}/cancel",
            headers={"Origin": "http://127.0.0.1:7860", "X-CSRF-Token": token},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True and body["state"] == "cancelled"

    def test_api_cancel_without_csrf_403(self, started):
        result, root = started
        resp = client.post(
            f"/api/console/runs/{result['thread_id']}/cancel",
            headers={"Origin": "http://127.0.0.1:7860"},
        )
        assert resp.status_code == 403

    def test_api_cancel_not_registered_409(self, started):
        result, root = started
        cs._ACTIVE_PROCS.clear()
        cs._STARTED_AT.clear()
        resp = client.post(f"/api/console/runs/{result['thread_id']}/cancel")
        assert resp.status_code == 409
