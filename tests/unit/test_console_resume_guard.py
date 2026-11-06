"""阶段 3(人工关卡与剪映草稿导入):恢复前文件安全门禁接线测试。

- ``web.gate_state.resume_guard``:取数层包装(assert_resume_ready 接收 state.draft_path);
- ``console_service.resume_run``:门禁不过 → 409 且不启动进程;门禁通过 →
  本次 resume 记录写入人工修改后草稿的 SHA-256(任务 manifest 审计);
- API 级:POST resume 门禁失败 → 409 + 明细文案。

沿用 test_console_resume.py 的 waiting_env 模式,全部假实现,无真机依赖。
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
import web.gate_state as gate_state  # noqa: E402
from monitor_fastapi import app  # noqa: E402
from web.console_service import ConsoleLaunchError, prepare_run, resume_run  # noqa: E402

client = TestClient(app)


class FakeProc:
    def __init__(self, pid: int = 4321):
        self.pid = pid

    def poll(self):
        return None


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def _ok_probe(path) -> dict:
    return {"width": 640, "height": 360, "duration": 30.0}


def _waiting_snapshot(tid):
    return {"available": True, "last_event": {"event_type": "run.interrupted"}}


@pytest.fixture()
def waiting_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
    monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "stub")
    monkeypatch.setattr(config, "CONSOLE_ALLOW_STUB", True)
    monkeypatch.setattr(cs, "_latest_snapshot", _waiting_snapshot)
    monkeypatch.setattr(cs, "_pid_alive", lambda pid: None)
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


class TestGateStateResumeGuard:
    def test_wraps_assert_resume_ready_with_state_draft_path(
        self, monkeypatch
    ):
        seen = {}

        def fake_assert(draft_file, *, proc_checker=None, require_jianying_closed=None):
            seen["draft_file"] = draft_file
            seen["closed"] = require_jianying_closed
            return {"ok": True, "problems": [], "hashes": {"draft_content.json": "abc"}, "jianying_running": False}

        monkeypatch.setattr(gate_state, "fetch_state_values",
                            lambda tid: {"draft_path": "X:/d/draft_content.json"})
        monkeypatch.setattr("draft_ops.draft_lock.assert_resume_ready", fake_assert)
        out = gate_state.resume_guard("console-ab12cd34ef56")
        assert seen["draft_file"] == "X:/d/draft_content.json"
        assert out == {"ok": True, "problems": [], "hashes": {"draft_content.json": "abc"}}

    def test_problems_propagate(self, monkeypatch):
        monkeypatch.setattr(gate_state, "fetch_state_values", lambda tid: {"draft_path": None})
        monkeypatch.setattr(
            "draft_ops.draft_lock.assert_resume_ready",
            lambda draft_file, **k: {"ok": False, "problems": ["state.draft_path 缺失"], "hashes": {}, "jianying_running": False},
        )
        out = gate_state.resume_guard("console-ab12cd34ef56")
        assert out["ok"] is False
        assert out["problems"] == ["state.draft_path 缺失"]


class TestConsoleResumeGuard:
    def test_guard_fail_blocks_popen_409(self, waiting_env):
        result, tmp_path = waiting_env

        def bad_guard(tid):
            return {"ok": False, "problems": ["检测到剪映客户端正在运行:请先关闭"], "hashes": {}}

        popen_calls = []
        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(
                result["thread_id"],
                popen=lambda a, **k: popen_calls.append(a) or FakeProc(),
                pg_check=lambda: None,
                draft_guard=bad_guard,
            )
        assert ei.value.status_code == 409
        assert "剪映" in str(ei.value)
        assert popen_calls == [], "门禁不过绝不能启动恢复进程"

    def test_guard_pass_writes_sha256_to_manifest(self, waiting_env):
        result, tmp_path = waiting_env
        hashes = {"draft_content.json": "a" * 64, "draft_info.json": "b" * 64}
        popen_calls = []

        def ok_guard(tid):
            return {"ok": True, "problems": [], "hashes": hashes}

        record = resume_run(
            result["thread_id"],
            popen=lambda a, **k: popen_calls.append(a) or FakeProc(),
            pg_check=lambda: None,
            draft_guard=ok_guard,
        )
        assert len(popen_calls) == 1
        entry = record["resumes"][-1]
        assert entry["draft_sha256"] == hashes, "审计哈希必须落进本次 resume 记录"
        # manifest 落盘
        on_disk = json.loads(
            cs.console_run_json_path(result["task_dir"]).read_text(encoding="utf-8")
        )
        assert on_disk["resumes"][-1]["draft_sha256"] == hashes


class TestApiLevelGuard:
    def test_resume_409_detail_on_guard_fail(self, waiting_env, monkeypatch):
        result, _tmp = waiting_env
        monkeypatch.setattr(
            cs, "_resume_draft_guard",
            lambda tid: {"ok": False, "problems": ["草稿文件不存在(可能被删除/移动): X"], "hashes": {}},
        )
        resp = client.post(
            f"/api/console/runs/{result['thread_id']}/resume", json={"value": True}
        )
        assert resp.status_code == 409
        assert "恢复前检查未通过" in resp.json()["detail"]
