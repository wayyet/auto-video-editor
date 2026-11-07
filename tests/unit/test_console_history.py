"""web/console_service 历史列表 / 状态调和 / 详情 单元测试(第二期阶段 5)。

全部注入假实现(事件库快照 / PID 探活 / Popen),无真机依赖。
覆盖:扫描过滤(非 console 目录/损坏记录跳过)、排序与 limit、
reconcile 词表(prepared/failed/cancelled/running/waiting_human/completed/
starting/unknown)、详情 404、服务重启后(注册表清空)的恢复视图。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config  # noqa: E402
import web.console_service as cs  # noqa: E402
from web.console_service import (  # noqa: E402
    ConsoleLaunchError,
    get_console_run,
    list_console_runs,
    prepare_run,
    reconcile_run_state,
    start_run,
)
from web.console_uploads import ConsoleUploadError  # noqa: E402


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
    result = prepare_run(
        prompt="剪一条短片", uploads=[_upload("a.mp4", b"abc")],
        target_duration_ms=30000, ratio="9:16", lang="zh",
        outputs_root=tmp_path, probe=_ok_probe,
    )
    yield result, tmp_path
    cs._ACTIVE_PROCS.clear()
    cs._STARTED_AT.clear()


def _snap(available=True, last_event=None):
    return {"available": available, "last_event": last_event}


class TestListConsoleRuns:
    def test_empty_root_returns_empty(self, tmp_path):
        assert list_console_runs(outputs_root=tmp_path) == []

    def test_missing_root_returns_empty(self, tmp_path):
        assert list_console_runs(outputs_root=tmp_path / "nope") == []

    def test_ignores_non_console_dirs_and_files(self, prepared):
        _, root = prepared
        (root / "other-task").mkdir()
        (root / "console-bad").mkdir()  # 不匹配 12hex 正则
        (root / "console-abcdef123456").mkdir()  # 匹配但无 console_run.json
        (root / "readme.txt").write_text("x", encoding="utf-8")
        runs = list_console_runs(outputs_root=root)
        assert [r["thread_id"] for r in runs] == [prepared[0]["thread_id"]]

    def test_skips_corrupt_record(self, prepared):
        result, root = prepared
        record_path = cs.console_run_json_path(result["task_dir"])
        record_path.write_text("{broken json", encoding="utf-8")
        assert list_console_runs(outputs_root=root) == []

    def test_sorted_by_created_at_desc_and_limit(self, prepared):
        _, root = prepared
        # 另造两个任务目录,created_at 用未来日期保证确定性排序(新 → 旧)
        for tid, created in [
            ("console-000000000001", "2026-12-02T00:00:00+00:00"),
            ("console-000000000002", "2026-12-01T00:00:00+00:00"),
        ]:
            d = root / tid
            d.mkdir()
            (d / "console_run.json").write_text(
                json.dumps({"thread_id": tid, "created_at": created,
                            "status": "prepared", "files": []}, ensure_ascii=False),
                encoding="utf-8",
            )
        runs = list_console_runs(outputs_root=root, limit=10)
        assert [r["thread_id"] for r in runs[:2]] == [
            "console-000000000001", "console-000000000002",
        ]
        assert len(list_console_runs(outputs_root=root, limit=2)) == 2


class TestReconcileRunState:
    def test_terminal_record_statuses_pass_through(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: _snap(available=False))
        for status in ("prepared", "failed", "cancelled"):
            assert reconcile_run_state({"status": status}, "console-abcdef123456") == status

    def test_starting_with_event_store_states(self, monkeypatch):
        # 与 api_workflow._LAST_EVENT_STATE / 前端 LAST_EVENT_RUN_STATE 同一套契约:
        # 注意 run.started 不在映射表内(→ idle → 落到进程探活),不进本用例
        for event_type, expected in [
            ("node.started", "running"),
            ("node.resumed", "running"),
            ("node.retrying", "running"),
            ("node.progress", "running"),
            ("run.interrupted", "waiting_human"),
            ("node.waiting_input", "waiting_human"),
            ("run.completed", "completed"),
            ("run.failed", "failed"),
        ]:
            monkeypatch.setattr(
                cs, "_latest_snapshot",
                lambda tid, et=event_type: _snap(True, {"event_type": et}),
            )
            record = {"status": "starting", "pid": 1}
            assert reconcile_run_state(record, "console-abcdef123456") == expected

    def test_starting_no_events_alive_registered_proc(self, prepared, monkeypatch):
        result, root = prepared
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: _snap(False))
        record = json.loads(
            cs.console_run_json_path(result["task_dir"]).read_text("utf-8")
        )
        record["status"] = "starting"
        record["pid"] = 4321
        cs._ACTIVE_PROCS[result["thread_id"]] = FakeProc(4321, alive=True)
        assert reconcile_run_state(record, result["thread_id"]) == "starting"

    def test_starting_no_events_dead_pid_unknown(self, prepared, monkeypatch):
        result, root = prepared
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: _snap(False))
        monkeypatch.setattr(cs, "_pid_alive", lambda pid: False)
        record = {"status": "starting", "pid": 4321}
        assert reconcile_run_state(record, result["thread_id"]) == "unknown"

    def test_starting_store_unavailable_pid_undetermined_unknown(self, monkeypatch):
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: _snap(False))
        monkeypatch.setattr(cs, "_pid_alive", lambda pid: None)
        assert reconcile_run_state({"status": "starting", "pid": None},
                                   "console-abcdef123456") == "unknown"

    def test_starting_no_events_alive_stale_pid_starting(self, monkeypatch):
        """重启后 PID 复用启发:pid 活着 → starting(事件库无结论时的诚实降级)。"""
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: _snap(False))
        monkeypatch.setattr(cs, "_pid_alive", lambda pid: True)
        assert reconcile_run_state({"status": "starting", "pid": 99},
                                   "console-abcdef123456") == "starting"


class TestGetConsoleRun:
    def test_detail_404_for_unknown(self, tmp_path):
        with pytest.raises(ConsoleLaunchError) as ei:
            get_console_run("console-abcdef123456", outputs_root=tmp_path)
        assert ei.value.status_code == 404

    def test_detail_returns_record_and_state(self, prepared, monkeypatch):
        result, root = prepared
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: _snap(False))
        monkeypatch.setattr(cs, "_pid_alive", lambda pid: False)
        data = get_console_run(result["thread_id"], outputs_root=root)
        assert data["record"]["thread_id"] == result["thread_id"]
        assert data["state"] == "prepared"
        assert data["task_dir"] == str(result["task_dir"])

    def test_detail_includes_initial_state_targets(self, prepared, monkeypatch):
        """详情携带 initial_state(创建台查看模式回填表单的数据源)。"""
        result, root = prepared
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: _snap(False))
        monkeypatch.setattr(cs, "_pid_alive", lambda pid: False)
        data = get_console_run(result["thread_id"], outputs_root=root)
        targets = data["initial_state"]["storyline_targets"]
        assert targets["user_request"] == "剪一条短片"
        assert targets["target_duration_ms"] == 30000
        assert targets["ratio"] == "9:16"
        assert targets["lang"] == "zh"

    def test_detail_initial_state_none_when_missing(self, prepared, monkeypatch):
        result, root = prepared
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: _snap(False))
        monkeypatch.setattr(cs, "_pid_alive", lambda pid: False)
        (result["task_dir"] / "initial_state.json").unlink()
        data = get_console_run(result["thread_id"], outputs_root=root)
        assert data["initial_state"] is None

    def test_detail_initial_state_none_when_corrupt(self, prepared, monkeypatch):
        result, root = prepared
        (result["task_dir"] / "initial_state.json").write_text(
            "{broken json", encoding="utf-8"
        )
        data = get_console_run(result["thread_id"], outputs_root=root)
        assert data["initial_state"] is None

    def test_traversal_thread_id_rejected(self, tmp_path):
        with pytest.raises(ConsoleUploadError) as ei:
            get_console_run("..", outputs_root=tmp_path)
        assert ei.value.status_code == 400


class TestRestartRecovery:
    def test_history_visible_and_restart_guard_after_registry_clear(
        self, prepared, monkeypatch,
    ):
        """模拟服务重启:_ACTIVE_PROCS 清空后,历史仍可见、重复启动仍被 409 拒绝。"""
        result, root = prepared
        monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", root)  # cancel_run 解析用
        monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "stub")
        monkeypatch.setattr(config, "CONSOLE_ALLOW_STUB", True)
        monkeypatch.setattr(cs, "_default_popen", lambda: (lambda a, **k: FakeProc(4321)))
        monkeypatch.setattr(cs, "_check_postgres", lambda: None)
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: _snap(False))
        monkeypatch.setattr(cs, "_pid_alive", lambda pid: False)

        start_run(result["task_dir"], venv_python="py-exe")
        assert result["thread_id"] in cs._ACTIVE_PROCS

        # —— 服务重启:注册表清空 ——
        cs._ACTIVE_PROCS.clear()
        cs._STARTED_AT.clear()

        runs = list_console_runs(outputs_root=root)
        assert len(runs) == 1
        assert runs[0]["thread_id"] == result["thread_id"]
        assert runs[0]["record_status"] == "starting"
        assert runs[0]["state"] == "unknown"  # pid 已死且无事件:如实报 unknown

        detail = get_console_run(result["thread_id"], outputs_root=root)
        assert detail["state"] == "unknown"

        # 重启后按 PID 盲杀被拒绝;对同一任务重复 start_run 也被 409 拒绝
        with pytest.raises(ConsoleLaunchError) as ei:
            cs.cancel_run(result["thread_id"])
        assert ei.value.status_code == 409
        with pytest.raises(ConsoleLaunchError) as ei2:
            start_run(result["task_dir"], venv_python="py-exe")
        assert ei2.value.status_code == 409
