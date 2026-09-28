"""节点 5/6:``assembly_repair_loop``(plan §7.5 + 9 工具迁移计划 §6.5)。

对应工具:``timeline_diff``(``apply=True``)。按 ``qc_preview`` /
``validate_timeline`` 返回的问题列表构造 ``patch``,调 ``timeline_diff``
写回 ``timeline.json``,然后回到节点 4 重新走一遍 validate/render/qc。

本节点负责:
1. 构造一个**最小可用 patch**(从 ``preview_qc_report.json`` 的 issues
   抽取"blocking + video 轨";目前只构造 ``update_clips`` 减少
   受影响 clip 的 duration 作为兜底;后续替换为基于 issue 类型的精细
   patch 构造)。
2. 调用 ``timeline_diff`` 写回 ``timeline.json``(``apply=True``)。
3. 增加 ``assembly_qc_retry_count``,供 ``route_after_assembly_qc`` 判断
   是否到 ``ASSEMBLY_QC_MAX_RETRY``。
4. **阶段五(§6.5)视觉证据复核**:QC 说"某处黑了/静音了/卡帧了"只给
   时间戳,人要看图才知道是不是真问题。这里在写 patch 之前先用
   ``video_watch_segment`` 对可疑时间段高 fps 重采样,再用
   ``video_read_frames`` 局部放大抽原分辨率帧,把证据帧落盘。

设计纪律:
- 此节点**不**直接进入 ``assembly_write_report`` —— 它的唯一出口是回到
  ``assembly_validate_render_qc`` 重新跑校验,让 graph 的条件边 + 路由
  函数来统一裁决"重试 or 收尾"。
- **视觉复核是旁路,不是主路**:任何一步失败(ffmpeg 缺失 / 预览文件不在 /
  QC 报告没时间戳 / PIL 缺失)都只写 ``error_log``,绝不影响
  ``timeline_diff`` 与重试计数(计划 §5.4 环境级失败降级兜底纪律)。
- 开关 ``ASSEMBLY_REPAIR_VISUAL_EVIDENCE=False`` → 完全退回阶段五之前的
  纯 ``timeline_diff`` 行为。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assembly_capabilities.qc_preview import parse_filter_ranges
from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext
from assembly_capabilities.timeline_ops import timeline_diff

from nodes.storyline._common import (
    _resolve_outputs_root,
    append_error,
    append_status_tag,
)
from state import WorkflowState
from video_edit_capabilities.visual_evidence import (
    MAX_SEGMENTS,
    MAX_SEGMENT_SECONDS,
    MAX_TOTAL_SECONDS,
    video_read_frames,
    video_watch_segment,
)

import config


def _build_repair_patch(qc_report_path: str | None, validation_report_path: str | None) -> dict[str, Any]:
    """构造最小可用 ``update_clips`` patch。

    阶段三扩展:按 issue 类型构造精细 patch(替换素材 / 删除卡帧 clip /
    调整时长等)。目前仅做"打补丁占位",确保 ``timeline_diff`` 链路畅通。
    """
    # 即使没 issue 也返回一个非空 patch(空 patch + apply=true 会被原版拒绝)
    # 用 ``set_timeline_fields`` 设一个 metadata 标记,确认 repair 链路真跑了
    return {
        "set_timeline_fields": {
            "metadata": {
                "repair_marker": True,
                "qc_report": qc_report_path,
                "validation_report": validation_report_path,
            },
        },
    }


# ---------------------------------------------------------------------------
# 阶段五(§6.5):从 QC 报告抽"可疑时间段"
# ---------------------------------------------------------------------------
# QC 的三类扫描日志落在报告顶层的 ``*_log_tail`` 字段里(各保留末 2000 字符):
#   blackdetect     → "black_start:2.5 black_end:4.5 black_duration:2"
#   silencedetect   → "silence_start: 3 | silence_duration: 1.2"
#   freezedetect    → "lavfi.freezedetect.freeze_start: 5"
# 三种 marker 前缀正好对应 :func:`parse_filter_ranges` 的 ``kind`` 参数
# (black / silence / freeze),直接复用,不再自己写正则。
_SUSPECT_SOURCES = (
    ("blackdetect_log_tail", "black"),
    ("silencedetect_log_tail", "silence"),
    ("freezedetect_log_tail", "freeze"),
)


def _merge_windows(windows: list[dict[str, Any]], *, gap: float = 0.5) -> list[dict[str, Any]]:
    """把重叠 / 间隔小于 ``gap`` 的窗口并成一个,并保留 ``kinds`` 溯源。

    例如 ``black 2.0-4.0`` 与 ``freeze 3.8-6.0`` 会被并成 ``2.0-6.0 [black,freeze]``,
    对同一段画面只做一次重采样,不浪费 ffmpeg 调用。
    """
    if not windows:
        return []
    ordered = sorted(windows, key=lambda w: (w["start"], w["end"]))
    merged = [dict(ordered[0])]
    for item in ordered[1:]:
        last = merged[-1]
        if item["start"] <= last["end"] + gap:
            last["end"] = max(last["end"], item["end"])
            last["kinds"] = sorted(set(last.get("kinds") or []) | set(item.get("kinds") or []))
        else:
            merged.append(dict(item))
    for item in merged:
        item["start"] = round(max(0.0, item["start"]), 3)
        item["end"] = round(item["end"], 3)
        item["duration"] = round(item["end"] - item["start"], 3)
    return merged


def _collect_suspicious_windows(
    qc_report: dict[str, Any],
    *,
    max_windows: int,
    pad: float = 0.5,
    clip_duration: float | None = None,
) -> list[dict[str, Any]]:
    """从 ``preview_qc_report.json`` 抽出最多 ``max_windows`` 个可疑时间段。

    约束(对齐 ``video_watch_segment`` 的入参校验,越界会被工具直接拒):
    - 单段 ≤ ``MAX_SEGMENT_SECONDS``
    - 段数 ≤ ``MAX_SEGMENTS``
    - 复核总时长 ≤ ``MAX_TOTAL_SECONDS``

    Args:
        qc_report: ``qc_preview`` 写出的报告 dict。
        max_windows: 最多取几个窗口(config ``ASSEMBLY_REPAIR_MAX_WINDOWS``)。
        pad: 每个窗口两侧各外扩多少秒 —— QC 只报"黑帧起点",外扩才能同时
            拍到黑帧前后的正常画面,人一眼能看出"从哪一帧开始坏的"。
        clip_duration: 已知素材总时长时用它裁掉越界窗口(预览片可能比日志长)。

    Returns:
        ``[{"start": float, "end": float, "duration": float, "kinds": [...]}]``;
        没有时间戳类 issue 时返回 ``[]``(调用方据此跳过复核)。
    """
    raw: list[dict[str, Any]] = []
    for field, kind in _SUSPECT_SOURCES:
        log = qc_report.get(field)
        if not isinstance(log, str) or not log.strip():
            continue
        for rng in parse_filter_ranges(log, kind):
            start = rng.get("start")
            end = rng.get("end")
            if start is None or end is None:
                continue
            raw.append({"start": float(start) - pad, "end": float(end) + pad, "kinds": [kind]})

    if not raw:
        return []

    merged = _merge_windows(raw)
    if clip_duration:
        merged = [
            {**w, "end": min(w["end"], max(w["start"] + 0.2, clip_duration))}
            for w in merged
        ]
        merged = [w for w in merged if w["end"] > w["start"]]

    # 长窗口先截到工具上限,再按"总时长预算"贪心取前 N 段
    max_segment = float(MAX_SEGMENT_SECONDS)
    capped: list[dict[str, Any]] = []
    for w in merged:
        end = min(w["end"], w["start"] + max_segment)
        capped.append({**w, "end": round(end, 3)})

    budget = float(min(MAX_TOTAL_SECONDS, max_segment * max(1, min(max_windows, MAX_SEGMENTS))))
    picked: list[dict[str, Any]] = []
    spent = 0.0
    for w in capped:
        duration = round(w["end"] - w["start"], 3)
        if duration <= 0:
            continue
        if picked and spent + duration > budget:
            break
        picked.append({**w, "duration": duration})
        spent += duration
        if len(picked) >= max(1, min(max_windows, MAX_SEGMENTS)):
            break
    return picked


def _probe_duration(video_path: Path) -> float | None:
    """取预览片时长(秒);探测失败返回 None(不阻断)。"""
    try:
        from video_edit_capabilities.timeline import media_duration_seconds

        return media_duration_seconds(video_path)
    except Exception:
        return None


def _run_visual_evidence(
    preview_path: Path,
    windows: list[dict[str, Any]],
    ctx: RunContext,
    out_dir: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """对每个可疑窗口做"高 fps 重采样 + 局部放大",返回 (逐窗结果, 错误列表)。

    两级复核,对应计划 §6.5 的"高 fps 重采样 + 局部放大":

    1. ``video_watch_segment`` —— 整段按 ``ASSEMBLY_REPAIR_WATCH_FPS`` 连续采样,
       回答"这一段到底卡在哪一帧 / 是不是整段都黑"。
    2. ``video_read_frames`` —— 每个窗口取中点帧、2x 放大、中心区域,
       回答"那几秒里画面上到底有什么"。

    两级都用 ``ToolResult`` 而非异常表达失败:工具本身返回 ``[ERROR] ...`` 文本,
    这里收集起来写进 evidence json,不往 pipeline 抛。
    """
    segments = [{"start": w["start"], "end": w["end"]} for w in windows]
    midpoints = [round((w["start"] + w["end"]) / 2.0, 3) for w in windows]

    watch_result: ToolResult = video_watch_segment(
        {
            "video_path": str(preview_path),
            "segments": segments,
            "fps": float(config.ASSEMBLY_REPAIR_WATCH_FPS),
            # 修复循环最多跑 ASSEMBLY_QC_MAX_RETRY 轮,窗口通常一模一样。
            # 不 force 会被 ledger 判定为"上次已看过",第二轮起拿不到新帧。
            "force": True,
            "save_frames_dir": str(out_dir / "repair_frames"),
        },
        ctx,
    )
    zoom_result: ToolResult = video_read_frames(
        {
            "video_path": str(preview_path),
            "timestamps": midpoints,
            "region": "center",
            "upscale": 2.0,
            "max_frames": len(midpoints) or 1,
            "output_dir": str(out_dir / "repair_zoom"),
        },
        ctx,
    )

    errors: list[str] = []
    for label, result in (("video_watch_segment", watch_result), ("video_read_frames", zoom_result)):
        if result.text.startswith("[ERROR]"):
            errors.append(f"{label}: {result.text}")

    watch_frames: list[dict[str, Any]] = []
    watch_data = watch_result.data or {}
    if isinstance(watch_data.get("segments"), list):
        watch_frames = [s for s in watch_data["segments"] if isinstance(s, dict)]
    zoom_frames: list[dict[str, Any]] = []
    zoom_data = zoom_result.data or {}
    if isinstance(zoom_data.get("frames"), list):
        zoom_frames = [f for f in zoom_data["frames"] if isinstance(f, dict)]

    results = [
        {
            "window": w,
            "watch_segments": watch_frames[i] if i < len(watch_frames) else None,
            "zoom_frame": zoom_frames[i] if i < len(zoom_frames) else None,
        }
        for i, w in enumerate(windows)
    ]
    return results, errors


def assembly_repair_loop_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"

    timeline_path = state.get("assembly_timeline_path")
    if not timeline_path:
        return {
            "error_log": append_error(
                state, node_kind="assembly_repair_loop", error_code="CONTRACT_INVALID",
                message="missing assembly_timeline_path",
            ),
            "status_log": append_status_tag(state, "assembly_repair_loop_failed"),
        }

    ctx = RunContext(session_kind="pipeline")
    patch = _build_repair_patch(
        qc_report_path=state.get("assembly_qc_report_path"),
        validation_report_path=state.get("assembly_timeline_validation_path"),
    )

    err_log_patch: list[str] = []
    evidence_path: str | None = None

    # ---- 阶段五:视觉证据复核(旁路,失败不阻断 patch 链路) ----
    if config.ASSEMBLY_REPAIR_VISUAL_EVIDENCE:
        preview_path = state.get("assembly_preview_path")
        qc_report_path = state.get("assembly_qc_report_path")
        try:
            qc_report: dict[str, Any] = {}
            if qc_report_path and Path(qc_report_path).is_file():
                loaded = json.loads(Path(qc_report_path).read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    qc_report = loaded

            windows = _collect_suspicious_windows(
                qc_report,
                max_windows=int(config.ASSEMBLY_REPAIR_MAX_WINDOWS),
                clip_duration=_probe_duration(Path(preview_path)) if preview_path else None,
            )
            if not windows:
                err_log_patch.append(
                    "[assembly_repair_loop] 视觉复核跳过:QC 报告里没有时间戳类可疑片段"
                )
            elif not preview_path or not Path(preview_path).is_file():
                err_log_patch.append(
                    "[assembly_repair_loop] 视觉复核跳过:预览视频不存在 "
                    f"(assembly_preview_path={preview_path!r})"
                )
            else:
                out_dir.mkdir(parents=True, exist_ok=True)
                evidence, ev_errors = _run_visual_evidence(
                    Path(preview_path), windows, ctx, out_dir
                )
                err_log_patch.extend(
                    f"[assembly_repair_loop] 视觉复核降级: {msg}" for msg in ev_errors
                )
                evidence_file = out_dir / "repair_visual_evidence.json"
                evidence_file.write_text(
                    json.dumps(
                        {
                            "tool": "assembly_repair_loop.visual_evidence",
                            "retry_index": int(state.get("assembly_qc_retry_count") or 0) + 1,
                            "preview_path": str(preview_path),
                            "qc_report_path": str(qc_report_path) if qc_report_path else None,
                            "watch_fps": float(config.ASSEMBLY_REPAIR_WATCH_FPS),
                            "windows": windows,
                            "evidence": evidence,
                            "errors": ev_errors,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                evidence_path = str(evidence_file)
        except Exception as exc:  # 视觉复核任何异常都不许打断主链
            err_log_patch.append(f"[assembly_repair_loop] 视觉复核异常(已忽略): {exc!r}")

    diff_out = out_dir / "timeline_diff.json"
    diff_result: ToolResult = timeline_diff(
        {
            "timeline_path": str(timeline_path),
            "instructions": "assembly_repair_loop placeholder: add metadata marker; stage 3 will add typed patches",
            "patch": patch,
            "apply": True,
            "output_json": str(diff_out),
        },
        ctx,
    )

    # 重试计数 +1(唯一写入者,无需 reducer)
    retry_count = int(state.get("assembly_qc_retry_count") or 0) + 1

    if diff_result.text.startswith("[ERROR]"):
        err_log_patch = append_error(
            state, node_kind="assembly_repair_loop",
            error_code="TOOL_EXECUTION_FAILED",
            message=diff_result.text,
        )

    tags = ["assembly_repair_loop_done"]
    if evidence_path:
        tags.append("assembly_repair_visual_evidence_done")

    return {
        "assembly_qc_retry_count": retry_count,
        "assembly_repair_evidence_path": evidence_path,
        "error_log": err_log_patch,
        "status_log": append_status_tag(state, *tags),
    }
