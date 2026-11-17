"""web/console_service.resume_run 单元测试(第二期阶段 5)。

全部注入假实现(事件库快照 / Popen / PG 预检),无真机依赖。
覆盖:waiting_human 门禁(非等待态 409)、argv 形状(resume_workflow.py +
--value JSON + --checkpoint 可选 + --llm-backend 与 start_run 同门禁)、
并发额度 409、PG 不可达 503、Popen 失败 500 + 记录落盘、API 级 CSRF 门禁;
2026-11-17 失败节点重试(failed 状态 → --continue argv / mode=retry 审计 /
completed·cancelled 仍 409 / 默认门禁 allow_missing_draft 接线)。
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

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
    def __init__(self, pid: int = 4321, alive: bool = True, returncode: int = 0):
        self.pid = pid
        self._alive = alive
        self._returncode = returncode

    def poll(self):
        return None if self._alive else self._returncode


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def _ok_probe(path) -> dict:
    return {"width": 640, "height": 360, "duration": 30.0}


def _waiting_snapshot(tid):
    return {"available": True, "last_event": {"event_type": "run.interrupted"}}


def _failed_snapshot(tid):
    return {"available": True, "last_event": {"event_type": "run.failed"}}


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
    cs._CLAIMED_TIDS.clear()
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
        # 2026-11-17:审计增加 mode 键(resume=关卡恢复 / retry=失败节点重试)
        # 2026-11-18 P0-06:审计增加 attempt_id 键(与 env 注入/202 响应一一对应)
        entry = record["resumes"][0]
        assert entry["mode"] == "resume"
        assert entry["checkpoint"] is None and entry["value"] is True
        assert entry["draft_sha256"] == {}
        assert isinstance(entry.get("attempt_id"), str) and entry["attempt_id"]
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
        # 2026-11-17:文案随语义扩宽(waiting_human 恢复 / failed 重试之外仍 409)
        assert "未处于可恢复状态" in str(ei.value)

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

    # ------------------------------------------------------------------
    # 2026-11-07:恢复优先用创建时固化的 record["llm_backend"]
    # ------------------------------------------------------------------

    def test_resume_uses_recorded_backend(self, waiting_env, monkeypatch):
        """记录固化 claude-cli、env 已漂移为 mcode-cli → 恢复仍用 claude-cli。"""
        result, root = waiting_env
        record_path = cs.console_run_json_path(result["task_dir"])
        stored = json.loads(record_path.read_text(encoding="utf-8"))
        stored["llm_backend"] = "claude-cli"
        cs._write_run_json(result["task_dir"], stored)
        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return FakeProc(4321)

        monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "mcode-cli")
        resume_run(result["thread_id"], popen=fake_popen, pg_check=lambda: None)
        argv = captured["argv"]
        assert argv[argv.index("--llm-backend") + 1] == "claude-cli"

    def test_resume_recorded_stub_blocked_without_allow(self, waiting_env, monkeypatch):
        """记录固化 stub 但 ALLOW_STUB 已收回 → 503(防御配置漂移)。"""
        result, root = waiting_env
        record_path = cs.console_run_json_path(result["task_dir"])
        stored = json.loads(record_path.read_text(encoding="utf-8"))
        stored["llm_backend"] = "stub"
        cs._write_run_json(result["task_dir"], stored)
        monkeypatch.setattr(config, "CONSOLE_ALLOW_STUB", False)
        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(),
                       pg_check=lambda: None)
        assert ei.value.status_code == 503

    def test_resume_recorded_invalid_backend_500(self, waiting_env, monkeypatch):
        """记录里出现白名单外后端 = 记录损坏 → 500,不回落 env(避免静默换后端)。"""
        result, root = waiting_env
        record_path = cs.console_run_json_path(result["task_dir"])
        stored = json.loads(record_path.read_text(encoding="utf-8"))
        stored["llm_backend"] = "gpt-4"
        cs._write_run_json(result["task_dir"], stored)
        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(),
                       pg_check=lambda: None)
        assert ei.value.status_code == 500

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


class TestAtomicClaim:
    """P0-01/02(2026-11-18 审查计划 §4.1):恢复/启动的 thread 级原子占用互斥。

    修复前:`_ACTIVE_LOCK` 内查完 `_ACTIVE_PROCS` 即放锁,spawn 与注册在锁外,
    两个并发请求可同时通过检查 → 同一 thread 双进程恢复。
    """

    def test_concurrent_resume_same_thread_single_acceptance(self, waiting_env, monkeypatch):
        """两个并发恢复请求同一 thread:恰一个受理、一个 409;popen 恰启动一次。"""
        result, _root = waiting_env
        spawn_calls: list[list[str]] = []
        release_pg = threading.Event()
        barrier = threading.Barrier(2, timeout=5)

        def sync_snapshot(tid):
            # 两个请求都在状态门禁处对齐,同时进入占用点(放大旧竞态窗口)
            barrier.wait()
            return {"available": True, "last_event": {"event_type": "run.interrupted"}}

        def slow_pg():
            # 胜者持占用阻塞在 PG 预检,确保败者此时撞占用 → 409
            release_pg.wait(timeout=5)

        def fake_popen(argv, **kwargs):
            spawn_calls.append(argv)
            return FakeProc(4321)

        monkeypatch.setattr(cs, "_latest_snapshot", sync_snapshot)

        outcomes: dict[int, tuple[str, Any]] = {}

        def worker(idx: int) -> None:
            try:
                outcomes[idx] = (
                    "ok",
                    resume_run(result["thread_id"], popen=fake_popen,
                               pg_check=slow_pg, venv_python="py-exe"),
                )
            except ConsoleLaunchError as e:
                outcomes[idx] = ("err", e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(v[0] == "err" for v in outcomes.values()):
                break
            time.sleep(0.02)
        release_pg.set()
        for t in threads:
            t.join(timeout=10)

        results = list(outcomes.values())
        assert len(results) == 2 and all(r[0] in ("ok", "err") for r in results)
        accepted = [r for r in results if r[0] == "ok"]
        rejected = [r for r in results if r[0] == "err"]
        assert len(accepted) == 1, "并发同 thread 恢复必须恰一个受理"
        assert len(rejected) == 1, "另一个请求必须 409"
        assert rejected[0][1].status_code == 409
        assert len(spawn_calls) == 1, "绝不能启动第二个恢复进程"
        assert result["thread_id"] in cs._ACTIVE_PROCS, "胜者进程已注册"
        assert result["thread_id"] not in cs._CLAIMED_TIDS, "占用已随转正释放"

    def test_manual_claim_blocks_resume_and_start(self, waiting_env, monkeypatch):
        """占用中的 thread:恢复与新建启动都 409(人工恢复/失败重试/新建互斥)。"""
        result, root = waiting_env
        monkeypatch.setattr(cs, "_latest_snapshot", _waiting_snapshot)
        monkeypatch.setattr(cs, "_pid_alive", lambda pid: None)
        cs._CLAIMED_TIDS.add(result["thread_id"])
        try:
            with pytest.raises(ConsoleLaunchError) as ei:
                resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(),
                           pg_check=lambda: None)
            assert ei.value.status_code == 409
            assert "已有恢复/重试进行中" in str(ei.value)
            # start_run 要求 prepared 状态才会走到占用检查;临时改回
            record_path = cs.console_run_json_path(result["task_dir"])
            stored = json.loads(record_path.read_text(encoding="utf-8"))
            stored["status"] = "prepared"
            cs._write_run_json(result["task_dir"], stored)
            with pytest.raises(ConsoleLaunchError) as ei2:
                cs.start_run(result["task_dir"], popen=lambda a, **k: FakeProc(),
                             pg_check=lambda: None)
            assert ei2.value.status_code == 409
            assert "已有恢复/重试进行中" in str(ei2.value)
            # 还原 starting,便于释放占用后验证恢复放行
            stored["status"] = "starting"
            cs._write_run_json(result["task_dir"], stored)
        finally:
            cs._release_claim(result["thread_id"])
        # 释放后恢复放行
        record = resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(4321),
                            pg_check=lambda: None, venv_python="py-exe")
        assert record["status"] == "starting"

    def test_different_thread_ids_not_blocked(self, waiting_env, monkeypatch):
        """不同 thread 并发恢复互不阻塞(互斥是 thread 级,不是全局)。"""
        result, _root = waiting_env
        # 另一个任务占着,不影响本任务恢复
        cs._CLAIMED_TIDS.add("console-zzzzzzzzzzzz")
        try:
            record = resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(4321),
                                pg_check=lambda: None, venv_python="py-exe")
            assert record["status"] == "starting"
        finally:
            cs._release_claim("console-zzzzzzzzzzzz")

    def test_claim_blocks_cancel_and_delete(self, waiting_env):
        result, _root = waiting_env
        cs._CLAIMED_TIDS.add(result["thread_id"])
        try:
            with pytest.raises(ConsoleLaunchError) as ei:
                cs.cancel_run(result["thread_id"], terminate=lambda p: None)
            assert ei.value.status_code == 409
            with pytest.raises(ConsoleLaunchError) as ei2:
                cs.delete_run(result["thread_id"])
            assert ei2.value.status_code == 409
        finally:
            cs._release_claim(result["thread_id"])


class TestRetryRunService:
    """2026-11-17 失败节点重试:failed 状态放行,argv 走 --continue 续跑。

    failed_env 不注入 draft_guard(走默认门禁):fixture 把 gate_state 取数层
    mock 成无草稿,验证 retry 模式 allow_missing_draft=True 放行的接线;
    门禁本体行为见 test_console_resume_guard.py。
    """

    @pytest.fixture()
    def failed_env(self, tmp_path, monkeypatch):
        """已启动(starting)任务 + 事件库调和为 failed(节点报错终态)。"""
        monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
        monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "stub")
        monkeypatch.setattr(config, "CONSOLE_ALLOW_STUB", True)
        monkeypatch.setattr(cs, "_latest_snapshot", _failed_snapshot)
        monkeypatch.setattr(cs, "_pid_alive", lambda pid: None)
        # 默认门禁链的取数层:state 无 draft_path(失败发生在产草稿前)
        monkeypatch.setattr("web.gate_state.fetch_state_values", lambda tid: {})
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

    def test_retry_happy_path_argv_and_mode(self, failed_env):
        result, _root = failed_env
        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return FakeProc(4321)

        record = resume_run(
            result["thread_id"], popen=fake_popen, pg_check=lambda: None,
            venv_python="py-exe",
        )
        argv = captured["argv"]
        # retry 分支:--continue 续跑;--value/--checkpoint 不适用
        assert "--continue" in argv
        assert "--value" not in argv and "--checkpoint" not in argv
        assert argv[argv.index("--thread-id") + 1] == result["thread_id"]
        assert argv[argv.index("--llm-backend") + 1] == "stub"  # 与关卡恢复同门禁
        # 审计记录:mode=retry,checkpoint/value 记缺省
        entry = record["resumes"][-1]
        assert entry["mode"] == "retry"
        assert entry["checkpoint"] is None and entry["value"] is True
        assert record["status"] == "starting"

    def test_retry_still_409_for_completed(self, failed_env, monkeypatch):
        result, _root = failed_env
        monkeypatch.setattr(
            cs, "_latest_snapshot",
            lambda tid: {"available": True,
                         "last_event": {"event_type": "run.completed"}},
        )
        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(),
                       pg_check=lambda: None)
        assert ei.value.status_code == 409

    def test_retry_still_409_for_cancelled_record(self, failed_env):
        """record.status=cancelled(canceled 终态)→ 409,事件库不参与调和。"""
        result, _root = failed_env
        record_path = cs.console_run_json_path(result["task_dir"])
        stored = json.loads(record_path.read_text(encoding="utf-8"))
        stored["status"] = "cancelled"
        cs._write_run_json(result["task_dir"], stored)
        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(),
                       pg_check=lambda: None)
        assert ei.value.status_code == 409

    def test_retry_default_guard_receives_allow_missing_draft(self, failed_env, monkeypatch):
        """默认门禁在 retry 模式下透传 allow_missing_draft=True(无草稿放行)。"""
        result, _root = failed_env
        seen: dict = {}

        def spy_guard(tid, *, allow_missing_draft=False):
            seen["allow_missing_draft"] = allow_missing_draft
            return {"ok": True, "problems": [], "hashes": {}}

        monkeypatch.setattr(cs, "_resume_draft_guard", spy_guard)
        resume_run(result["thread_id"], popen=lambda a, **k: FakeProc(),
                   pg_check=lambda: None)
        assert seen["allow_missing_draft"] is True

    def test_retry_guard_fail_409_no_popen(self, failed_env):
        """retry 模式下有草稿但门禁不过(如剪映未关)→ 409,不启动进程。"""
        result, _root = failed_env
        popen_calls = []

        def bad_guard(tid):
            return {"ok": False, "problems": ["检测到剪映客户端正在运行"], "hashes": {}}

        with pytest.raises(ConsoleLaunchError) as ei:
            resume_run(result["thread_id"], popen=lambda a, **k: popen_calls.append(a) or FakeProc(),
                       pg_check=lambda: None, draft_guard=bad_guard)
        assert ei.value.status_code == 409
        assert popen_calls == []

    def test_api_retry_202(self, failed_env, monkeypatch):
        result, _root = failed_env
        monkeypatch.setattr(cs, "_default_popen", lambda: (lambda a, **k: FakeProc(4321)))
        monkeypatch.setattr(cs, "_check_postgres", lambda: None)
        resp = client.post(
            f"/api/console/runs/{result['thread_id']}/resume",
            json={"checkpoint": None, "value": True},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["ok"] is True and body["state"] == "resuming"
        # P0-06:202 = 受理而非执行成功,attempt_id 必须随受理返回
        assert isinstance(body.get("attempt_id"), str) and body["attempt_id"]


class TestAttemptLifecycle:
    """P0-05/06(审查计划 §4.4):进程退出收割 + attempt 审计生命周期。

    CLI 在发出 run.resumed 前提前退出(前置检查失败/PG 不可达)时,事件库没有
    新事件,服务端必须自己把 attempt 收敛为明确终态,不遗留永久 starting;
    工作流状态与恢复尝试状态分开记录,绝不覆盖事件库给出的真实终态。
    """

    def test_reaper_finalizes_attempt_on_early_exit(self, waiting_env, monkeypatch):
        """CLI 已退出(exit=7,无事件)→ attempt=failed_preflight + record 收敛 failed。"""
        result, _root = waiting_env
        # 受理阶段事件库仍是 waiting_human(fixture 快照);进程退出后无新事件
        record = resume_run(
            result["thread_id"],
            popen=lambda a, **k: FakeProc(4321, alive=False, returncode=7),
            pg_check=lambda: None, venv_python="py-exe",
        )
        assert record["resumes"][-1]["mode"] == "resume"
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: {"available": False})
        cs.reap_now()
        stored = json.loads(
            cs.console_run_json_path(result["task_dir"]).read_text(encoding="utf-8")
        )
        entry = stored["resumes"][-1]
        assert entry["exit_code"] == 7
        assert entry["result"] == "failed_preflight"
        assert entry["finished_at"]
        assert stored["status"] == "failed", "提前退出且无终态事件 → 不得停留在 starting"
        assert "exit=7" in stored["error"]
        assert result["thread_id"] not in cs._ACTIVE_PROCS
        # 收割标记落盘:重启对账不会再翻这条记录
        assert stored["last_proc_exit"]["exit_code"] == 7

    def test_reaper_exit0_leaves_status_to_reconciliation(self, waiting_env, monkeypatch):
        """exit=0 → attempt=succeeded + 收割标记;record 状态交事件库调和
        (waiting_human/completed 属正常落点,对账不因事件库暂时不可达误翻)。"""
        result, _root = waiting_env
        resume_run(
            result["thread_id"],
            popen=lambda a, **k: FakeProc(4321, alive=False, returncode=0),
            pg_check=lambda: None, venv_python="py-exe",
        )
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: {"available": False})
        cs.reap_now()
        stored = json.loads(
            cs.console_run_json_path(result["task_dir"]).read_text(encoding="utf-8")
        )
        entry = stored["resumes"][-1]
        assert entry["exit_code"] == 0 and entry["result"] == "succeeded"
        assert stored["status"] == "starting"

    def test_reaper_nonzero_exit_with_terminal_event_keeps_event_truth(self, waiting_env, monkeypatch):
        """exit≠0 但事件库已有 run.failed → 终态以事件库为准,记录不被覆盖。"""
        result, _root = waiting_env
        resume_run(
            result["thread_id"],
            popen=lambda a, **k: FakeProc(4321, alive=False, returncode=1),
            pg_check=lambda: None, venv_python="py-exe",
        )
        monkeypatch.setattr(
            cs, "_latest_snapshot",
            lambda tid: {"available": True, "last_event": {"event_type": "run.failed"}},
        )
        cs.reap_now()
        stored = json.loads(
            cs.console_run_json_path(result["task_dir"]).read_text(encoding="utf-8")
        )
        assert stored["resumes"][-1]["exit_code"] == 1
        assert stored["resumes"][-1]["result"] == "failed_execution"
        assert stored["status"] == "starting", "事件库是事实来源,不被退出码覆盖"

    def test_attempt_id_injected_via_env(self, waiting_env):
        """attempt_id 经 env 注入子进程,与审计条目一致(P0-06 全链路)。"""
        result, _root = waiting_env
        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            return FakeProc(4321)

        record = resume_run(
            result["thread_id"], popen=fake_popen, pg_check=lambda: None,
            venv_python="py-exe",
        )
        env_attempt = captured["env"].get("AUTO_VIDEO_RESUME_ATTEMPT_ID")
        assert env_attempt == record["resumes"][-1]["attempt_id"]
        assert env_attempt, "env 必须携带 attempt_id"


class TestStaleReconcile:
    """P0-05 服务重启对账:遗留 starting/running 收敛,不误伤活进程/正常挂起。"""

    def test_dead_pid_no_events_converges_failed(self, waiting_env, monkeypatch):
        """重启遗留(未收尾 attempt + 无事件 + 进程已死)→ failed + attempt 收尾。"""
        result, _root = waiting_env
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: {"available": False})
        record_path = cs.console_run_json_path(result["task_dir"])
        stored = json.loads(record_path.read_text(encoding="utf-8"))
        stored["resumes"] = [
            {"at": "2026-11-18T00:00:00+00:00", "mode": "retry", "attempt_id": "abc123"}
        ]  # 无 finished_at = 服务死亡导致收尾从未发生
        cs._write_run_json(result["task_dir"], stored)
        converged = cs.reconcile_stale_starting(force=True)
        assert result["thread_id"] in converged
        after = json.loads(record_path.read_text(encoding="utf-8"))
        assert after["status"] == "failed"
        assert "服务重启对账" in after["error"]
        assert after["resumes"][-1]["finished_at"]
        assert after["resumes"][-1]["exit_code"] is None
        assert after["resumes"][-1]["result"] == "failed_execution"

    def test_reaped_exit0_record_not_reflipped(self, waiting_env, monkeypatch):
        """收割器已观察过 exit=0(有 finished_at + last_proc_exit)→ 对账跳过。"""
        result, _root = waiting_env
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: {"available": False})
        record_path = cs.console_run_json_path(result["task_dir"])
        stored = json.loads(record_path.read_text(encoding="utf-8"))
        stored["resumes"] = [{
            "at": "2026-11-18T00:00:00+00:00", "mode": "resume", "attempt_id": "x1",
            "finished_at": "2026-11-18T00:01:00+00:00", "exit_code": 0, "result": "succeeded",
        }]
        stored["last_proc_exit"] = {"exit_code": 0, "at": "2026-11-18T00:01:00+00:00"}
        cs._write_run_json(result["task_dir"], stored)
        converged = cs.reconcile_stale_starting(force=True)
        assert result["thread_id"] not in converged
        after = json.loads(record_path.read_text(encoding="utf-8"))
        assert after["status"] == "starting"

    def test_waiting_human_untouched(self, waiting_env, monkeypatch):
        """事件库派生 waiting_human = 正常挂起稳态:不改记录,attempt 记成功。"""
        result, _root = waiting_env
        monkeypatch.setattr(cs, "_latest_snapshot", _waiting_snapshot)
        record_path = cs.console_run_json_path(result["task_dir"])
        stored = json.loads(record_path.read_text(encoding="utf-8"))
        stored["resumes"] = [{"at": "2026-11-18T00:00:00+00:00", "mode": "resume"}]
        cs._write_run_json(result["task_dir"], stored)
        converged = cs.reconcile_stale_starting(force=True)
        assert result["thread_id"] not in converged
        after = json.loads(record_path.read_text(encoding="utf-8"))
        assert after["status"] == "starting"
        assert after["resumes"][-1]["result"] == "succeeded"

    def test_alive_pid_skipped(self, waiting_env, monkeypatch):
        """PID 仍活着(入口 A 直启/PID 复用)→ 绝不误判,保持调和态。"""
        result, _root = waiting_env
        monkeypatch.setattr(cs, "_latest_snapshot", lambda tid: {"available": False})
        monkeypatch.setattr(cs, "_pid_alive", lambda pid: True)
        converged = cs.reconcile_stale_starting(force=True)
        assert result["thread_id"] not in converged
        after = json.loads(
            cs.console_run_json_path(result["task_dir"]).read_text(encoding="utf-8")
        )
        assert after["status"] == "starting"


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
