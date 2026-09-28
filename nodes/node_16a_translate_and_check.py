"""node_16a_translate_and_check — 步骤 16a:翻译 + layout 检测(Week 5 新增)。

对齐第 5 周计划 §2.1 / §4.2:
- 只做"翻译 + 写 marker + 写 subtitle_segments_en 到 state + 检测 layout +
  写 SRT(正常路径)”。**不**含 ``interrupt()`` —— 这是设计关键:翻译 API
  调用独立成节点后,LangGraph 中间件重放时由 ``_marker_exists`` 守住,自然幂等
  (附件"关键发现①/③")。
- ``layout_issues`` / ``layout_issues_detected`` 写到 state,后续
  ``node_checkpoint3_layout_review`` 据此决定是否触发关卡③。
- SRT 写入:正常路径下(无 layout 异常)在 16a 内一次性写完,与 Week 4 原
  ``node_16_translate_subtitles`` 行为对齐;关卡③ 触发路径下 SRT 由
  ``node_checkpoint3_layout_review`` resume 后重写。

9 工具迁移 §6.4 step 2 改造:把第 79-84 行 ``validate_layout(...)`` 替换为优先
走 ``subtitle_build → subtitle_render → subtitle_qc`` 链(qc 链);qc 链
任一步失败 → 降级为旧 ``validate_layout``(对照 §6.4 step 2 关键契约)。
``layout_issues`` / ``layout_issues_detected`` 字段名与结构**完全不变**,关卡③
``node_checkpoint3_layout_review`` 现有逻辑零改动。

依赖:共享纯函数来自 ``node_16_translate_subtitles``(Week 5 拆分后保留)。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from assembly_capabilities.run_context import RunContext
from jy_common.translate_client import translate_segments
from nodes.node_16_translate_subtitles import (
    _marker_exists,
    _read_segments_from_draft,
    _save_marker,
    _write_marker,
    validate_layout,
    write_srt_from_segments,
)
from state import WorkflowState
from video_edit_capabilities.subtitle_build import subtitle_build, subtitle_qc, subtitle_render


# 9 工具迁移 §6.4 step 2:qc 链中间产物落盘根目录(对照 RunContext.work_dir)
_QC_CHAIN_WORK_ROOT = ".video_agent/layout_qc"


def _segments_en_to_transcript_records(
    segments_en: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """``subtitle_segments_en`` → ``load_timed_segments`` 接受的 JSON 记录列表。

    字段映射(对照 ``assembly_capabilities/transcript.py`` ``load_timed_segments`` /
    ``collect_segments`` 期望的 key 集合):``start`` / ``end``(秒)+ ``text``。
    """
    records: list[dict[str, Any]] = []
    for seg in segments_en:
        start_ms = seg.get("start_ms")
        end_ms = seg.get("end_ms")
        text_en = str(seg.get("text_en") or "").strip()
        if start_ms is None or end_ms is None or not text_en:
            continue
        try:
            start_s = float(start_ms) / 1000.0
            end_s = float(end_ms) / 1000.0
        except (TypeError, ValueError):
            continue
        if end_s <= start_s:
            continue
        records.append({"text": text_en, "start": start_s, "end": end_s})
    return records


def _check_layout_via_subtitle_qc(
    segments_en: list[dict[str, Any]],
    video_path: str,
    draft_dir: Path,
) -> tuple[list[dict[str, Any]] | None, str | None, list[str] | None]:
    """跑 ``subtitle_build → subtitle_render → subtitle_qc`` 链,返回 issues / preview / evidence。

    任一步失败 → 返回 ``(None, None, None)``,调用方降级为旧 ``validate_layout``。

    Returns:
        ``(issues, preview_path, evidence_frames)`` 三元组。``issues`` 为 ``[]``
        表示 qc 通过;``preview_path`` 为烧录字幕 MP4 路径(供关卡③ payload 附
        ``preview_path`` 给人工直接打开);``evidence_frames`` 为 qc 证据帧路径
        列表(``None`` 表示 qc 未生成或步骤失败)。
    """
    if not video_path:
        return None, None, None
    video_p = Path(video_path)
    if not video_p.is_file():
        return None, None, None

    transcript_records = _segments_en_to_transcript_records(segments_en)
    if not transcript_records:
        return None, None, None

    ctx = RunContext()
    work_root = ctx.resolve(_QC_CHAIN_WORK_ROOT)
    work_root.mkdir(parents=True, exist_ok=True)
    transcript_path = work_root / "transcript.json"
    transcript_path.write_text(
        json.dumps(transcript_records, ensure_ascii=False),
        encoding="utf-8",
    )

    try:
        # 1. subtitle_build
        build_out = subtitle_build(
            {
                "transcript_path": str(transcript_path),
                "video_path": str(video_p),
                "preset": "shortform_zh",
            },
            ctx,
        )
        if build_out.text.startswith("[ERROR]"):
            return None, None, None
        subtitles_path = build_out.artifacts[0] if build_out.artifacts else None
        if not subtitles_path or not Path(subtitles_path).is_file():
            return None, None, None

        # 2. subtitle_render(mode=burn → 出预览 MP4)
        render_out = subtitle_render(
            {
                "video_path": str(video_p),
                "subtitles_path": str(subtitles_path),
                "mode": "burn",
                "output_path": str(work_root / "preview.mp4"),
            },
            ctx,
        )
        if render_out.text.startswith("[ERROR]"):
            return None, None, None
        burned = render_out.data.get("outputs", {}).get("burned") if isinstance(render_out.data, dict) else None
        if not burned:
            return None, None, None

        # 3. subtitle_qc(拿 issues + evidence frames)
        qc_out = subtitle_qc(
            {
                "subtitles_path": str(subtitles_path),
                "video_path": str(burned),
            },
            ctx,
        )
        if qc_out.text.startswith("[ERROR]"):
            # qc 步骤失败 → 整链降级为 validate_layout(对照 9 工具迁移 §6.4 step 2)
            return None, None, None
        qc_data = qc_out.data if isinstance(qc_out.data, dict) else {}
        issues = qc_data.get("issues") or []
        evidence_frames_meta = qc_data.get("evidence_frames") or []
        evidence_paths: list[str] = []
        for meta in evidence_frames_meta:
            if isinstance(meta, dict):
                p = meta.get("path") or meta.get("image_path")
                if p:
                    evidence_paths.append(str(p))
            elif isinstance(meta, str):
                evidence_paths.append(meta)
        # 也合并 ToolResult.image_paths(对照 subtitle_qc 实现)
        if isinstance(qc_out.image_paths, list):
            for p in qc_out.image_paths:
                if p and p not in evidence_paths:
                    evidence_paths.append(str(p))

        return list(issues), str(burned), evidence_paths
    finally:
        # 清理中间 transcript(产物 subtitles.json / preview.mp4 留作关卡③ payload)
        try:
            transcript_path.unlink(missing_ok=True)
        except Exception:
            pass


def node_16a_translate_and_check(state: WorkflowState) -> dict:
    """LangGraph 节点(Week 5 新增):翻译 + 写 marker + layout 校验。

    Args:
        state: 需含 ``asr_segments_zh`` / ``draft_dir_en_branch``。

    Returns:
        dict,只含变更字段 — 避免 fan-in 时与其他分支并发写同一字段:
        - ``subtitle_segments_en``: 英文字幕段
        - ``layout_issues``: layout 异常列表(可能为空)
        - ``layout_issues_detected``: bool,关卡③ 触发条件
        - ``layout_issues_source``: ``"qc_chain"`` / ``"validate_layout"`` /
          ``"empty"`` 三选一(便于审计哪条路径产出)
        - ``preview_video_path``: 关卡③ 预览 MP4 路径(qc 链成功时)
        - ``subtitle_qc_evidence_frames``: 关卡③ 证据帧路径列表(qc 链成功时)
        - ``status_log``: 仅 delta(由 reducer ``_append_unique`` 合并)
    """
    segments_zh = list(state.get("asr_segments_zh") or [])
    if not segments_zh:
        return {
            "error_log": ["[node_16a] asr_segments_zh 缺失,跳过翻译"],
            "status_log": ["node_16a_translate_skipped"],
        }

    draft_dir = state.get("draft_dir_en_branch")
    if not draft_dir:
        return {
            "error_log": ["[node_16a] draft_dir_en_branch 缺失,跳过翻译"],
            "status_log": ["node_16a_translate_skipped"],
        }

    draft_dir_p = Path(draft_dir)
    video_path = str(state.get("video_input_path") or "")

    # ----- 翻译(已有 marker 则跳过)-----
    if _marker_exists(draft_dir_p, expected_count=len(segments_zh)):
        # 已有 marker,跳过翻译 API 调用,直接读草稿(可能含人工修正)
        segments_en = _read_segments_from_draft(draft_dir_p)
    else:
        translated = translate_segments(segments_zh)
        # 同步 start_ms / end_ms(translate_client 可能不返回)
        for i, (src, dst) in enumerate(zip(segments_zh, translated)):
            dst.setdefault("start_ms", src.get("start_ms", 0))
            dst.setdefault("end_ms", src.get("end_ms", 0))
            dst.setdefault("index", src.get("index", i))
        # 写入 draft(materials.texts[i].text_en)与 marker
        _write_marker(draft_dir_p, translated)
        _save_marker(draft_dir_p, translated)
        segments_en = translated

    # ----- layout 校验:9 工具迁移 §6.4 step 2 优先 qc 链,失败兜底旧 validate_layout -----
    issues: list[dict[str, Any]]
    issues_source: str
    preview_path: str | None = None
    evidence_frames: list[str] | None = None

    qc_issues, qc_preview, qc_evidence = _check_layout_via_subtitle_qc(
        list(segments_en), video_path, draft_dir_p
    )
    if qc_issues is None:
        # qc 链失败或未跑 → 降级为旧 validate_layout(保留原 Week 5 行为)
        issues = validate_layout(
            segments=segments_en,
            font_path=None,
            font_size=48,
            max_width_px=1500,
        )
        issues_source = "validate_layout"
    else:
        issues = qc_issues
        issues_source = "qc_chain"
        preview_path = qc_preview
        evidence_frames = qc_evidence or None

    # ----- 正常路径下写 SRT(关卡③ 触发时由 c3 resume 后重写)-----
    srt_path = write_srt_from_segments(draft_dir_p, segments_en)

    out: dict[str, Any] = {
        "subtitle_segments_en": list(segments_en),
        "subtitle_srt_path": srt_path,
        "layout_issues": issues,
        "layout_issues_detected": bool(issues),
        "layout_issues_source": issues_source,
        "status_log": ["node_16a_translate_done"],
    }
    if preview_path is not None:
        out["preview_video_path"] = preview_path
    if evidence_frames is not None:
        out["subtitle_qc_evidence_frames"] = evidence_frames
    return out
