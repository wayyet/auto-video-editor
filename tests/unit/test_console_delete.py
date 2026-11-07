"""web/console_service.delete_run / 历史任务删除 单元测试(2026-11-07)。

全部注入假实现(Popen / 事件删除),无真机、无 Postgres 依赖。
覆盖:仅终态 + prepared 可删(running/waiting_human/unknown 409 拒删)、
注册表双保险、事件删除降级不阻断目录删除、目录删除失败 500(占用)、
API 级 CSRF / 形状 / 404 门禁。
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
import monitoring.workflow_event_store as store  # noqa: E402
import web.console_service as cs  # noqa: E402
from monitor_fastapi import app  # noqa: E402
from web.console_service import (  # noqa: E402
    ConsoleLaunchError,
    delete_run,
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
def prepared(tmp_path, monkeypatch):
    """一个已 prepare 但未启动的任务(record status=prepared)。"""
    monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
    monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "stub")
    monkeypatch.setattr(config, "CONSOLE_ALLOW_STUB", True)
    result = prepare_run(
        prompt="p", uploads=[_upload("a.mp4", b"x")],
        target_duration_ms=30000, ratio="9:16", lang="zh",
        outputs_root=tmp_path, probe=_ok_probe,
    )
    yield result, tmp_path
    cs._ACTIVE_PROCS.clear()
    cs._STARTED_AT.clear()


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


def _mark_status(task_dir, status):
    record = cs._mark_record(Path(task_dir), {"status": status})
    assert record, "测试前置:_mark_record 应成功"


class TestDeleteRun:
    def test_prepared_deletable(self, prepared, monkeypatch):
        result, root = prepared
        monkeypatch.setattr(store, "delete_thread_events", lambda tid: 0)
        out = delete_run(result["thread_id"])
        assert out == {
            "thread_id": result["thread_id"], "state": "prepared", "events_deleted": 0,
        }
        assert not Path(result["task_dir"]).exists()

    def test_terminal_record_deletable_and_unlisted(self, prepared, monkeypatch):
        result, root = prepared
        _mark_status(result["task_dir"], "cancelled")
        # 兄弟任务目录与无关文件预先存在,删除后必须原样保留
        sibling = root / "console-ffffffffffff"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("keep", encoding="utf-8")
        (root / "not-a-task.txt").write_text("keep", encoding="utf-8")
        monkeypatch.setattr(store, "delete_thread_events", lambda tid: 3)
        out = delete_run(result["thread_id"])
        assert out["state"] == "cancelled" and out["events_deleted"] == 3
        assert not Path(result["task_dir"]).exists()
        # 历史列表不再列出;兄弟目录/文件不受影响
        assert all(
            r["thread_id"] != result["thread_id"]
            for r in cs.list_console_runs(outputs_root=root)
        )
        assert sibling.exists() and (root / "not-a-task.txt").exists()

    def test_failed_record_deletable(self, prepared, monkeypatch):
        result, root = prepared
        _mark_status(result["task_dir"], "failed")
        monkeypatch.setattr(store, "delete_thread_events", lambda tid: 1)
        out = delete_run(result["thread_id"])
        assert out["state"] == "failed"
        assert not Path(result["task_dir"]).exists()

    def test_completed_via_snapshot_deletable(self, prepared, monkeypatch):
        """record 停在 starting,但事件库派生 completed(事实来源)→ 可删。"""
        result, root = prepared
        _mark_status(result["task_dir"], "starting")
        monkeypatch.setattr(
            cs, "_latest_snapshot",
            lambda tid: {"available": True, "last_event": {"event_type": "run.completed"}},
        )
        monkeypatch.setattr(store, "delete_thread_events", lambda tid: 9)
        out = delete_run(result["thread_id"])
        assert out["state"] == "completed" and out["events_deleted"] == 9
        assert not Path(result["task_dir"]).exists()

    def test_missing_task_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
        with pytest.raises(ConsoleLaunchError) as ei:
            delete_run("console-abcdef123456")
        assert ei.value.status_code == 404

    def test_bad_thread_id_400(self):
        with pytest.raises(Exception) as ei:
            delete_run("../escape")
        assert "thread_id" in str(ei.value)

    def test_running_409_dir_intact(self, started):
        """活跃进程在册:拒删(fixture 无事件,调和态为 starting=启动中)。"""
        result, root = started
        with pytest.raises(ConsoleLaunchError) as ei:
            delete_run(result["thread_id"])
        assert ei.value.status_code == 409
        assert "启动中" in str(ei.value)
        assert Path(result["task_dir"]).exists()

    def test_registered_proc_beats_reconcile_409(self, started, monkeypatch):
        """调和态是终态、但注册表仍有活跃进程(并发恢复竞态)→ 仍拒删。"""
        result, root = started
        _mark_status(result["task_dir"], "cancelled")
        with pytest.raises(ConsoleLaunchError) as ei:
            delete_run(result["thread_id"])
        assert ei.value.status_code == 409
        assert Path(result["task_dir"]).exists()

    def test_waiting_human_409(self, prepared, monkeypatch):
        result, root = prepared
        _mark_status(result["task_dir"], "starting")
        monkeypatch.setattr(
            cs, "_latest_snapshot",
            lambda tid: {"available": True, "last_event": {"event_type": "node.waiting_input"}},
        )
        with pytest.raises(ConsoleLaunchError) as ei:
            delete_run(result["thread_id"])
        assert ei.value.status_code == 409
        assert "人工关卡" in str(ei.value)
        assert Path(result["task_dir"]).exists()

    def test_unknown_409_fail_closed(self, prepared, monkeypatch):
        """事件库不可用 + 进程不在册且 PID 探活失败 → unknown,拒删。"""
        result, root = prepared
        _mark_status(result["task_dir"], "starting")
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: {"available": False})
        monkeypatch.setattr(cs, "_pid_alive", lambda pid: False)
        with pytest.raises(ConsoleLaunchError) as ei:
            delete_run(result["thread_id"])
        assert ei.value.status_code == 409
        assert Path(result["task_dir"]).exists()

    def test_events_degrade_does_not_block_dir_delete(self, prepared, monkeypatch):
        result, root = prepared
        monkeypatch.setattr(store, "delete_thread_events", lambda tid: None)
        out = delete_run(result["thread_id"])
        assert out["events_deleted"] is None
        assert not Path(result["task_dir"]).exists()

    def test_events_import_failure_still_deletes_dir(self, prepared, monkeypatch):
        def boom(tid):
            raise RuntimeError("pg down (mock)")

        monkeypatch.setattr(store, "delete_thread_events", boom)
        result, root = prepared
        out = delete_run(result["thread_id"])
        assert out["events_deleted"] is None
        assert not Path(result["task_dir"]).exists()

    def test_rmtree_oserror_500(self, prepared, monkeypatch):
        result, root = prepared
        monkeypatch.setattr(store, "delete_thread_events", lambda tid: 0)

        def locked(path, *a, **k):
            raise OSError("being used by another process (mock)")

        monkeypatch.setattr("shutil.rmtree", locked)
        with pytest.raises(ConsoleLaunchError) as ei:
            delete_run(result["thread_id"])
        assert ei.value.status_code == 500
        assert "占用" in str(ei.value)
        assert Path(result["task_dir"]).exists()


class TestDeleteApi:
    def test_api_delete_200_with_csrf(self, prepared, monkeypatch):
        result, root = prepared
        monkeypatch.setattr(store, "delete_thread_events", lambda tid: 0)
        token = client.get("/api/console/csrf").json()["token"]
        resp = client.post(
            f"/api/console/runs/{result['thread_id']}/delete",
            headers={"Origin": "http://127.0.0.1:7860", "X-CSRF-Token": token},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["state"] == "prepared"
        assert body["events_deleted"] == 0
        assert not Path(result["task_dir"]).exists()

    def test_api_delete_without_csrf_403(self, prepared):
        result, root = prepared
        resp = client.post(
            f"/api/console/runs/{result['thread_id']}/delete",
            headers={"Origin": "http://127.0.0.1:7860"},
        )
        assert resp.status_code == 403
        assert Path(result["task_dir"]).exists()

    def test_api_delete_bad_thread_id_400(self):
        resp = client.post("/api/console/runs/not-a-task/delete")
        assert resp.status_code == 400

    def test_api_delete_missing_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
        resp = client.post("/api/console/runs/console-abcdef123456/delete")
        assert resp.status_code == 404

    def test_api_delete_running_409(self, started):
        result, root = started
        resp = client.post(f"/api/console/runs/{result['thread_id']}/delete")
        assert resp.status_code == 409
        assert Path(result["task_dir"]).exists()
