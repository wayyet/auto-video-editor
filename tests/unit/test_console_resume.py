"""web/console_service.resume_run 单元测试(第二期阶段 5)。

全部注入假实现(事件库快照 / Popen / PG 预检),无真机依赖。
覆盖:waiting_human 门禁(非等待态 409)、argv 形状(resume_workflow.py +
--value JSON + --checkpoint 可选 + --llm-backend 与 start_run 同门禁)、
并发额度 409、PG 不可达 503、Popen 失败 500 + 记录落盘、API 级 CSRF 门禁。
"""

from __future__ import annotations

import io
import json
import sys
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
    prepare_run,
    resume_run,
)

client = TestClient(app)


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


def _waiting_snapshot(tid):
    return {"available": True, "last_event": {"event_type": "run.interrupted"}}


@pytest.fixture()
def waiting_env(tmp_path, monkeypatch):
    """已启动(starting)任务 + 事件库调和为 waiting_human + 启动门禁放行。

    恢复的前置状态 = "运行曾启动、已停在人工关卡",因此 record 须为
    starting(prepared 意味着从未启动,恢复门禁会直接 409)。
    """
    monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
    monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "stub")
    monkeypatch.setattr(config, "CONSOLE_ALLOW_STUB", True)
    monkeypatch.setattr(cs, "_latest_snapshot", _waiting_snapshot)
    monkeypatch.setattr(cs, "_pid_alive", lambda pid: None)
    # 阶段 3 恢复门禁:本文件聚焦启动/Popen 语义,门禁统一注入放行
    # (门禁本体行为见 test_console_resume_guard.py / test_draft_lock.py)
    monkeypatch.setattr(
        cs, "_resume_draft_guard",
        lambda tid: {"ok": True, "problems": [], "hashes": {}},
    )
    result = prepare_run(
        prompt="剪一条短片", uploads=[_upload("a.mp4", b"abc")],
        target_duration_ms=30000, ratio="9:16", lang="zh",
        outputs_root=tmp_path, probe=_ok_probe,
    )
    record_path = cs.console_run_json_path(result["task_dir"])
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["status"] = "starting"
    record["pid"] = 4321
    cs._write_run_json(result["task_dir"], record)
    yield result, tmp_path
    cs._ACTIVE_PROCS.clear()
    cs._STARTED_AT.clear()
    api_console._CSRF_TOKENS.clear()


class TestResumeRunService:
    def test_happy_path_argv_and_registration(self, waiting_env, monkeypatch):
        result, root = waiting_env
        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return FakeProc(4321)

        record = resume_run(
            result["thread_id"], popen=fake_popen, pg_check=lambda: None,
            venv_python="py-exe",
        )
        argv = captured["argv"]
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert argv[0] == "py-exe"
        assert "scripts/resume_workflow.py" in argv[1]
        assert "--production" in argv and "--skip-preflight" in argv
        assert argv[argv.index("--thread-id") + 1] == result["thread_id"]
        assert argv[argv.index("--value") + 1] == "true"  # 默认放行值 JSON 序列化
        assert argv[argv.index("--llm-backend") + 1] == "stub"  # 与 start_run 同门禁
        assert "--checkpoint" not in argv  # 省略 = 单挂起关卡自动匹配
        assert captured["kwargs"]["shell"] is False
        assert captured["kwargs"]["env"]["WORKFLOW_ENV"] == "production"

        assert result["thread_id"] in cs._ACTIVE_PROCS
        assert result["thread_id"] in cs._STARTED_AT
        # 阶段 3:resume 记录带 draft_sha256 审计字段(门禁注入放行时为空 dict)
        assert record["resumes"] == [{"at": record["resumes"][0]["at"],
                                      "checkpoint": None, "value": True,
                                      "draft_sha256": {}}]
        assert record["status"] == "starting"

    def test_with_checkpoint_flag(self, waiting_env, monkeypatch):
        result, root = waiting_env
        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return FakeProc(4321)

        resume_run(result["thread_id"], checkpoint="①", value={"answer": "ok"},
                   popen=fake_popen, pg_check=lambda: None, venv_python="py-exe")
        argv = captured["argv"]
        assert argv[argv.index("--checkpoint") + 1] == "①"
        assert argv[argv.index("--value") + 1] == '{"answer": "ok"}'

    def test_unknown_task_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run("console-abcdef123456", popen=lambda a, **k: FakeProc(),
                       pg_check=lambda: None)
        assert ei.value.status_code == 404

    def test_not_waiting_human_409(self, tmp_path, monkeypatch):
        """事件库无结论(prepared 任务,无事件)→ 409,绝不盲目恢复。"""
        monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
        monkeypatch.setattr(cs, "_latest_snapshot",
                            lambda tid: {"available": False})
        result = prepare_run(
            prompt="p", uploads=[_upload("a.mp4", b"x")],
            target_duration_ms=30000, ratio="9:16", lang="zh",
            outputs_root=tmp_path, probe=_ok_probe,
        )
        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(),
                       pg_check=lambda: None)
        assert ei.value.status_code == 409
        assert "未处于人工等待" in str(ei.value)

    def test_running_state_409(self, waiting_env, monkeypatch):
        result, root = waiting_env
        monkeypatch.setattr(
            cs, "_latest_snapshot",
            lambda tid: {"available": True,
                         "last_event": {"event_type": "node.started"}},
        )
        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(),
                       pg_check=lambda: None)
        assert ei.value.status_code == 409

    def test_pg_down_503(self, waiting_env, monkeypatch):
        result, root = waiting_env

        def pg_down():
            raise ConsoleLaunchError("PostgreSQL 不可达", status_code=503)

        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(),
                       pg_check=pg_down)
        assert ei.value.status_code == 503

    def test_backend_gate_503(self, waiting_env, monkeypatch):
        result, root = waiting_env
        monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "")
        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(),
                       pg_check=lambda: None)
        assert ei.value.status_code == 503

    def test_concurrency_limit_409(self, waiting_env, monkeypatch):
        result, root = waiting_env
        monkeypatch.setattr(config, "CONSOLE_MAX_CONCURRENT_RUNS", 1)
        cs._ACTIVE_PROCS["console-ffffffffffff"] = FakeProc(1111)
        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(),
                       pg_check=lambda: None)
        assert ei.value.status_code == 409

    def test_same_task_active_409(self, waiting_env, monkeypatch):
        result, root = waiting_env
        cs._ACTIVE_PROCS[result["thread_id"]] = FakeProc(1111)
        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(),
                       pg_check=lambda: None)
        assert ei.value.status_code == 409

    def test_popen_failure_500_and_record(self, waiting_env, monkeypatch):
        result, root = waiting_env

        def boom(argv, **kwargs):
            raise OSError("spawn failed")

        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(result["thread_id"], popen=boom, pg_check=lambda: None,
                       venv_python="py-exe")
        assert ei.value.status_code == 500
        record = json.loads(
            cs.console_run_json_path(result["task_dir"]).read_text("utf-8")
        )
        assert record["status"] == "failed"
        assert "恢复进程启动失败" in record["error"]


class TestResumeApi:
    def test_api_happy_path_202_with_csrf(self, waiting_env, monkeypatch):
        result, root = waiting_env
        monkeypatch.setattr(cs, "_default_popen", lambda: (lambda a, **k: FakeProc(4321)))
        monkeypatch.setattr(cs, "_check_postgres", lambda: None)
        token = client.get("/api/console/csrf").json()["token"]
        resp = client.post(
            f"/api/console/runs/{result['thread_id']}/resume",
            json={"checkpoint": "①", "value": True},
            headers={"Origin": "http://127.0.0.1:7860", "X-CSRF-Token": token},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["ok"] is True and body["state"] == "resuming"
        assert body["events_url"] == f"/api/workflow/runs/{result['thread_id']}/events"

    def test_api_without_csrf_403(self, waiting_env, monkeypatch):
        result, root = waiting_env
        monkeypatch.setattr(cs, "_default_popen", lambda: (lambda a, **k: FakeProc(4321)))
        monkeypatch.setattr(cs, "_check_postgres", lambda: None)
        resp = client.post(
            f"/api/console/runs/{result['thread_id']}/resume",
            json={"value": True},
            headers={"Origin": "http://127.0.0.1:7860"},
        )
        assert resp.status_code == 403

    def test_api_no_origin_exempt(self, waiting_env, monkeypatch):
        """无 Origin 的本机脚本豁免 CSRF(与创建接口同一纪律)。"""
        result, root = waiting_env
        monkeypatch.setattr(cs, "_default_popen", lambda: (lambda a, **k: FakeProc(4321)))
        monkeypatch.setattr(cs, "_check_postgres", lambda: None)
        resp = client.post(
            f"/api/console/runs/{result['thread_id']}/resume", json={"value": True}
        )
        assert resp.status_code == 202
