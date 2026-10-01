"""节点 4a ``import_video`` 单测(2026-10 拆分 node_04 新增)。

覆盖:
1. 无 result + ``timeout_s=0`` → ``timeout``,且请求文件已写出。
2. 预置 ``ok=True`` result → ``imported`` + 回填 3 字段,**不写请求文件**(幂等)。
3. 预置 ``ok=False`` result → ``failed`` + ``error_log`` 含"网页端导入失败"。
4. ``IMPORT_VIDEO_MANUAL_REQUIRED=False`` → 不写请求文件,直接 ``imported``
   且 ``triggered_by="auto_fallback"``。
5. ``find_pending_request``:3 个 job,1 个已有 result → 返回剩下里 mtime 最新的。
6. 守护:源码文本不得出现 ``langgraph`` / ``pydantic`` / ``storyline_capabilities``
   ——守住"轻依赖"跨 venv 硬约束。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import config
import pytest

from nodes.node_04a_import_video import (
    import_video,
    import_video_request_filename,
    import_video_result_filename,
    write_import_request,
    write_import_result,
)


# ---------------------------------------------------------------------------
# 1. 超时
# ---------------------------------------------------------------------------
def test_timeout_when_no_result(tmp_path: Path) -> None:
    """无 result + timeout_s=0 → timeout,请求文件写过后**被清掉**。

    超时即清是刻意的:留着陈旧请求会让之后的网页按钮把它当成"当前待应答的
    job"回写结果(见 ``clear_import_request`` 文档)。
    """
    out_root = tmp_path / "outputs"
    out = import_video(
        {"session_id": "job-to", "video_input_path": "/tmp/a.mp4", "error_log": []},
        outputs_root=out_root,
        poll_interval_s=0,
        timeout_s=0,
    )
    assert out["import_video_status"] == "timeout"
    assert "import_video_timeout" in out["status_log"]
    assert any("超时" in e for e in out["error_log"])
    # 三个回填字段必须为空,不得凭空造出媒体路径
    assert out["import_video_media_path"] is None
    assert out["import_video_web_session_id"] is None
    assert out["import_video_filename"] is None
    # 超时后不留陈旧请求(否则网页按钮会误应答死 job)
    assert not (out_root / "job-to" / import_video_request_filename).exists()
    # 确实走到过"发请求"这一步(目录被建出来了)
    assert (out_root / "job-to").is_dir()


# ---------------------------------------------------------------------------
# 2. 幂等:预置 ok=True 结果
# ---------------------------------------------------------------------------
def test_existing_ok_result_is_reused_without_writing_request(tmp_path: Path) -> None:
    """已有 ok=True result → imported + 回填,且**不再写请求文件**。"""
    out_root = tmp_path / "outputs"
    write_import_result(
        "job-idem",
        {
            "ok": True,
            "web_session_id": "websid1",
            "media_id": "media_0001",
            "filename": "demo.mp4",
            "stored_path": "E:/media/media_0001.mp4",
            "triggered_by": "import_video_button",
        },
        root=out_root,
    )
    out = import_video(
        {"session_id": "job-idem", "error_log": []},
        outputs_root=out_root,
        poll_interval_s=0,
        timeout_s=0,
    )
    assert out["import_video_status"] == "imported"
    assert out["import_video_media_path"] == "E:/media/media_0001.mp4"
    assert out["import_video_web_session_id"] == "websid1"
    assert out["import_video_filename"] == "demo.mp4"
    assert "import_video_done" in out["status_log"]
    # 幂等前置检查生效:没重发请求
    assert not (out_root / "job-idem" / import_video_request_filename).exists()


# ---------------------------------------------------------------------------
# 3. 预置 ok=False 结果
# ---------------------------------------------------------------------------
def test_existing_failed_result_marks_failed(tmp_path: Path) -> None:
    """已有 ok=False result → failed,且 error_log 说明是网页端失败。"""
    out_root = tmp_path / "outputs"
    write_import_result(
        "job-bad",
        {"ok": False, "error": "upload aborted"},
        root=out_root,
    )
    out = import_video(
        {"session_id": "job-bad", "error_log": []},
        outputs_root=out_root,
        poll_interval_s=0,
        timeout_s=0,
    )
    assert out["import_video_status"] == "failed"
    assert any("网页端导入失败" in e and "upload aborted" in e for e in out["error_log"])


# ---------------------------------------------------------------------------
# 4. 回滚开关
# ---------------------------------------------------------------------------
def test_manual_not_required_falls_back_to_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``IMPORT_VIDEO_MANUAL_REQUIRED=False`` → 不写请求文件,直接 auto_fallback。"""
    monkeypatch.setattr(config, "IMPORT_VIDEO_MANUAL_REQUIRED", False)
    out_root = tmp_path / "outputs"
    out = import_video(
        {
            "session_id": "job-auto",
            "video_input_path": "E:/clips/a.mp4",
            "error_log": [],
        },
        outputs_root=out_root,
        poll_interval_s=0,
        timeout_s=0,
    )
    assert out["import_video_status"] == "imported"
    assert out["import_video_media_path"] == "E:/clips/a.mp4"
    assert out["import_video_filename"] == "a.mp4"
    # 回滚路径**不**发请求(没人会来应答)
    assert not (out_root / "job-auto" / import_video_request_filename).exists()
    # 自己写的 result 文件标记为 auto_fallback
    result = json.loads(
        (out_root / "job-auto" / import_video_result_filename).read_text(encoding="utf-8")
    )
    assert result["triggered_by"] == "auto_fallback"
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# 5. find_pending_request 取最新未应答
# ---------------------------------------------------------------------------
def test_find_pending_request_returns_newest_unanswered(tmp_path: Path) -> None:
    """3 个 job:job_a / job_b / job_c。job_a 已有 result → 应被排除。

    剩下 job_b、job_c 里按 mtime 最新的那个胜出。
    """
    from nodes.node_04a_import_video import find_pending_request

    out_root = tmp_path / "outputs"
    write_import_request("job_a", root=out_root)
    write_import_result("job_a", {"ok": True, "filename": "a.mp4"}, root=out_root)

    time.sleep(0.02)  # 保证 mtime 严格递增(部分 FS mtime 精度只有 1s)
    write_import_request("job_b", root=out_root)
    time.sleep(0.02)
    write_import_request("job_c", root=out_root)

    pending = find_pending_request(root=out_root)
    assert pending is not None
    job_id, payload = pending
    assert job_id == "job_c", "应返回最新(mtime 最大)的未应答请求"
    assert payload["job_id"] == "job_c"

    # 请求体契约:accept_exts / accept_mime_prefixes 供前端 input[accept] 用
    assert ".mp4" in payload["accept_exts"]
    assert payload["accept_mime_prefixes"] == ["video/"]


# ---------------------------------------------------------------------------
# 6. 守护:轻依赖硬约束
# ---------------------------------------------------------------------------
def test_module_source_has_no_heavy_dependencies() -> None:
    """源码文本不得出现 ``langgraph`` / ``pydantic`` / ``storyline_capabilities``。

    本模块会被 ``openstoryline/.venv`` 里的 agent_fastapi.py 跨 venv 导入,
    顶层一旦引入重依赖,那个 venv 就会 ImportError(见模块 docstring)。
    用源码文本断言比运行时试 import 更早失败,且不依赖 openstoryline venv。
    """
    src = (Path(config.__file__).parent / "nodes" / "node_04a_import_video.py").read_text(
        encoding="utf-8"
    )
    # 去掉模块 docstring(里面为了说明原因会提到这些词)再查真正的 import 语句
    parts = src.split('"""')
    body = parts[2] if len(parts) > 2 else src
    # 反向断言:body 必须真的截到了 import 区,否则下面的检查会"空过"
    assert "import config" in body, "docstring 剥离失败,本用例将失去意义"
    for forbidden in ("langgraph", "pydantic", "storyline_capabilities"):
        assert forbidden not in body, (
            f"node_04a_import_video.py 不得依赖 {forbidden}(跨 venv 硬约束)"
        )
