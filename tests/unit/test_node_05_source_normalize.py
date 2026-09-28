"""阶段五(§6.5)单测 —— ``node_05_generate_draft`` 的素材宽高比归一化。

对应计划 §6.5 第 2 步:混剪素材宽高比不一致时先用
``video_basic_operation`` 裁切,再进草稿。

覆盖:
1. **零开销路径**:开关关闭 / 文件不存在 / 比例已对齐 → 原样放行,不碰 ffmpeg
2. **裁切参数**:横屏 / 竖屏 / 极端比例各算出什么 crop 框
3. **降级纪律**:工具报错 / 探测失败 → 退回原素材 + 写 error_log,草照写
4. **契约不破**:草稿 materials 用归一化路径,manifest 仍记**输入**素材路径
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
from assembly_capabilities.result import ToolResult
from draft_ops.encryption_detector import DraftStatus
from nodes import node_05_generate_draft as mod
from nodes.node_05_generate_draft import (
    _normalize_source_video,
    generate_initial_jianying_draft,
)

CANVAS_RATIO = config.CANVAS_WIDTH / config.CANVAS_HEIGHT  # 1080x1920 → 0.5625


@pytest.fixture
def fake_source(tmp_path, monkeypatch):
    """一个"存在但不是真视频"的素材文件 —— ffprobe 一律被 monkeypatch 拦下。"""
    source = tmp_path / "input.mp4"
    source.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(mod, "storyline_outputs_root", lambda: tmp_path / "outputs")
    return source


@pytest.fixture
def no_subprocess_guard(monkeypatch):
    """关掉"剪映是否在跑"的子进程探测,避免测试真的去 spawn tasklist。"""
    monkeypatch.setattr(
        "draft_ops.safe_write_guard._default_windows_proc_query", lambda: [], raising=False
    )


def _fake_probe(size):
    return lambda source: size


def _fake_basic_op(calls, *, text="Video basic operation completed", make_output=True):
    def _impl(args, ctx):
        calls.append(args)
        if text.startswith("[ERROR]"):
            return ToolResult(text=text)
        out = Path(args["output_path"])
        if make_output:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00" * 32)
        return ToolResult(
            text=text,
            data={"operation": args["operation"], "output_path": str(out)},
            artifacts=[str(out)],
        )

    return _impl


# ===========================================================================
# 零开销路径
# ===========================================================================
def test_normalize_passthrough_when_disabled(fake_source, monkeypatch):
    monkeypatch.setattr(config, "DRAFT_SOURCE_NORMALIZE_ENABLED", False)
    monkeypatch.setattr(mod, "probe_video_size", _fake_probe((1920, 1080)))
    calls: list = []
    monkeypatch.setattr(mod, "video_basic_operation", _fake_basic_op(calls))

    path, normalized, errors = _normalize_source_video(str(fake_source), job_id="j1")

    assert (path, normalized, errors) == (str(fake_source), None, [])
    assert calls == [], "开关关闭时不得调用 video_basic_operation"


def test_normalize_passthrough_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "storyline_outputs_root", lambda: tmp_path / "outputs")
    calls: list = []
    monkeypatch.setattr(mod, "video_basic_operation", _fake_basic_op(calls))

    missing = str(tmp_path / "nope.mp4")
    path, normalized, errors = _normalize_source_video(missing, job_id="j1")

    assert (path, normalized) == (missing, None)
    assert calls == [], "文件不存在(测试占位 / 远程 URL)时不该调 ffmpeg"


def test_normalize_passthrough_when_ratio_already_matches(fake_source, monkeypatch):
    """比例已对齐(1080x1920)→ 零开销直接放行,这是绝大多数情况。"""
    monkeypatch.setattr(mod, "probe_video_size", _fake_probe((1080, 1920)))
    calls: list = []
    monkeypatch.setattr(mod, "video_basic_operation", _fake_basic_op(calls))

    path, normalized, errors = _normalize_source_video(str(fake_source), job_id="j1")

    assert (path, normalized, errors) == (str(fake_source), None, [])
    assert calls == []


def test_normalize_tolerates_tiny_ratio_drift(fake_source, monkeypatch):
    """1% 以内的比例漂移(编解码亚像素误差)不该触发裁切。"""
    width = int(round(1080 * (CANVAS_RATIO * 1.005 / CANVAS_RATIO)))
    monkeypatch.setattr(mod, "probe_video_size", _fake_probe((1086, 1920)))
    calls: list = []
    monkeypatch.setattr(mod, "video_basic_operation", _fake_basic_op(calls))

    _path, normalized, _errors = _normalize_source_video(str(fake_source), job_id="j1")
    assert normalized is None
    assert calls == []


# ===========================================================================
# 裁切参数
# ===========================================================================
def test_normalize_crops_landscape_source(fake_source, monkeypatch):
    """1920x1080 进 1080x1920 竖屏画布 → 切左右,取 608x1080 居中区域。"""
    monkeypatch.setattr(mod, "probe_video_size", _fake_probe((1920, 1080)))
    calls: list = []
    monkeypatch.setattr(mod, "video_basic_operation", _fake_basic_op(calls))

    path, normalized, errors = _normalize_source_video(str(fake_source), job_id="j1")

    assert errors == []
    assert normalized == path and path.endswith("input_608x1080.mp4")
    args = calls[0]
    assert args["operation"] == "crop"
    assert (args["width"], args["height"]) == (608, 1080)
    assert (args["x"], args["y"]) == ((1920 - 608) // 2, 0)


def test_normalize_crops_tall_source(fake_source, monkeypatch):
    """1440x2560(9:16)进 1080x1920 → 比例一致不该裁。

    1440/2560 = 0.5625 = 画布比例,正好放行。
    """
    monkeypatch.setattr(mod, "probe_video_size", _fake_probe((1440, 2560)))
    calls: list = []
    monkeypatch.setattr(mod, "video_basic_operation", _fake_basic_op(calls))

    _path, normalized, _errors = _normalize_source_video(str(fake_source), job_id="j1")
    assert normalized is None and calls == []


def test_normalize_crops_extra_tall_source(fake_source, monkeypatch):
    """1080x2400(超长竖屏)→ 太高,切上下取 1080x1920。"""
    monkeypatch.setattr(mod, "probe_video_size", _fake_probe((1080, 2400)))
    calls: list = []
    monkeypatch.setattr(mod, "video_basic_operation", _fake_basic_op(calls))

    path, normalized, _errors = _normalize_source_video(str(fake_source), job_id="j1")

    assert normalized == path
    args = calls[0]
    assert (args["width"], args["height"]) == (1080, 1920)
    assert (args["x"], args["y"]) == (0, (2400 - 1920) // 2)


def test_normalize_crop_dims_are_even(fake_source, monkeypatch):
    """yuv420p + libx264 要求偶数边,裁切框不能出奇数。"""
    monkeypatch.setattr(mod, "probe_video_size", _fake_probe((1919, 1081)))
    calls: list = []
    monkeypatch.setattr(mod, "video_basic_operation", _fake_basic_op(calls))

    _normalize_source_video(str(fake_source), job_id="j1")

    args = calls[0]
    assert args["width"] % 2 == 0 and args["height"] % 2 == 0


# ===========================================================================
# 降级纪律(计划 §5.4)
# ===========================================================================
def test_normalize_falls_back_when_tool_errors(fake_source, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mod, "probe_video_size", _fake_probe((1920, 1080)))
    monkeypatch.setattr(
        mod, "video_basic_operation", _fake_basic_op(calls, text="[ERROR] crop operation failed")
    )

    path, normalized, errors = _normalize_source_video(str(fake_source), job_id="j1")

    assert (path, normalized) == (str(fake_source), None), "工具失败必须退回原素材"
    assert errors and "素材归一化失败" in errors[0]


def test_normalize_falls_back_when_probe_fails(fake_source, monkeypatch):
    def _boom(source):
        raise RuntimeError("ffprobe not found on PATH")

    monkeypatch.setattr(mod, "probe_video_size", _boom)
    calls: list = []
    monkeypatch.setattr(mod, "video_basic_operation", _fake_basic_op(calls))

    path, normalized, errors = _normalize_source_video(str(fake_source), job_id="j1")

    assert (path, normalized) == (str(fake_source), None)
    assert errors and "探测分辨率失败" in errors[0]
    assert calls == []


# ===========================================================================
# 节点级:草稿内容 + state + manifest 契约
# ===========================================================================
def _write_state(source: Path) -> dict:
    return {
        "session_id": "j-normalize",
        "video_input_path": str(source),
        "shot_plan": {"shots": [{"id": "s1", "start_s": 0.0, "end_s": 5.0}]},
        "status_log": [],
        "error_log": [],
    }


def test_draft_materials_use_normalized_path(fake_source, monkeypatch, no_subprocess_guard):
    monkeypatch.setattr(mod, "probe_video_size", _fake_probe((1920, 1080)))
    calls: list = []
    monkeypatch.setattr(mod, "video_basic_operation", _fake_basic_op(calls))

    state = _write_state(fake_source)
    out = generate_initial_jianying_draft(
        state, fake_source.parent / "draft",
        encrypt_detector=lambda _d: DraftStatus.PLAINTEXT,
        writer=lambda draft_dir, content: (
            Path(draft_dir, "draft_content.json").write_text(
                json.dumps(content), encoding="utf-8"
            ) or {"jianying_running": False}
        ),
    )

    assert out["draft_source_normalized_path"], "state 必须记下归一化产物路径"
    draft = json.loads(Path(out["draft_path"]).read_text(encoding="utf-8"))
    material_path = draft["materials"]["videos"][0]["path"]
    assert material_path == out["draft_source_normalized_path"], "草稿 materials 必须用裁切后的素材"
    assert material_path != str(fake_source)


def test_manifest_still_records_original_input(fake_source, monkeypatch, no_subprocess_guard):
    """manifest / 幂等键记的是**输入**素材,不能被归一化产物顶掉。"""
    monkeypatch.setattr(mod, "probe_video_size", _fake_probe((1920, 1080)))
    calls: list = []
    monkeypatch.setattr(mod, "video_basic_operation", _fake_basic_op(calls))

    out = generate_initial_jianying_draft(
        _write_state(fake_source), fake_source.parent / "draft",
        encrypt_detector=lambda _d: DraftStatus.PLAINTEXT,
        writer=lambda draft_dir, content: (
            Path(draft_dir, "draft_content.json").write_text(
                json.dumps(content), encoding="utf-8"
            ) or {"jianying_running": False}
        ),
    )

    manifest_path = (
        fake_source.parent / "outputs" / "j-normalize" / "manifest.json"
    )
    assert manifest_path.is_file(), f"manifest 应落在 outputs/j-normalize/,实际未生成: {out['draft_path']}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # manifest 不存裸路径,只存输入素材的 sha256 —— 幂等键建立在这个值上,
    # 拿归一化产物去算会让"同一素材重跑"命中不了幂等。
    from storyline.output_isolation import compute_input_sha256

    assert manifest["input_sha256"] == compute_input_sha256(video_path=str(fake_source))
    assert manifest["input_sha256"] != compute_input_sha256(
        video_path=str(out["draft_source_normalized_path"])
    )


def test_draft_still_written_when_normalize_fails(fake_source, monkeypatch, no_subprocess_guard):
    """归一化失败不阻断草稿生成 —— 退回原素材,只记一条 error_log。"""
    monkeypatch.setattr(mod, "probe_video_size", _fake_probe((1920, 1080)))
    monkeypatch.setattr(
        mod, "video_basic_operation", _fake_basic_op([], text="[ERROR] ffmpeg crash")
    )

    out = generate_initial_jianying_draft(
        _write_state(fake_source), fake_source.parent / "draft",
        encrypt_detector=lambda _d: DraftStatus.PLAINTEXT,
        writer=lambda draft_dir, content: (
            Path(draft_dir, "draft_content.json").write_text(
                json.dumps(content), encoding="utf-8"
            ) or {"jianying_running": False}
        ),
    )

    assert out["draft_path"], "归一化失败也必须写出草稿"
    assert out["draft_source_normalized_path"] is None
    assert any("素材归一化失败" in e for e in out["error_log"])
    draft = json.loads(Path(out["draft_path"]).read_text(encoding="utf-8"))
    assert draft["materials"]["videos"][0]["path"] == str(fake_source)

