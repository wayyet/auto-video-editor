"""Console 创建 → 子进程 → PG 事件 集成测试(第二期阶段 4)。

边界与纪律
----------
- 隔离:``isolated_postgres_db`` 建唯一命名测试库(含应用迁移),会话结束删除;
  PG 不可达时按 conftest 纪律**直接 fail**(不静默 skip,方案 §7.2)。
- ``POSTGRES_URI`` 走环境变量(monkeypatch.setenv):服务端 PG 预检与
  ``run_workflow.py`` 子进程(全新 Python 进程)解析优先级都认 env,
  因此父/子两条路径都落到隔离库,生产库一个字节不动。
- ffmpeg/ffprobe 缺失时 skipif(与 test_console_uploads.TestProbeVideoFileReal
  同一先例:工具缺失属环境缺口,显式标注)。

自动化范围:真实 multipart 上传(真 ffprobe)→ 真 PG 预检 → 真 Popen
(``scripts/run_workflow.py`` 全链启动:migrations → checkpointer →
build_graph → run_with_events)→ 首批事件落库 → 进程清理。
**完整跑通整条工作流属真机验收**(阶段 0 文档 §9 遗留 1),不在自动化内。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config  # noqa: E402
import web.api_console as api_console  # noqa: E402
import web.console_service as console_service  # noqa: E402
from monitor_fastapi import app  # noqa: E402

pytestmark = [pytest.mark.postgres]

client = TestClient(app)

#: 子进程从 Popen 到首个事件落库的等待上限(含重依赖 import + 迁移)
_FIRST_EVENT_TIMEOUT_SEC = 180.0
_POLL_INTERVAL_SEC = 2.0


def _make_tiny_mp4(path: Path) -> None:
    """ffmpeg 合成 1 秒彩色测试视频(无外部素材依赖)。"""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
         "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, f"ffmpeg 生成测试视频失败: {proc.stderr[-500:]}"


def _kill_process_tree(proc) -> None:
    """树杀子进程(Windows 用 taskkill /T;POSIX terminate),防测试泄漏。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=30,
            )
        else:
            proc.terminate()
    except Exception:  # noqa: BLE001 — 清理失败不影响断言结论
        pass


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="本机没有 ffmpeg/ffprobe,无法生成与校验真实视频",
)
def test_console_create_run_spawns_workflow_and_writes_first_events(
    tmp_path, monkeypatch, isolated_postgres_db,
):
    """真实上传 + 真实子进程启动 + 首批事件写入隔离库 workflow_run_events。"""
    uri = isolated_postgres_db()
    # 与 test_workflow_stream_events._isolate_event_store 同款:resolve_postgres_target
    # 每次动态读 env,父进程预检与子进程(全新 Python)都落到隔离库
    monkeypatch.setenv("POSTGRES_URI", uri)
    monkeypatch.delenv("AGENT_GATE_POSTGRES_URI", raising=False)
    monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
    monkeypatch.setattr(config, "CONSOLE_LLM_BACKEND", "stub")
    monkeypatch.setattr(config, "CONSOLE_ALLOW_STUB", True)

    api_console._CSRF_TOKENS.clear()
    console_service._ACTIVE_PROCS.clear()

    video = tmp_path / "tiny.mp4"
    _make_tiny_mp4(video)

    proc = None
    try:
        resp = client.post(
            "/api/console/runs",
            data={"prompt": "集成测试:剪一条超短回顾短片"},
            files=[("videos", ("tiny.mp4", video.read_bytes(), "video/mp4"))],
        )
        assert resp.status_code == 202, resp.text
        tid = resp.json()["thread_id"]

        # 落盘断言:initial_state + console_run.json(PID/后端)
        task_dir = tmp_path / tid
        assert (task_dir / "inputs" / "0001.mp4").exists()
        assert (task_dir / "initial_state.json").exists()
        record = json.loads((task_dir / "console_run.json").read_text("utf-8"))
        assert record["status"] == "starting"
        assert isinstance(record["pid"], int) and record["pid"] > 0
        assert record["llm_backend"] == "stub"
        assert record["argv"][-2:] == ["--llm-backend", "stub"]

        proc = console_service._ACTIVE_PROCS.get(tid)
        assert proc is not None and proc.poll() is None  # 子进程存活

        # 轮询隔离库:等子进程写入首批事件(run.started / node.started)
        from monitoring.workflow_event_store import fetch_events_after

        deadline = time.monotonic() + _FIRST_EVENT_TIMEOUT_SEC
        events = []
        while time.monotonic() < deadline:
            events = fetch_events_after(tid, 0, 100)
            if events:
                break
            time.sleep(_POLL_INTERVAL_SEC)

        if not events:
            log_path = task_dir / "console_run.log"
            tail = (
                log_path.read_text("utf-8", errors="replace")[-2000:]
                if log_path.exists() else "<无日志>"
            )
            pytest.fail(
                f"子进程 {_FIRST_EVENT_TIMEOUT_SEC:.0f}s 内未写入任何事件;"
                f"console_run.log 尾部:\n{tail}"
            )

        types = {str(e.get("event_type")) for e in events}
        assert types & {"run.started", "node.started"}, f"首批事件异常: {sorted(types)}"
        # 事件确属本线程且 id 单调
        assert all(e.get("thread_id") == tid for e in events)
        ids = [int(e["id"]) for e in events if e.get("id") is not None]
        assert ids == sorted(ids)
    finally:
        _kill_process_tree(proc)
        console_service._ACTIVE_PROCS.clear()
        api_console._CSRF_TOKENS.clear()
