"""POST /api/console/runs API 测试(第二期阶段 1)。

覆盖计划 §11 阶段 1 验收 + 测试矩阵:
- 单视频/多视频 happy path(202 + prepared + 三个 URL);
- 参数校验 400;扩展名 415;超限 413;Origin 403;
- 任务目录外无文件;不启动工作流(响应 state 恒为 prepared,阶段 2 前无启动语义)。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config  # noqa: E402
from monitor_fastapi import app  # noqa: E402
from web.console_uploads import ConsoleUploadError  # noqa: E402

client = TestClient(app)


def _ok_probe(path):
    return {"width": 1280, "height": 720, "duration": 30.0}


@pytest.fixture()
def console_env(tmp_path, monkeypatch):
    """隔离 outputs 根目录 + 探测函数(不依赖真实 ffprobe)。"""
    monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
    monkeypatch.setattr("web.console_uploads.probe_video_file", _ok_probe)
    return tmp_path


def _files(*specs):
    out = []
    for name, data in specs:
        out.append(("videos", (name, io.BytesIO(data), "application/octet-stream")))
    return out


class TestHappyPath:
    def test_single_video_202_prepared(self, console_env):
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "剪一条 30 秒旅行回顾短片"},
            files=_files(("trip.mp4", b"video-bytes")),
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["state"] == "prepared"
        tid = body["thread_id"]
        assert tid.startswith("console-") and len(tid) == len("console-") + 12
        assert body["monitor_url"] == f"/?thread_id={tid}"
        assert body["snapshot_url"] == f"/api/workflow/runs/{tid}"
        assert body["events_url"] == f"/api/workflow/runs/{tid}/events"
        task_dir = console_env / tid
        assert (task_dir / "inputs" / "0001.mp4").read_bytes() == b"video-bytes"
        assert (task_dir / "initial_state.json").exists()
        assert (task_dir / "console_run.json").exists()

    def test_multi_video_202(self, console_env):
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p", "target_duration_ms": "60000",
                  "ratio": "16:9", "lang": "en"},
            files=_files(("a.mp4", b"1"), ("b.mov", b"2")),
        )
        assert resp.status_code == 202, resp.text
        tid = resp.json()["thread_id"]
        assert (console_env / tid / "inputs" / "0001.mp4").exists()
        assert (console_env / tid / "inputs" / "0002.mov").exists()

    def test_no_files_outside_task_dir(self, console_env):
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p"}, files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 202
        entries = sorted(p.name for p in console_env.iterdir())
        assert len(entries) == 1 and entries[0].startswith("console-")


class TestParamValidation400:
    def test_missing_prompt(self, console_env):
        resp = client.post(
            "/api/console/runs", data={}, files=_files(("a.mp4", b"x"))
        )
        assert resp.status_code == 400
        assert "prompt" in resp.json()["detail"]

    def test_blank_prompt(self, console_env):
        resp = client.post(
            "/api/console/runs", data={"prompt": "   "},
            files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 400

    def test_prompt_too_long(self, console_env, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_PROMPT_MAX_CHARS", 10)
        resp = client.post(
            "/api/console/runs", data={"prompt": "字" * 11},
            files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 400

    def test_no_videos(self, console_env):
        resp = client.post("/api/console/runs", data={"prompt": "p"})
        assert resp.status_code == 400

    @pytest.mark.parametrize("duration", ["0", "-5", "999999999"])
    def test_bad_target_duration(self, console_env, duration):
        resp = client.post(
            "/api/console/runs", data={"prompt": "p", "target_duration_ms": duration},
            files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("ratio", ["1:1", "21:9", "nine"])
    def test_bad_ratio(self, console_env, ratio):
        resp = client.post(
            "/api/console/runs", data={"prompt": "p", "ratio": ratio},
            files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("lang", ["fr", "jp"])
    def test_bad_lang(self, console_env, lang):
        resp = client.post(
            "/api/console/runs", data={"prompt": "p", "lang": lang},
            files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 400

    def test_empty_ratio_lang_fall_back_to_defaults(self, console_env):
        """空串表单值按 FastAPI 语义回退契约默认值(9:16 / zh)。"""
        resp = client.post(
            "/api/console/runs", data={"prompt": "p", "ratio": "", "lang": ""},
            files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 202
        tid = resp.json()["thread_id"]
        state = json.loads(
            (console_env / tid / "initial_state.json").read_text(encoding="utf-8")
        )
        assert state["storyline_targets"]["ratio"] == "9:16"
        assert state["storyline_targets"]["lang"] == "zh"


class TestFileValidation:
    def test_disallowed_extension_415(self, console_env):
        resp = client.post(
            "/api/console/runs", data={"prompt": "p"},
            files=_files(("evil.txt", b"text")),
        )
        assert resp.status_code == 415
        assert list(console_env.iterdir()) == []  # 无孤儿目录

    def test_oversize_413(self, console_env, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_MAX_VIDEO_BYTES", 10)
        resp = client.post(
            "/api/console/runs", data={"prompt": "p"},
            files=_files(("big.mp4", b"x" * 100)),
        )
        assert resp.status_code == 413
        assert list(console_env.iterdir()) == []

    def test_too_many_files_400(self, console_env, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_MAX_VIDEOS", 2)
        resp = client.post(
            "/api/console/runs", data={"prompt": "p"},
            files=_files(("a.mp4", b"1"), ("b.mp4", b"2"), ("c.mp4", b"3")),
        )
        assert resp.status_code == 400

    def test_probe_failure_415(self, console_env, monkeypatch):
        def _bad_probe(path):
            raise ConsoleUploadError("ffprobe 校验失败", status_code=415)

        monkeypatch.setattr("web.console_uploads.probe_video_file", _bad_probe)
        resp = client.post(
            "/api/console/runs", data={"prompt": "p"},
            files=_files(("fake.mp4", b"garbage")),
        )
        assert resp.status_code == 415
        assert list(console_env.iterdir()) == []  # 失败不生成可运行任务


class TestOriginGuard:
    def test_cross_origin_rejected_403(self, console_env):
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p"}, files=_files(("a.mp4", b"x")),
            headers={"Origin": "http://evil.example.com"},
        )
        assert resp.status_code == 403
        assert list(console_env.iterdir()) == []

    def test_local_origin_accepted(self, console_env):
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p"}, files=_files(("a.mp4", b"x")),
            headers={"Origin": "http://127.0.0.1:7860"},
        )
        assert resp.status_code == 202


class TestReadOnlyBoundary:
    def test_workflow_api_stays_get_only(self):
        """防漂移:挂载 console router 后,/api/workflow/* 仍不得出现 POST。"""
        from web.api_workflow import router as wf_router

        methods = {
            (getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", []) or [])))
            for r in wf_router.routes
        }
        assert all(m != ("POST",) for _, m in methods)

    def test_console_route_registered(self):
        """console router 声明了 POST /runs(挂载生效由上方 TestClient 用例证明;
        FastAPI 0.162 include_router 惰性挂载,app.routes 里看不到 router 路径,
        检查口径与 tests/unit/test_api_workflow_readonly.py 一致)。"""
        from web.api_console import router as console_router

        console_routes = {
            (getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", []) or [])))
            for r in console_router.routes
        }
        assert ("/api/console/runs", ("POST",)) in console_routes
