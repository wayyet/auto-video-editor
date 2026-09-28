"""阶段五(§6.5)单测 —— ``assembly_repair_loop`` 的视觉证据复核。

对应计划 §6.5 第 1 步:QC escalate 时对可疑片段做高 fps 重采样 +
局部放大。覆盖两层:

1. **纯函数层** ``_merge_windows`` / ``_collect_suspicious_windows``
   —— 把 QC 报告里的 blackdetect / silencedetect / freezedetect 日志翻成
   时间窗口,并保证不越 ``video_watch_segment`` 的入参上限。
2. **节点层** ``assembly_repair_loop_node``
   —— 开关、force 重看、工具失败降级、证据落盘、state 字段。

设计纪律(与实现一一对应):视觉复核是**旁路**,任何失败都不许影响
``timeline_diff`` 与重试计数(计划 §5.4)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from assembly_capabilities.result import ToolResult
from nodes.assembly.node_repair_loop import (
    _collect_suspicious_windows,
    _merge_windows,
    assembly_repair_loop_node,
)


# 本文件**刻意不写** ``from .conftest import ...``。同目录的
# ``test_assembly_nodes.py`` 用了相对导入,一旦本文件先进了
# ``sys.modules``,pytest 就不再把 ``tests/unit/nodes/conftest.py`` 注册成插件,
# 结果兄弟模块的 ``base_state`` / ``mock_assembly_capabilities`` fixture 全部找不到。
# 这里内联一份最小 timeline,保持本文件零 conftest 依赖。
def _minimal_timeline(source: str = "x.mp4") -> dict:
    return {
        "project": {"name": "test"},
        "assets": [{"id": "a1", "path": source, "duration": 0.0,
                    "width": 16, "height": 16, "fps": 30}],
        "sequence": {"duration": 0.0, "fps": 30,
                     "canvas": {"width": 16, "height": 16, "fps": 30}},
        "tracks": [{"type": "video", "name": "v1", "clips": []}],
    }

# ---------------------------------------------------------------------------
# ffmpeg 日志样本(逐字取自真实 blackdetect / freezedetect 输出格式)
# ---------------------------------------------------------------------------
BLACK_LOG = (
    "[blackdetect @ 000001f0] black_start:2.5 black_end:4.5 black_duration:2\n"
    "[blackdetect @ 000001f0] black_start:20 black_end:21.25 black_duration:1.25\n"
)
FREEZE_LOG = (
    "[freezedetect @ 000001f1] lavfi.freezedetect.freeze_start: 12\n"
    "[freezedetect @ 000001f1] lavfi.freezedetect.freeze_duration: 1.5\n"
    "[freezedetect @ 000001f1] lavfi.freezedetect.freeze_end: 13.5\n"
)
SILENCE_LOG = (
    "[silencedetect @ 000001f2] silence_start: 8\n"
    "[silencedetect @ 000001f2] silence_end: 9.5 | silence_duration: 1.5\n"
)


def _fake_watch(calls: list[dict]):
    def _impl(args, ctx):
        calls.append(args)
        return ToolResult(
            text="watched",
            data={"segments": [{"start_time": s["start"], "end_time": s["end"]} for s in args["segments"]]},
        )

    return _impl


def _fake_read_frames(calls: list[dict]):
    def _impl(args, ctx):
        calls.append(args)
        return ToolResult(
            text="read frames",
            data={
                "frames": [
                    {"timestamp": t, "path": f"frame_{t}.jpg", "size": [1080, 1920]}
                    for t in args["timestamps"]
                ]
            },
        )

    return _impl


def _seed(state: dict, tmp_path: Path, *, qc_report: dict | None, preview: bool = True) -> None:
    """给 state 塞一份能触发视觉复核的 timeline / preview / qc_report。"""
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps(_minimal_timeline()), encoding="utf-8")
    state["assembly_timeline_path"] = str(timeline_path)

    preview_path = tmp_path / "preview.mp4"
    if preview:
        preview_path.write_bytes(b"\x00" * 512)
        state["assembly_preview_path"] = str(preview_path)

    if qc_report is not None:
        qc_path = tmp_path / "preview_qc_report.json"
        qc_path.write_text(json.dumps(qc_report), encoding="utf-8")
        state["assembly_qc_report_path"] = str(qc_path)


# ===========================================================================
# 纯函数层:窗口抽取
# ===========================================================================
def test_collect_windows_parses_blackdetect_and_pads():
    """blackdetect 报的 [2.5, 4.5] 应外扩 0.5s → [2.0, 5.0]。

    外扩是为了让人一眼看出"从哪一帧开始坏的",而不只是拍到黑帧本体。
    """
    windows = _collect_suspicious_windows(
        {"blackdetect_log_tail": BLACK_LOG}, max_windows=3, clip_duration=30.0
    )
    assert windows == [
        {"start": 2.0, "end": 5.0, "duration": 3.0, "kinds": ["black"]},
        {"start": 19.5, "end": 21.75, "duration": 2.25, "kinds": ["black"]},
    ]


def test_collect_windows_parses_freezedetect_prefixed_marker():
    """freezedetect 的 marker 带 ``lavfi.freezedetect.`` 前缀,仍要能解析出来。"""
    windows = _collect_suspicious_windows(
        {"freezedetect_log_tail": FREEZE_LOG}, max_windows=3, clip_duration=30.0
    )
    assert len(windows) == 1
    assert windows[0]["kinds"] == ["freeze"]
    assert windows[0]["start"] == 11.5 and windows[0]["end"] == 14.0


def test_collect_windows_parses_silencedetect():
    windows = _collect_suspicious_windows(
        {"silencedetect_log_tail": SILENCE_LOG}, max_windows=3, clip_duration=30.0
    )
    assert windows[0]["kinds"] == ["silence"]
    assert (windows[0]["start"], windows[0]["end"]) == (7.5, 10.0)


def test_merge_windows_merges_overlap_and_unions_kinds():
    merged = _merge_windows(
        [
            {"start": 2.0, "end": 4.0, "kinds": ["black"]},
            {"start": 3.8, "end": 6.0, "kinds": ["freeze"]},
        ]
    )
    assert merged == [{"start": 2.0, "end": 6.0, "duration": 4.0, "kinds": ["black", "freeze"]}]


def test_collect_windows_merges_same_segment_across_kinds():
    """同一段既是黑帧又是卡帧 → 只复核一次,不重复调 ffmpeg。"""
    overlapping_freeze = (
        "[freezedetect @ 000001f1] lavfi.freezedetect.freeze_start: 3\n"
        "[freezedetect @ 000001f1] lavfi.freezedetect.freeze_duration: 1\n"
        "[freezedetect @ 000001f1] lavfi.freezedetect.freeze_end: 4\n"
    )
    windows = _collect_suspicious_windows(
        {"blackdetect_log_tail": BLACK_LOG, "freezedetect_log_tail": overlapping_freeze},
        max_windows=3,
        clip_duration=30.0,
    )
    kinds = {tuple(w["kinds"]) for w in windows}
    assert ("black", "freeze") in kinds
    # 黑帧两段 + 与第一段重叠的卡帧段 → 仍是 2 个窗口,不是 3 个
    assert len(windows) == 2


def test_collect_windows_respects_max_windows():
    windows = _collect_suspicious_windows(
        {"blackdetect_log_tail": BLACK_LOG}, max_windows=1, clip_duration=30.0
    )
    assert len(windows) == 1


def test_collect_windows_empty_without_timestamp_issues():
    """只有"音量偏低"这类无时间戳 issue → 不复核(没有可疑片段)。"""
    assert _collect_suspicious_windows({"issues": [{"severity": "warning", "message": "low volume"}]}, max_windows=3) == []
    assert _collect_suspicious_windows({}, max_windows=3) == []


def test_collect_windows_clamps_to_clip_duration():
    """窗口延伸到素材之外时按素材总时长裁掉,否则 ffmpeg 采样会 seek 越界。"""
    windows = _collect_suspicious_windows(
        {"blackdetect_log_tail": BLACK_LOG}, max_windows=3, clip_duration=5.0
    )
    assert windows[0]["end"] <= 5.0
    assert windows[0]["end"] > windows[0]["start"]


def test_collect_windows_never_exceeds_tool_segment_cap():
    """超长黑帧段(>60s)必须截到 MAX_SEGMENT_SECONDS,否则工具直接 [ERROR]。"""
    from video_edit_capabilities.visual_evidence import MAX_SEGMENT_SECONDS

    long_log = "[blackdetect @ x] black_start:0 black_end:600 black_duration:600\n"
    windows = _collect_suspicious_windows(
        {"blackdetect_log_tail": long_log}, max_windows=3, clip_duration=600.0
    )
    assert windows[0]["duration"] <= MAX_SEGMENT_SECONDS


# ===========================================================================
# 节点层:开关 / 落盘 / 降级
# ===========================================================================
def test_repair_loop_writes_visual_evidence(
    mock_assembly_capabilities, base_state, tmp_path, monkeypatch
):
    from nodes.assembly import node_repair_loop as mod

    watch_calls: list[dict] = []
    read_calls: list[dict] = []
    monkeypatch.setattr(mod, "video_watch_segment", _fake_watch(watch_calls))
    monkeypatch.setattr(mod, "video_read_frames", _fake_read_frames(read_calls))
    _seed(base_state, tmp_path, qc_report={"blackdetect_log_tail": BLACK_LOG})

    out = assembly_repair_loop_node(base_state)

    assert out["assembly_qc_retry_count"] == 1
    assert out["assembly_repair_evidence_path"], "必须落证据 json"
    assert "assembly_repair_visual_evidence_done" in out["status_log"]
    # 两级复核都调到了
    assert len(watch_calls) == 1 and len(read_calls) == 1
    # 关键:必须 force —— 否则第 2 轮修复窗口相同时会被 ledger 判成"已看过"
    assert watch_calls[0]["force"] is True
    assert watch_calls[0]["fps"] == mod.config.ASSEMBLY_REPAIR_WATCH_FPS
    assert read_calls[0]["upscale"] == 2.0

    evidence = json.loads(Path(out["assembly_repair_evidence_path"]).read_text(encoding="utf-8"))
    assert evidence["tool"] == "assembly_repair_loop.visual_evidence"
    assert len(evidence["windows"]) == 2
    assert evidence["windows"][0]["kinds"] == ["black"]
    assert evidence["errors"] == []


def test_repair_loop_visual_evidence_disabled(
    mock_assembly_capabilities, base_state, tmp_path, monkeypatch
):
    import config
    from nodes.assembly import node_repair_loop as mod

    watch_calls: list[dict] = []
    monkeypatch.setattr(config, "ASSEMBLY_REPAIR_VISUAL_EVIDENCE", False)
    monkeypatch.setattr(mod, "video_watch_segment", _fake_watch(watch_calls))
    _seed(base_state, tmp_path, qc_report={"blackdetect_log_tail": BLACK_LOG})

    out = assembly_repair_loop_node(base_state)

    assert watch_calls == [], "开关关闭时不应调用视觉工具"
    assert out["assembly_repair_evidence_path"] is None
    # 主链不受影响
    assert out["assembly_qc_retry_count"] == 1
    assert "assembly_repair_loop_done" in out["status_log"]


def test_repair_loop_survives_visual_tool_failure(
    mock_assembly_capabilities, base_state, tmp_path, monkeypatch
):
    """视觉工具全炸 → 证据文件仍落盘 + 错误入 error_log + 主链照跑(计划 §5.4)。"""
    from nodes.assembly import node_repair_loop as mod

    monkeypatch.setattr(
        mod, "video_watch_segment", lambda args, ctx: ToolResult(text="[ERROR] ffmpeg not found on PATH")
    )
    monkeypatch.setattr(mod, "video_read_frames", lambda args, ctx: ToolResult(text="[ERROR] boom"))
    _seed(base_state, tmp_path, qc_report={"blackdetect_log_tail": BLACK_LOG})

    out = assembly_repair_loop_node(base_state)

    assert out["assembly_qc_retry_count"] == 1, "视觉复核失败不得影响重试计数"
    joined = " ".join(out["error_log"])
    assert "视觉复核降级" in joined and "ffmpeg not found" in joined
    evidence = json.loads(Path(out["assembly_repair_evidence_path"]).read_text(encoding="utf-8"))
    assert len(evidence["errors"]) == 2


def test_repair_loop_survives_visual_tool_exception(
    mock_assembly_capabilities, base_state, tmp_path, monkeypatch
):
    from nodes.assembly import node_repair_loop as mod

    def _boom(args, ctx):
        raise RuntimeError("PIL missing")

    monkeypatch.setattr(mod, "video_watch_segment", _boom)
    _seed(base_state, tmp_path, qc_report={"blackdetect_log_tail": BLACK_LOG})

    out = assembly_repair_loop_node(base_state)

    assert out["assembly_qc_retry_count"] == 1
    assert any("视觉复核异常" in e for e in out["error_log"])
    assert out["assembly_repair_evidence_path"] is None


def test_repair_loop_skips_when_no_suspicious_windows(
    mock_assembly_capabilities, base_state, tmp_path, monkeypatch
):
    """QC 干净(无时间戳 issue)→ 不调工具,写明原因。"""
    from nodes.assembly import node_repair_loop as mod

    watch_calls: list[dict] = []
    monkeypatch.setattr(mod, "video_watch_segment", _fake_watch(watch_calls))
    _seed(base_state, tmp_path, qc_report={"issues": [], "blackdetect_log_tail": ""})

    out = assembly_repair_loop_node(base_state)

    assert watch_calls == []
    assert out["assembly_repair_evidence_path"] is None
    assert any("没有时间戳类可疑片段" in e for e in out["error_log"])


def test_repair_loop_skips_when_preview_missing(
    mock_assembly_capabilities, base_state, tmp_path, monkeypatch
):
    """预览片不在(render 就失败了)→ 跳过复核,不硬调工具。"""
    from nodes.assembly import node_repair_loop as mod

    watch_calls: list[dict] = []
    monkeypatch.setattr(mod, "video_watch_segment", _fake_watch(watch_calls))
    _seed(base_state, tmp_path, qc_report={"blackdetect_log_tail": BLACK_LOG}, preview=False)

    out = assembly_repair_loop_node(base_state)

    assert watch_calls == []
    assert any("预览视频不存在" in e for e in out["error_log"])
    assert out["assembly_qc_retry_count"] == 1


def test_repair_loop_still_writes_timeline_marker(
    mock_assembly_capabilities, base_state, tmp_path, monkeypatch
):
    """阶段五接入不得改变阶段五之前的行为:timeline_diff 仍要写 repair_marker。"""
    from nodes.assembly import node_repair_loop as mod

    monkeypatch.setattr(mod, "video_watch_segment", _fake_watch([]))
    monkeypatch.setattr(mod, "video_read_frames", _fake_read_frames([]))
    _seed(base_state, tmp_path, qc_report={"blackdetect_log_tail": BLACK_LOG})

    assembly_repair_loop_node(base_state)

    written = json.loads((tmp_path / "timeline.json").read_text(encoding="utf-8"))
    assert written["metadata"]["repair_marker"] is True
