"""POST /api/console/runs API 测试(第二期阶段 1;阶段 3 扩充 CSRF/config/State 映射)。

覆盖计划 §11 阶段 1 验收 + 测试矩阵:
- 单视频/多视频 happy path(202 + prepared + 三个 URL);
- 参数校验 400;扩展名 415;超限 413;Origin 403;
- 任务目录外无文件;不启动工作流(响应 state 恒为 prepared,阶段 2 前无启动语义);
- 阶段 3:CSRF 门禁(带 Origin 必须携带 token)、GET /config 限制下发、
  提示词 → storyline_targets 的 API 级映射断言。
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
import web.api_console as api_console  # noqa: E402
import web.console_service as console_service  # noqa: E402
from monitor_fastapi import app  # noqa: E402
from web.console_uploads import ConsoleUploadError  # noqa: E402

client = TestClient(app)


class _FakeProc:
    def __init__(self, pid: int = 4321):
        self.pid = pid
        self._rc = None

    def poll(self):
        return self._rc


def _ok_probe(path):
    return {"width": 1280, "height": 720, "duration": 30.0}


@pytest.fixture()
def console_env(tmp_path, monkeypatch):
    """隔离 outputs 根目录 + 探测函数 + 启动接缝(PG/Popen 全部假实现)。"""
    monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
    monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "stub")
    monkeypatch.setattr(config, "CONSOLE_ALLOW_STUB", True)
    monkeypatch.setattr("web.console_uploads.probe_video_file", _ok_probe)
    monkeypatch.setattr("web.console_service._check_postgres", lambda: None)
    monkeypatch.setattr(
        "web.console_service._default_popen", lambda: (lambda a, **k: _FakeProc(4321))
    )
    yield tmp_path
    console_service._ACTIVE_PROCS.clear()
    api_console._CSRF_TOKENS.clear()


def _csrf_token() -> str:
    """同源取一个 CSRF token(阶段 3 起,带 Origin 的浏览器写请求必须携带)。"""
    resp = client.get("/api/console/csrf")
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _files(*specs):
    out = []
    for name, data in specs:
        out.append(("videos", (name, io.BytesIO(data), "application/octet-stream")))
    return out


class TestHappyPath:
    def test_single_video_202_starting(self, console_env):
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "剪一条 30 秒旅行回顾短片"},
            files=_files(("trip.mp4", b"video-bytes")),
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["state"] == "starting"  # 阶段 2:子进程已创建,不等待执行
        tid = body["thread_id"]
        assert tid.startswith("console-") and len(tid) == len("console-") + 12
        assert body["monitor_url"] == f"/?thread_id={tid}"
        assert body["snapshot_url"] == f"/api/workflow/runs/{tid}"
        assert body["events_url"] == f"/api/workflow/runs/{tid}/events"
        task_dir = console_env / tid
        assert (task_dir / "inputs" / "0001.mp4").read_bytes() == b"video-bytes"
        assert (task_dir / "initial_state.json").exists()
        assert (task_dir / "console_run.log").exists()
        record = json.loads((task_dir / "console_run.json").read_text("utf-8"))
        assert record["status"] == "starting"
        assert record["pid"] == 4321
        assert record["llm_backend"] == "stub"

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

    def test_llm_backend_override_recorded(self, console_env, monkeypatch):
        """2026-11-07:表单 llm_backend 显式选择覆盖 env 默认,argv 与 record 同步。"""
        captured = {}

        def capturing_popen(argv, **kwargs):
            captured["argv"] = list(argv)
            return _FakeProc(4321)

        monkeypatch.setattr("web.console_service._default_popen", lambda: capturing_popen)
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p", "llm_backend": "claude-cli"},
            files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 202, resp.text
        tid = resp.json()["thread_id"]
        record = json.loads((console_env / tid / "console_run.json").read_text("utf-8"))
        assert record["llm_backend"] == "claude-cli"
        argv = captured["argv"]
        assert argv[argv.index("--llm-backend") + 1] == "claude-cli"

    def test_prompt_never_in_argv(self, console_env):
        """提示词只进 initial_state.json,绝不进命令行(计划 §7.3)。"""
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "绝密提示词内容XYZ"},
            files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 202
        tid = resp.json()["thread_id"]
        record = json.loads(
            (console_env / tid / "console_run.json").read_text("utf-8")
        )
        argv = record.get("argv") or []
        assert all("绝密提示词内容XYZ" not in a for a in argv)
        state = json.loads(
            (console_env / tid / "initial_state.json").read_text("utf-8")
        )
        assert state["storyline_targets"]["user_request"] == "绝密提示词内容XYZ"

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

    @pytest.mark.parametrize("backend", ["stub", "gate", "gpt-4", "claude cli"])
    def test_bad_llm_backend(self, console_env, backend):
        """stub/gate 等不在表单白名单;浏览器只允许 claude-cli / mcode-cli。"""
        resp = client.post(
            "/api/console/runs", data={"prompt": "p", "llm_backend": backend},
            files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 400
        assert "llm_backend" in resp.json()["detail"]

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


def _write_template(tmp_path, pending: bool, keyword_map: dict) -> Path:
    """写一份贴纸模板夹具;resource_id 是否 19 位由调用方给值决定。"""
    tpl = tmp_path / "sticker_template.json"
    tpl.write_text(
        json.dumps(
            {
                "_reverse_engineering_pending": pending,
                "sticker_by_keyword": keyword_map,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return tpl


class TestStickerLibraryReady:
    """_sticker_library_ready 双条件判定(pending 翻转 + 全值 19 位;缺失/坏 JSON fail-closed)。"""

    def test_pending_true_not_ready(self, tmp_path):
        assert api_console._sticker_library_ready(
            _write_template(tmp_path, True, {"赞": "1234567890123456789"})
        ) is False

    def test_backfilled_placeholder_id_not_ready(self, tmp_path):
        """pending 已翻转但残留占位 ID(模板被改坏)→ 仍拦截。"""
        assert api_console._sticker_library_ready(
            _write_template(tmp_path, False, {"赞": "PLACEHOLDER_STICKER_THUMBS_UP"})
        ) is False

    def test_backfilled_real_ids_ready(self, tmp_path):
        assert api_console._sticker_library_ready(
            _write_template(
                tmp_path, False,
                {"赞": "1234567890123456789", "默认": "9876543210987654321"},
            )
        ) is True

    def test_missing_file_fail_closed(self, tmp_path):
        assert api_console._sticker_library_ready(tmp_path / "nope.json") is False

    def test_malformed_json_fail_closed(self, tmp_path):
        tpl = tmp_path / "broken.json"
        tpl.write_text("{not-json", encoding="utf-8")
        assert api_console._sticker_library_ready(tpl) is False

    def test_empty_keyword_map_not_ready(self, tmp_path):
        assert api_console._sticker_library_ready(
            _write_template(tmp_path, False, {})
        ) is False


class TestStickerRequiredGuard:
    """阶段 6 护栏:贴纸模板未实机回填时,创建勾选 require_stickers=true 直接 400
    (否则要跑完整条链才死在交付 QA,console-ef8504d3a62c 即此死法)。"""

    @pytest.fixture()
    def stickers_not_ready(self, monkeypatch):
        monkeypatch.setattr(api_console, "_sticker_library_ready", lambda *a, **k: False)

    @pytest.fixture()
    def stickers_ready(self, monkeypatch):
        monkeypatch.setattr(api_console, "_sticker_library_ready", lambda *a, **k: True)

    def test_stickers_required_not_ready_400(self, console_env, stickers_not_ready):
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p", "require_stickers": "true"},
            files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "贴纸模板未回填" in detail
        assert "『贴纸』" in detail
        # fail-fast:任务目录不应已创建(prepare_run 之前就拒绝)
        assert not any(console_env.iterdir())

    def test_stickers_unchecked_not_ready_202(self, console_env, stickers_not_ready):
        """不勾贴纸(显式 false 或全缺省)不受护栏影响。"""
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p", "require_stickers": "false", "require_bgm": "true"},
            files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 202, resp.text

    def test_stickers_required_ready_202(self, console_env, stickers_ready):
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p", "require_stickers": "true"},
            files=_files(("a.mp4", b"x")),
        )
        assert resp.status_code == 202, resp.text
        tid = resp.json()["thread_id"]
        state = json.loads(
            (console_env / tid / "initial_state.json").read_text(encoding="utf-8")
        )
        assert state["required_features"]["stickers"] is True

    def test_config_exposes_stickers_ready(self, stickers_not_ready):
        resp = client.get("/api/console/config")
        assert resp.status_code == 200
        assert resp.json()["stickers_ready"] is False


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
        """阶段 3 起同源浏览器请求须携带有效 CSRF token。"""
        token = _csrf_token()
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p"}, files=_files(("a.mp4", b"x")),
            headers={"Origin": "http://127.0.0.1:7860", "X-CSRF-Token": token},
        )
        assert resp.status_code == 202

    def test_lan_origin_same_host_accepted(self, console_env):
        """绑 0.0.0.0 后局域网同源放行:从 http://<LAN-IP>:7860 打开的页面,
        浏览器发出的 Origin 与请求 Host 头天然一致(真同源)。"""
        token = _csrf_token()
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p"}, files=_files(("a.mp4", b"x")),
            headers={
                "Host": "192.168.1.50:7860",
                "Origin": "http://192.168.1.50:7860",
                "X-CSRF-Token": token,
            },
        )
        assert resp.status_code == 202

    def test_lan_origin_host_mismatch_rejected_403(self, console_env):
        """Origin 是局域网 IP 但与 Host 头不一致(跨站页面/代理改写)→ 403。"""
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p"}, files=_files(("a.mp4", b"x")),
            headers={"Host": "192.168.1.50:7860", "Origin": "http://192.168.1.66:7860"},
        )
        assert resp.status_code == 403
        assert list(console_env.iterdir()) == []


class TestCsrfGate:
    """阶段 3 CSRF 门禁:带 Origin 的写请求必须携带 GET /api/console/csrf 签发的 token;
    无 Origin 的本机脚本/curl 豁免(既有 API 兼容)。"""

    def test_csrf_endpoint_issues_token(self):
        resp = client.get("/api/console/csrf")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert isinstance(body["token"], str) and len(body["token"]) >= 32
        assert body["ttl_seconds"] == api_console.CSRF_TOKEN_TTL_SEC

    def test_origin_without_token_403(self, console_env):
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p"}, files=_files(("a.mp4", b"x")),
            headers={"Origin": "http://127.0.0.1:7860"},
        )
        assert resp.status_code == 403
        assert "CSRF" in resp.json()["detail"]

    def test_origin_with_invalid_token_403(self, console_env):
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p"}, files=_files(("a.mp4", b"x")),
            headers={"Origin": "http://127.0.0.1:7860", "X-CSRF-Token": "bogus-token"},
        )
        assert resp.status_code == 403

    def test_expired_token_403(self, console_env):
        token = _csrf_token()
        # 直接把过期时间拨到过去,模拟 TTL 到期
        api_console._CSRF_TOKENS[token] = 0.0
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "p"}, files=_files(("a.mp4", b"x")),
            headers={"Origin": "http://127.0.0.1:7860", "X-CSRF-Token": token},
        )
        assert resp.status_code == 403

    def test_token_reusable_within_ttl(self, console_env):
        token = _csrf_token()
        for _ in range(2):
            resp = client.post(
                "/api/console/runs",
                data={"prompt": "p"}, files=_files(("a.mp4", b"x")),
                headers={"Origin": "http://127.0.0.1:7860", "X-CSRF-Token": token},
            )
            assert resp.status_code == 202

    def test_no_origin_exempt(self, console_env):
        """无 Origin = 非浏览器客户端,不要求 token(兼容既有脚本)。"""
        resp = client.post(
            "/api/console/runs", data={"prompt": "p"}, files=_files(("a.mp4", b"x"))
        )
        assert resp.status_code == 202

    def test_csrf_endpoint_cross_origin_403(self):
        resp = client.get("/api/console/csrf", headers={"Origin": "http://evil.example.com"})
        assert resp.status_code == 403


class TestConsoleConfig:
    """阶段 3:GET /api/console/config 下发表单限制与默认值(不含敏感项)。"""

    def test_config_shape_and_defaults(self, console_env):
        resp = client.get("/api/console/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["max_videos"] == config.CONSOLE_MAX_VIDEOS
        assert body["max_video_bytes"] == config.CONSOLE_MAX_VIDEO_BYTES
        assert body["prompt_max_chars"] == config.CONSOLE_PROMPT_MAX_CHARS
        assert body["target_duration_ms_default"] == config.CONSOLE_TARGET_DURATION_MS_DEFAULT
        assert body["target_duration_ms_max"] == config.CONSOLE_TARGET_DURATION_MS_MAX
        assert body["allowed_video_exts"] == [".mov", ".mp4"]  # sorted,输出稳定
        assert body["ratios"] == ["9:16", "16:9", "4:3"]
        assert body["langs"] == ["zh", "en"]

    def test_config_reflects_env_override(self, console_env, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_MAX_VIDEOS", 3)
        monkeypatch.setattr(config, "CONSOLE_PROMPT_MAX_CHARS", 77)
        body = client.get("/api/console/config").json()
        assert body["max_videos"] == 3
        assert body["prompt_max_chars"] == 77

    def test_config_no_sensitive_keys(self, console_env):
        body = client.get("/api/console/config").json()
        joined = json.dumps(body)
        assert "llm" not in joined.lower()
        assert "backend" not in joined.lower()
        assert "postgres" not in joined.lower()

    def test_config_cross_origin_403(self):
        resp = client.get("/api/console/config", headers={"Origin": "http://evil.example.com"})
        assert resp.status_code == 403


class TestConcurrency409:
    """阶段 4 矩阵「多个任务并发」的 API 级断言(服务级见 test_console_service_launch)。"""

    def test_second_run_rejected_when_limit_reached(self, console_env, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_MAX_CONCURRENT_RUNS", 1)
        r1 = client.post("/api/console/runs", data={"prompt": "p"},
                         files=_files(("a.mp4", b"x")))
        assert r1.status_code == 202
        r2 = client.post("/api/console/runs", data={"prompt": "q"},
                         files=_files(("b.mp4", b"y")))
        assert r2.status_code == 409
        assert "上限" in r2.json()["detail"]


class TestPromptStateMapping:
    """阶段 4 矩阵「提示词 State 映射」的 API 级断言(阶段 3 随前端补齐)。"""

    def test_prompt_maps_to_state_targets(self, console_env):
        prompt = "节奏明快的旅行回顾短片,字幕简体中文"
        resp = client.post("/api/console/runs", data={"prompt": prompt},
                           files=_files(("a.mp4", b"x"), ("b.mov", b"y")))
        assert resp.status_code == 202
        tid = resp.json()["thread_id"]
        state = json.loads((console_env / tid / "initial_state.json").read_text("utf-8"))
        assert state["session_id"] == tid
        assert state["storyline_targets"]["user_request"] == prompt
        assert state["storyline_targets"]["script_request"] == prompt
        assert "query" not in state["storyline_targets"]  # 不得误触发网页话题分支
        assert state["video_input_path"].endswith("0001.mp4")
        assert len(state["video_input_paths"]) == 2
        assert state["storyline_outputs_root"] == str(console_env / tid)
        assert state["error_log"] == []


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
        """console router 声明了写入口与阶段 3 前端支撑 GET(挂载生效由上方
        TestClient 用例证明;FastAPI 0.162 include_router 惰性挂载,app.routes
        里看不到 router 路径,检查口径与 tests/unit/test_api_workflow_readonly.py 一致)。"""
        from web.api_console import router as console_router

        console_routes = {
            (getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", []) or [])))
            for r in console_router.routes
        }
        assert ("/api/console/runs", ("POST",)) in console_routes
        assert ("/api/console/config", ("GET",)) in console_routes
        assert ("/api/console/csrf", ("GET",)) in console_routes


# ---------------------------------------------------------------------------
# 阶段 6(2026-11-09 效果缺失修复):必需功能表单字段
# ---------------------------------------------------------------------------
def test_create_run_required_features_form(console_env, monkeypatch):
    """require_* 表单字段 → initial_state.json 的 required_features(白名单键)。

    桩掉贴纸库就绪检查:本用例只测表单映射,不依赖真实模板的回填状态
    (真实模板 pending 翻转后此桩即冗余,但保留以隔离外部状态)。
    """
    monkeypatch.setattr(api_console, "_sticker_library_ready", lambda *a, **k: True)
    resp = client.post(
        "/api/console/runs",
        data={
            "prompt": "剪一条 30 秒短片",
            "require_bgm": "true",
            "require_transitions": "false",
            "require_stickers": "1",
        },
        files=_files(("trip.mp4", b"video-bytes")),
    )
    assert resp.status_code == 202, resp.text
    tid = resp.json()["thread_id"]
    state = json.loads((console_env / tid / "initial_state.json").read_text("utf-8"))
    assert state["required_features"] == {"bgm": True, "transitions": False, "stickers": True}


def test_create_run_required_features_absent_means_none(console_env):
    """不传 require_* → initial_state 不含 required_features(运行时推导)。"""
    resp = client.post(
        "/api/console/runs",
        data={"prompt": "剪一条 30 秒短片"},
        files=_files(("trip.mp4", b"video-bytes")),
    )
    assert resp.status_code == 202, resp.text
    tid = resp.json()["thread_id"]
    state = json.loads((console_env / tid / "initial_state.json").read_text("utf-8"))
    assert "required_features" not in state
