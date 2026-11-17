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
    cs._CLAIMED_TIDS.clear()


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

    def test_no_draft_anywhere_passes_with_note(self, tmp_path, monkeypatch):
        """P0-03 矩阵「无 draft_path + 无草稿」:物理确认 staging 与正式库均无
        本任务产物 → 按「产草稿前失败」放行(附 note 审计),绝不触碰
        assert_resume_ready(没有草稿可保护)。"""
        monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
        monkeypatch.setattr(config, "JIANYING_DRAFT_ROOT", tmp_path / "jy_root")
        monkeypatch.setattr(gate_state, "fetch_state_values", lambda tid: {})
        called = {"n": 0}

        def fake_assert(draft_file, **k):
            called["n"] += 1
            return {"ok": False, "problems": ["不应被调用"], "hashes": {}, "jianying_running": False}

        monkeypatch.setattr("draft_ops.draft_lock.assert_resume_ready", fake_assert)
        out = gate_state.resume_guard("console-ab12cd34ef56", allow_missing_draft=True)
        assert out["ok"] is True and out["problems"] == [] and out["hashes"] == {}
        assert "已确认无本任务草稿产物" in out["note"]
        assert called["n"] == 0

    def test_allow_missing_draft_ignored_when_draft_present(self, monkeypatch):
        """state 有 draft_path 时 allow_missing_draft 不生效,照常全量校验。"""
        seen = {}

        def fake_assert(draft_file, **k):
            seen["draft_file"] = draft_file
            return {"ok": True, "problems": [], "hashes": {}, "jianying_running": False}

        monkeypatch.setattr(gate_state, "fetch_state_values",
                            lambda tid: {"draft_path": "X:/d/draft_content.json"})
        monkeypatch.setattr("draft_ops.draft_lock.assert_resume_ready", fake_assert)
        out = gate_state.resume_guard("console-ab12cd34ef56", allow_missing_draft=True)
        assert seen["draft_file"] == "X:/d/draft_content.json"
        assert out["ok"] is True


class TestFindTaskDraftArtifacts:
    """P0-03:draft_ops.draft_lock.find_task_draft_artifacts 定位规则本体。"""

    def test_empty_task_dir_and_missing_draft_root(self, tmp_path):
        from draft_ops.draft_lock import find_task_draft_artifacts

        out = find_task_draft_artifacts(
            tmp_path / "console-ab12cd34ef56", tmp_path / "jy_root", "console-ab12cd34ef56"
        )
        assert out == {"staging": [], "official": None, "unreadable": False}

    def test_staging_found_depth1_and_depth2(self, tmp_path):
        from draft_ops.draft_lock import find_task_draft_artifacts

        task_dir = tmp_path / "console-ab12cd34ef56"
        d1 = task_dir / "drafts" / "zh-main"
        d2 = task_dir / "drafts" / "en-branch" / "nested"
        (d1 / "draft_content.json").parent.mkdir(parents=True)
        (d1 / "draft_content.json").write_text("{}", encoding="utf-8")
        (d2 / "draft_content.json").parent.mkdir(parents=True)
        (d2 / "draft_content.json").write_text("{}", encoding="utf-8")
        out = find_task_draft_artifacts(task_dir, None, "console-ab12cd34ef56")
        assert len(out["staging"]) == 2
        assert out["official"] is None and out["unreadable"] is False

    def test_official_found_by_registry_naming(self, tmp_path):
        from draft_ops.draft_lock import find_task_draft_artifacts
        from draft_ops.jianying_registry import official_draft_dir

        root = tmp_path / "jy_root"
        official_dir = official_draft_dir(root, "console-ab12cd34ef56")
        official_dir.mkdir(parents=True)
        (official_dir / "draft_content.json").write_text("{}", encoding="utf-8")
        out = find_task_draft_artifacts(
            tmp_path / "console-ab12cd34ef56", root, "console-ab12cd34ef56"
        )
        assert out["official"] == official_dir / "draft_content.json"
        assert out["staging"] == []


class TestPhysicalDraftArtifacts:
    """P0-03(审查计划 §4.2 / §7.4 矩阵):state 无 draft_path 时的物理草稿门禁。

    「checkpoint 无 draft_path」不能推出「磁盘无草稿」—— 定位到产物必须恢复
    完整门禁(含剪映进程检查),确认无产物才放行,目录不可读 fail-closed。
    """

    def _patch_roots(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
        monkeypatch.setattr(config, "JIANYING_DRAFT_ROOT", tmp_path / "jy_root")
        monkeypatch.setattr(gate_state, "fetch_state_values", lambda tid: {})

    def test_staging_draft_found_runs_full_gate(self, tmp_path, monkeypatch):
        tid = "console-ab12cd34ef56"
        self._patch_roots(tmp_path, monkeypatch)
        draft_file = tmp_path / tid / "drafts" / "zh-main" / "draft_content.json"
        draft_file.parent.mkdir(parents=True)
        draft_file.write_text("{}", encoding="utf-8")
        seen = {}

        def fake_assert(draft_file_arg, **k):
            seen["draft_file"] = draft_file_arg
            return {"ok": True, "problems": [], "hashes": {"draft_content.json": "abc"},
                    "jianying_running": False}

        monkeypatch.setattr("draft_ops.draft_lock.assert_resume_ready", fake_assert)
        out = gate_state.resume_guard(tid, allow_missing_draft=True)
        assert seen["draft_file"] == draft_file, "定位到 staging 草稿必须恢复完整门禁"
        assert out["ok"] is True and out["located"] == str(draft_file)

    def test_official_draft_found_runs_full_gate(self, tmp_path, monkeypatch):
        from draft_ops.jianying_registry import official_draft_dir

        tid = "console-ab12cd34ef56"
        self._patch_roots(tmp_path, monkeypatch)
        official_dir = official_draft_dir(tmp_path / "jy_root", tid)
        official_dir.mkdir(parents=True)
        (official_dir / "draft_content.json").write_text("{}", encoding="utf-8")
        seen = {}

        def fake_assert(draft_file_arg, **k):
            seen["draft_file"] = draft_file_arg
            return {"ok": True, "problems": [], "hashes": {}, "jianying_running": False}

        monkeypatch.setattr("draft_ops.draft_lock.assert_resume_ready", fake_assert)
        out = gate_state.resume_guard(tid, allow_missing_draft=True)
        assert seen["draft_file"] == official_dir / "draft_content.json"
        assert out["ok"] is True

    def test_found_draft_with_jianying_running_rejected(self, tmp_path, monkeypatch):
        """矩阵「无 draft_path + staging 有草稿 + 剪映运行」→ 拒绝恢复。"""
        tid = "console-ab12cd34ef56"
        self._patch_roots(tmp_path, monkeypatch)
        draft_file = tmp_path / tid / "drafts" / "zh-main" / "draft_content.json"
        draft_file.parent.mkdir(parents=True)
        draft_file.write_text("{}", encoding="utf-8")
        out = gate_state.resume_guard(
            tid, allow_missing_draft=True, proc_checker=lambda: True
        )
        assert out["ok"] is False
        assert any("剪映" in p for p in out["problems"])

    def test_unreadable_artifacts_fail_closed(self, tmp_path, monkeypatch):
        """目录不可读/归属不明 → fail-closed,拒绝自动恢复。"""
        tid = "console-ab12cd34ef56"
        self._patch_roots(tmp_path, monkeypatch)

        def unreadable_finder(task_dir, draft_root, thread_id):
            return {"staging": [], "official": None, "unreadable": True}

        monkeypatch.setattr("draft_ops.draft_lock.find_task_draft_artifacts", unreadable_finder)
        out = gate_state.resume_guard(tid, allow_missing_draft=True)
        assert out["ok"] is False
        assert "fail-closed" in out["problems"][0]

    def test_real_staging_draft_gate_passes_when_jianying_closed(self, tmp_path, monkeypatch):
        """真文件 + 剪映未运行(注入):门禁全量执行并通过,hashes 落审计值。"""
        tid = "console-ab12cd34ef56"
        self._patch_roots(tmp_path, monkeypatch)
        draft_file = tmp_path / tid / "drafts" / "zh-main" / "draft_content.json"
        draft_file.parent.mkdir(parents=True)
        draft_file.write_text('{"name":"zh-main"}', encoding="utf-8")
        out = gate_state.resume_guard(
            tid, allow_missing_draft=True,
            proc_checker=lambda: False, require_jianying_closed=True,
        )
        assert out["ok"] is True, out["problems"]
        assert "draft_content.json" in out["hashes"]


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
