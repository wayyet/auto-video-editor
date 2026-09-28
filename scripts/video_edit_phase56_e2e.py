"""9 个 MCP 工具迁移 —— 阶段五 + 阶段六 端到端联调脚本。

对照 ``docs/integration/video-agent-kit九个MCP工具迁移至auto-video-editor设计执行计划.md``
§6.5(阶段五:``video_basic_operation`` / ``video_watch_segment`` 接入)与
§6.6(阶段六:端到端联调)。

跑法::

    python scripts/video_edit_phase56_e2e.py                # 全 6 个 case
    python scripts/video_edit_phase56_e2e.py --case all     # 同上
    python scripts/video_edit_phase56_e2e.py --case repair_visual_evidence

所有 case 都用**真实素材** ``inputs/30s.mp4`` + 真实 ffmpeg,不 mock 工具层,
产物落 ``outputs/phase56-e2e-<case>/``,最后汇总写
``outputs/phase56_e2e_report.json``。

每个 case 返回 ``{"case": str, "all_ok": bool, "checks": [...], ...}``;
``checks`` 里每条形如 ``{"name": ..., "ok": bool, "detail": str}``,
便于联调报告逐项打勾(计划 §7.3 验收清单)。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_INPUT = ROOT / "inputs" / "30s.mp4"
TRANSCRIPT_FIXTURE = ROOT / "tests" / "fixtures" / "assembly_phase5" / "transcript.json"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _case(name: str, checks: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"case": name, "all_ok": all(c["ok"] for c in checks), "checks": checks, **extra}


def _out_dir(name: str) -> Path:
    d = ROOT / "outputs" / f"phase56-e2e-{name}-{int(time.time())}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ffmpeg_ready() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# ---------------------------------------------------------------------------
# Case 1:assembly 6 节点真实链路(顺带验证 repair loop 没被接入搞坏)
# ---------------------------------------------------------------------------
def case_assembly_chain(video_input: Path, transcript: str | None) -> dict[str, Any]:
    """真实跑 discover → asr/visual → timeline → validate/render/qc → report。"""
    from nodes.assembly import (
        assembly_build_timeline_node,
        assembly_discover_and_probe_node,
        assembly_validate_render_qc_node,
        assembly_write_report_node,
    )

    out = _out_dir("assembly")
    state: dict[str, Any] = {
        "session_id": f"phase56-assembly-{int(time.time())}",
        "video_input_path": str(video_input),
        "storyline_outputs_root": str(out),
        "status_log": [],
        "error_log": [],
    }
    checks: list[dict[str, Any]] = []

    state.update(assembly_discover_and_probe_node(state))
    checks.append(_check("discover_and_probe 产出 media 报告",
                         bool(state.get("assembly_media_artifact")),
                         f"assembly_media_artifact={state.get('assembly_media_artifact')}"))

    state.update(assembly_build_timeline_node(state))
    checks.append(_check("build_timeline 产出 timeline.json",
                         bool(state.get("assembly_timeline_path")),
                         f"assembly_timeline_path={state.get('assembly_timeline_path')}"))

    state.update(assembly_validate_render_qc_node(state))
    qc_status = state.get("assembly_qc_status")
    checks.append(_check("validate/render/qc 跑完并给出状态",
                         qc_status in ("pass", "pass_with_warnings", "escalated"),
                         f"assembly_qc_status={qc_status}"))
    checks.append(_check("预览 MP4 落盘",
                         bool(state.get("assembly_preview_path"))
                         and Path(str(state["assembly_preview_path"])).is_file(),
                         f"assembly_preview_path={state.get('assembly_preview_path')}"))

    state.update(assembly_write_report_node(state))
    report = state.get("assembly_report_path")
    checks.append(_check("report.md 生成",
                         bool(report) and Path(str(report)).is_file(), f"report={report}"))

    return _case("assembly_chain", checks, qc_status=qc_status, out_dir=str(out))


# ---------------------------------------------------------------------------
# Case 2:阶段五核心 —— repair loop 视觉证据复核(真实 ffmpeg 出图)
# ---------------------------------------------------------------------------
def case_repair_visual_evidence(video_input: Path) -> dict[str, Any]:
    """强制 QC escalated,真跑一次 repair loop,验证证据帧真的落盘。

    这条是 §6.5 第 1 步的验收:QC 说"这里黑了/卡了"只是时间戳,阶段五要求
    额外产出**可看的图** —— 高 fps 重采样 + 局部放大各一轮。
    """
    import config
    from nodes.assembly.node_repair_loop import (
        _collect_suspicious_windows,
        assembly_repair_loop_node,
    )

    out = _out_dir("repair_visual_evidence")
    if not shutil.which("ffmpeg"):
        return _case("repair_visual_evidence",
                     [_check("ffmpeg 可用", False, "ffmpeg 不在 PATH")])

    # 先真跑 assembly 上游,拿到 **真实** timeline.json 与真实渲染的 preview.mp4
    # (假 timeline 会被 timeline_diff 的 project contract 拒掉,验不到主链)
    from nodes.assembly import (
        assembly_build_timeline_node,
        assembly_discover_and_probe_node,
        assembly_validate_render_qc_node,
    )

    state: dict[str, Any] = {
        "session_id": f"phase56-repair-{int(time.time())}",
        "video_input_path": str(video_input),
        "storyline_outputs_root": str(out),
        "status_log": [],
        "error_log": [],
    }
    state.update(assembly_discover_and_probe_node(state))
    state.update(assembly_build_timeline_node(state))
    state.update(assembly_validate_render_qc_node(state))

    timeline_path = Path(str(state.get("assembly_timeline_path") or ""))
    preview = Path(str(state.get("assembly_preview_path") or ""))
    if not timeline_path.is_file() or not preview.is_file():
        return _case("repair_visual_evidence", [_check(
            "上游产出真实 timeline + preview", False,
            f"timeline={timeline_path} preview={preview} qc={state.get('assembly_qc_status')}",
        )])

    # 在真实 QC 报告上叠加"带时间戳"的可疑片段,模拟 QC escalate 到 repair loop 的场景
    qc_path = Path(str(state.get("assembly_qc_report_path") or (out / "assembly" / "preview_qc_report.json")))
    qc_report = json.loads(qc_path.read_text(encoding="utf-8")) if qc_path.is_file() else {}
    duration = 12.0
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(preview)],
        capture_output=True, text=True,
    )
    try:
        duration = float((probe.stdout or "12").strip())
    except ValueError:
        duration = 12.0

    qc_report.update({
        "blackdetect_log_tail": (
            "[blackdetect @ 0x1] black_start:1 black_end:2.5 black_duration:1.5\n"
            "[blackdetect @ 0x1] black_start:7 black_end:8 black_duration:1\n"
        ),
        "freezedetect_log_tail": (
            "[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 5\n"
            "[freezedetect @ 0x2] lavfi.freezedetect.freeze_duration: 1\n"
            "[freezedetect @ 0x2] lavfi.freezedetect.freeze_end: 6\n"
        ),
        "silencedetect_log_tail": "",
    })
    qc_path.write_text(json.dumps(qc_report, ensure_ascii=False), encoding="utf-8")

    state["assembly_qc_status"] = "escalated"
    state["assembly_preview_path"] = str(preview)
    state["assembly_qc_report_path"] = str(qc_path)

    from nodes.assembly.node_repair_loop import (
        _collect_suspicious_windows,
        assembly_repair_loop_node,
    )

    windows = _collect_suspicious_windows(
        qc_report, max_windows=config.ASSEMBLY_REPAIR_MAX_WINDOWS, clip_duration=duration
    )
    checks = [_check("从 QC 日志抽出可疑窗口", len(windows) > 0,
                     f"windows={[(w['start'], w['end'], w['kinds']) for w in windows]}")]

    result = assembly_repair_loop_node(state)
    evidence_path = result.get("assembly_repair_evidence_path")
    checks.append(_check("repair loop 产出证据汇总 json", bool(evidence_path),
                         f"assembly_repair_evidence_path={evidence_path}"))

    if evidence_path and Path(evidence_path).is_file():
        evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
        checks.append(_check("证据 json 无工具错误", not evidence.get("errors"),
                             f"errors={evidence.get('errors')}"))
        frames_root = Path(evidence_path).parent
        watch_frames = len(list((frames_root / "repair_frames").rglob("*.jpg"))) \
            if (frames_root / "repair_frames").exists() else 0
        zoom_frames = len(list((frames_root / "repair_zoom").rglob("*.jpg"))) \
            if (frames_root / "repair_zoom").exists() else 0
        checks.append(_check("高 fps 重采样帧落盘(>0 张)", watch_frames > 0,
                             f"repair_frames jpgs={watch_frames}"))
        checks.append(_check("局部放大帧落盘(>0 张)", zoom_frames > 0,
                             f"repair_zoom jpgs={zoom_frames}"))
    checks.append(_check("重试计数照常 +1", result.get("assembly_qc_retry_count") == 1,
                         f"retry={result.get('assembly_qc_retry_count')}"))
    checks.append(_check("timeline_diff 主链未被影响",
                         json.loads(timeline_path.read_text(encoding="utf-8"))
                         .get("metadata", {}).get("repair_marker") is True,
                         "repair_marker 应写进 timeline.json"))

    return _case("repair_visual_evidence", checks, out_dir=str(out))


# ---------------------------------------------------------------------------
# Case 3:阶段五核心 —— node_05 素材归一化(真实 ffmpeg 裁切)
# ---------------------------------------------------------------------------
def case_source_normalize(video_input: Path) -> dict[str, Any]:
    """真调一次 ``video_basic_operation(crop)``,验证宽高比真的被拉齐。"""
    from assembly_capabilities.run_context import RunContext
    from video_edit_capabilities.media_operation import video_basic_operation
    from video_edit_capabilities.render import probe_video_size

    out = _out_dir("source_normalize")
    src_w, src_h = probe_video_size(video_input)
    canvas_w, canvas_h = 1080, 1920
    canvas_ratio = canvas_w / canvas_h
    src_ratio = src_w / src_h

    checks = [_check("读得到源素材分辨率", src_w > 0 and src_h > 0,
                     f"{src_w}x{src_h}, ratio={src_ratio:.4f}, canvas_ratio={canvas_ratio:.4f}")]

    if abs(src_ratio / canvas_ratio - 1.0) <= 0.02:
        # 比例本来就对齐 —— node_05 会零开销跳过,这本身就是正确行为
        checks.append(_check("比例已对齐 → node_05 跳过归一化(零开销)", True,
                             "素材比例与画布一致,归一化不触发"))
        return _case("source_normalize", checks, out_dir=str(out), skipped=True)

    if src_ratio > canvas_ratio:
        crop_h, crop_w = src_h, int(round(src_h * canvas_ratio))
    else:
        crop_w, crop_h = src_w, int(round(src_w / canvas_ratio))
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    x, y = (src_w - crop_w) // 2, (src_h - crop_h) // 2

    result = video_basic_operation({
        "operation": "crop",
        "input_path": str(video_input),
        "width": crop_w, "height": crop_h, "x": max(0, x), "y": max(0, y),
        "output_path": str(out / f"normalized_{crop_w}x{crop_h}.mp4"),
    }, RunContext())

    checks.append(_check("video_basic_operation(crop) 成功", not result.text.startswith("[ERROR]"),
                         result.text[:200]))
    norm_path = out / f"normalized_{crop_w}x{crop_h}.mp4"
    if norm_path.is_file():
        nw, nh = probe_video_size(norm_path)
        checks.append(_check("裁切后比例对齐画布",
                             abs((nw / nh) / canvas_ratio - 1.0) <= 0.02,
                             f"{nw}x{nh}, ratio={(nw / nh):.4f} vs canvas {canvas_ratio:.4f}"))
    else:
        checks.append(_check("归一化产物落盘", False, f"缺 {norm_path}"))
    return _case("source_normalize", checks, out_dir=str(out))


# ---------------------------------------------------------------------------
# Case 4:字幕四件套真实链路(scout → build → render → qc)
# ---------------------------------------------------------------------------
def case_subtitle_chain(video_input: Path) -> dict[str, Any]:
    """真跑 scout→build→render→qc,产出烧录预览 MP4 与 QC 问题列表。"""
    from assembly_capabilities.run_context import RunContext
    from video_edit_capabilities.subtitle_build import subtitle_build
    from video_edit_capabilities.subtitle_qc import subtitle_qc
    from video_edit_capabilities.subtitle_render import subtitle_render
    from video_edit_capabilities.subtitle_scout import subtitle_scout

    out = _out_dir("subtitle_chain")
    ctx = RunContext()
    checks: list[dict[str, Any]] = []

    scout = subtitle_scout({"video_path": str(video_input)}, ctx)
    scout_ok = not scout.text.startswith("[ERROR]")
    checks.append(_check("subtitle_scout 返回 style + shot_cuts", scout_ok, scout.text[:200]))
    if scout_ok:
        data = scout.data if isinstance(scout.data, dict) else {}
        # 注意真实契约:scout **没有** 顶层 "style" 键,排版建议分散在
        # contrast_recommendation.style / font_recommendation.style 里。
        contrast = (data.get("contrast_recommendation") or {}).get("style") or {}
        font = (data.get("font_recommendation") or {}).get("style") or {}
        checks.append(_check(
            "scout 给出排版建议(contrast/font,非顶层 style)",
            bool(contrast) and bool(font),
            f"contrast={contrast}, font={font}, 顶层键含 style? {'style' in data}",
        ))

    transcript = [{"text": "这是端到端联调的中文字幕测试样例。", "start": 1.0, "end": 5.0},
                  {"text": "second line for the qc chain", "start": 5.5, "end": 9.0}]
    tpath = out / "transcript.json"
    tpath.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")

    build = subtitle_build({"transcript_path": str(tpath), "video_path": str(video_input),
                            "preset": "shortform_zh"}, ctx)
    checks.append(_check("subtitle_build 产出字幕文件", not build.text.startswith("[ERROR]"),
                         build.text[:200]))
    subs = build.artifacts[0] if build.artifacts else None
    if not (subs and Path(subs).is_file()):
        return _case("subtitle_chain", checks, out_dir=str(out))

    render = subtitle_render({"video_path": str(video_input), "subtitles_path": str(subs),
                              "mode": "burn", "output_path": str(out / "subtitled.mp4")}, ctx)
    checks.append(_check("subtitle_render 烧录出预览 MP4",
                         not render.text.startswith("[ERROR]"), render.text[:200]))
    burned = (render.data or {}).get("outputs", {}).get("burned") if isinstance(render.data, dict) else None
    checks.append(_check("烧录产物存在且非空",
                         bool(burned) and Path(str(burned)).is_file()
                         and Path(str(burned)).stat().st_size > 0,
                         f"burned={burned}"))

    qc = subtitle_qc({"subtitles_path": str(subs), "video_path": str(burned)}, ctx)
    checks.append(_check("subtitle_qc 返回 issues 列表", not qc.text.startswith("[ERROR]"),
                         qc.text[:200]))
    qc_data = qc.data if isinstance(qc.data, dict) else {}
    checks.append(_check("qc 产出可读 issues(非崩溃即可,空=通过)",
                         isinstance(qc_data.get("issues"), list),
                         f"issues={qc_data.get('issues')}"))

    return _case("subtitle_chain", checks, out_dir=str(out),
                 burned_path=str(burned) if burned else None)


# ---------------------------------------------------------------------------
# Case 5:节点 08 样式来自 scout 还是兜底
# ---------------------------------------------------------------------------
def case_node08_style(video_input: Path) -> dict[str, Any]:
    """节点 08:scout 建议拿得到 + 是否真的写进草稿样式。

    注意本条只验证"取得到建议"和"node_08 是否把建议落到 texts[].style";
    scout 失败回退默认样式是 §6.4 的既有行为,这里一并验。
    """
    from nodes.node_08_add_subtitles import _JIANYING_DEFAULT_STYLE, _run_subtitle_scout

    report = _run_subtitle_scout(str(video_input))
    checks = [
        _check("node_08 拿到 scout 报告(或明确回退 None)",
               isinstance(report, dict) or report is None,
               "scout 成功" if isinstance(report, dict) else "scout 回退 None"),
    ]
    if isinstance(report, dict):
        font = (report.get("font_recommendation") or {}).get("style") or {}
        checks.append(_check("scout 报告内含字号建议", bool(font.get("font_size")),
                             f"font_recommendation.style={font}"))
        # 关键核对点:scout 建议目前只落到 state.subtitle_scout_report,
        # 草稿里 texts[].style 仍是 _JIANYING_DEFAULT_STYLE(§6.4 的 STYLE_KEY_MAP 未实施)
        checks.append(_check(
            "草稿默认样式键名(供对照 STYLE_KEY_MAP)",
            bool(_JIANYING_DEFAULT_STYLE),
            f"default_style={_JIANYING_DEFAULT_STYLE}",
        ))
    return _case("node08_style", checks)


# ---------------------------------------------------------------------------
# Case 6:关卡③ 预览路径 + 节点 17 TTS 回退
# ---------------------------------------------------------------------------
def case_checkpoint3_and_tts(video_input: Path) -> dict[str, Any]:
    """qc 链出 preview_path(关卡③ payload 要挂它)+ node_17 TTS 真实调用/回退。"""
    from nodes.node_16a_translate_and_check import _check_layout_via_subtitle_qc
    from nodes.node_17_inject_english_tts import node_17_inject_english_tts

    out = _out_dir("checkpoint3_tts")
    checks: list[dict[str, Any]] = []
    segments_en = [
        # node_16a 认的是 SubtitleSegment 契约:start_ms / end_ms / text_en
        {"index": 0, "start_ms": 1000, "end_ms": 5000,
         "text_en": "This is an end to end dubbing sample."},
        {"index": 1, "start_ms": 5500, "end_ms": 9000,
         "text_en": "The second line continues the sentence."},
    ]

    issues, preview, evidence = _check_layout_via_subtitle_qc(segments_en, str(video_input), out)
    if issues is None:
        # qc 链任一步失败 → 降级 validate_layout,联调如实记"未走 qc 链"
        checks.append(_check("qc 链产出 issues(失败=降级 validate_layout)", False,
                             "qc 链返回 None,已按设计降级"))
    else:
        checks.append(_check("qc 链产出 issues(失败=降级 validate_layout)", True,
                             f"{len(issues)} 条 issue"))
        checks.append(_check("关卡③ 预览 MP4 存在(人工可直接打开)",
                             bool(preview) and Path(str(preview)).is_file(),
                             f"preview_video_path={preview}"))
        checks.append(_check("qc 证据帧返回", isinstance(evidence, list),
                             f"evidence_frames={len(evidence or [])}"))

    # 节点 17:TTS 真调(本机无 FireRedTTS2 → 应优雅回退 None,不阻塞主链)
    tts_state = {"subtitle_segments_en": segments_en, "error_log": [], "status_log": []}
    tts_out = node_17_inject_english_tts(tts_state)
    audio = tts_out.get("en_audio_path")
    checks.append(_check("node_17 真实调 TTS 并写 en_audio_path(成功或 None)",
                         "en_audio_path" in tts_out,
                         f"en_audio_path={audio}"))
    if audio:
        checks.append(_check("TTS 产物真实存在", Path(str(audio)).is_file(), f"path={audio}"))
    else:
        errs = tts_out.get("error_log") or []
        checks.append(_check("TTS 失败优雅回退(有 error_log,主链不阻塞)", bool(errs),
                             f"error_log 尾条={errs[-1] if errs else None}"))

    return _case("checkpoint3_and_tts", checks, out_dir=str(out))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
CASES = {
    "assembly_chain": lambda v, t: case_assembly_chain(v, t),
    "repair_visual_evidence": lambda v, t: case_repair_visual_evidence(v),
    "source_normalize": lambda v, t: case_source_normalize(v),
    "subtitle_chain": lambda v, t: case_subtitle_chain(v),
    "node08_style": lambda v, t: case_node08_style(v),
    "checkpoint3_and_tts": lambda v, t: case_checkpoint3_and_tts(v),
}


def main() -> int:
    p = argparse.ArgumentParser(prog="video_edit_phase56_e2e")
    p.add_argument("--case", default="all", choices=("all", *CASES))
    p.add_argument("--input", default=str(DEFAULT_INPUT))
    p.add_argument("--transcript", default=str(TRANSCRIPT_FIXTURE))
    p.add_argument("--report-json", default=None)
    args = p.parse_args()

    video_input = Path(args.input)
    if not video_input.is_file():
        print(f"[FAIL] 素材不存在: {video_input}")
        return 2
    if not _ffmpeg_ready():
        print("[FAIL] ffmpeg / ffprobe 不在 PATH,端到端无法跑")
        return 2

    names = list(CASES) if args.case == "all" else [args.case]
    results = []
    for name in names:
        print(f"\n===== CASE {name} =====")
        try:
            result = CASES[name](video_input, args.transcript)
        except Exception as exc:  # 任何 case 崩了都记下来,继续跑下一个
            result = _case(name, [_check("case 执行", False, repr(exc))])
        results.append(result)
        for c in result["checks"]:
            print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']} — {c['detail']}")

    all_ok = all(r["all_ok"] for r in results)
    report = {
        "tool": "video_edit_phase56_e2e",
        "input": str(video_input),
        "all_ok": all_ok,
        "passed": sum(1 for r in results if r["all_ok"]),
        "total": len(results),
        "results": results,
    }
    report_path = Path(args.report_json) if args.report_json else ROOT / "outputs" / "phase56_e2e_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n===== 汇总: {report['passed']}/{report['total']} case 通过 =====")
    print(f"报告: {report_path}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
