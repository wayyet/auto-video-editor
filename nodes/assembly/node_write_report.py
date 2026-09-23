"""节点 6/6:``assembly_write_report``(plan §7.6)。

汇总前 5 步所有产物(素材清单、转写摘要、选段理由、质检结果、修复记录),
写成 Markdown 报告,路径写入 ``assembly_report_path``。

设计纪律:
- **绝不**抛异常 —— 任何步骤都包 try/except,失败就降级写空 section,
  保证关卡① 拿到的是"可读的、最坏情况是空报告"而不是流水线崩溃。
- 报告开头含一行提示:"本次已生成组装质检报告,建议对照剪映草稿一并查看"
  (plan §7.6 + ADR-3)。关卡① 的人工通知文案会引用此路径(本仓库阶段二
  还没接关卡① 通知,阶段五接)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nodes.storyline._common import (
    _resolve_outputs_root,
    append_status_tag,
)
from state import WorkflowState


def _safe_read_json(path: str | Path | None) -> Any:
    if not path:
        return None
    p = Path(str(path))
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _summarize_media(media_reports: list[dict[str, Any]]) -> str:
    lines: list[str] = [f"- 素材数量:{len(media_reports)}"]
    for i, rep in enumerate(media_reports[:5], start=1):
        source = rep.get("source", "<unknown>")
        analysis = rep.get("analysis") or {}
        scenes = (analysis.get("scene_change_count") if isinstance(analysis, dict) else None)
        candidates = (analysis.get("candidate_segment_count") if isinstance(analysis, dict) else None)
        duration = (analysis.get("duration_seconds") if isinstance(analysis, dict) else None)
        lines.append(
            f"  - [{i}] `{source}` — duration={duration}s, scenes={scenes}, candidates={candidates}"
        )
    if len(media_reports) > 5:
        lines.append(f"  - ...(其余 {len(media_reports) - 5} 条省略)")
    return "\n".join(lines)


def _summarize_qc(qc_data: Any) -> str:
    if not isinstance(qc_data, dict):
        return "- (无 QC 报告)"
    status = qc_data.get("status", "unknown")
    issues = qc_data.get("issues") or []
    blocking = [i for i in issues if isinstance(i, dict) and i.get("severity") == "error"]
    warnings = [i for i in issues if isinstance(i, dict) and i.get("severity") == "warning"]
    lines = [
        f"- 整体状态:`{status}`",
        f"- 阻断项:{len(blocking)} 条 / 警告项:{len(warnings)} 条",
    ]
    for i in blocking[:10]:
        lines.append(f"  - [ERROR] {i.get('message', '')}")
    for i in warnings[:10]:
        lines.append(f"  - [WARN] {i.get('message', '')}")
    if len(blocking) > 10:
        lines.append(f"  - ...(其余 {len(blocking) - 10} 条阻断项省略)")
    if len(warnings) > 10:
        lines.append(f"  - ...(其余 {len(warnings) - 10} 条警告省略)")
    return "\n".join(lines)


def _summarize_validation(validation_data: Any) -> str:
    if not isinstance(validation_data, dict):
        return "- (无 validation 报告)"
    status = validation_data.get("status", "unknown")
    clip_count = validation_data.get("clip_count", 0)
    issues = validation_data.get("issues") or []
    errors = [i for i in issues if isinstance(i, dict) and i.get("severity") == "error"]
    warnings = [i for i in issues if isinstance(i, dict) and i.get("severity") == "warning"]
    lines = [
        f"- 整体状态:`{status}`",
        f"- clip 数量:{clip_count}",
        f"- error:{len(errors)} 条 / warning:{len(warnings)} 条",
    ]
    for i in errors[:5]:
        lines.append(f"  - [ERROR] {i.get('message', '')}")
    return "\n".join(lines)


def _summarize_timeline(timeline_path: str | None) -> str:
    if not timeline_path:
        return "- (timeline.json 未生成)"
    data = _safe_read_json(timeline_path)
    if not isinstance(data, dict):
        return f"- timeline 文件存在但无法解析:`{timeline_path}`"
    tracks = data.get("tracks") or []
    total_clips = sum(len(t.get("clips") or []) for t in tracks if isinstance(t, dict))
    sequence = data.get("sequence") or {}
    lines = [
        f"- 路径:`{timeline_path}`",
        f"- 轨道数:{len(tracks)}",
        f"- 总 clip 数:{total_clips}",
        f"- sequence.duration:{sequence.get('duration')}s, fps:{sequence.get('fps')}",
    ]
    return "\n".join(lines)


def assembly_write_report_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. 汇总输入 ----
    media_data = _safe_read_json(state.get("assembly_media_artifact"))
    qc_data = _safe_read_json(state.get("assembly_qc_report_path"))
    validation_data = _safe_read_json(state.get("assembly_timeline_validation_path"))
    timeline_path = state.get("assembly_timeline_path")
    preview_path = state.get("assembly_preview_path")
    qc_status = state.get("assembly_qc_status") or "unknown"
    retry_count = int(state.get("assembly_qc_retry_count") or 0)

    # ---- 2. 渲染 Markdown ----
    sections: list[str] = []
    sections.append("# Assembly QC 报告\n")
    sections.append("> 本次已生成组装质检报告,建议对照剪映草稿一并查看。")
    sections.append("> 关卡① 的人工通知文案会引用此路径。\n")

    sections.append("## 1. 总览\n")
    sections.append(f"- QC 状态:`{qc_status}`")
    sections.append(f"- 修复循环重试次数:{retry_count}")
    sections.append(f"- timeline.json:`{timeline_path or '(未生成)'}`")
    sections.append(f"- preview.mp4:`{preview_path or '(未生成)'}`\n")

    sections.append("## 2. 素材清单 (media.json)\n")
    if isinstance(media_data, list):
        sections.append(_summarize_media(media_data))
    else:
        sections.append("- (无素材清单)")
    sections.append("")

    sections.append("## 3. timeline 校验 (timeline_validation.json)\n")
    sections.append(_summarize_validation(validation_data))
    sections.append("")

    sections.append("## 4. timeline 内容\n")
    sections.append(_summarize_timeline(timeline_path))
    sections.append("")

    sections.append("## 5. 质检报告 (preview_qc_report.json)\n")
    sections.append(_summarize_qc(qc_data))
    sections.append("")

    sections.append("---\n*本报告由 assembly_write_report 节点自动生成;关卡① 人工看到后建议把 `preview.mp4` 与 `剪映草稿` 一起过一遍。*")

    out_path = out_dir / "report.md"
    out_path.write_text("\n".join(sections), encoding="utf-8")
    return {
        "assembly_report_path": str(out_path),
        "status_log": append_status_tag(state, "assembly_write_report_done"),
    }
