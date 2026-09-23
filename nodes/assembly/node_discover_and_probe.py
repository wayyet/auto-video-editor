"""节点 1/6:``assembly_discover_and_probe``(plan §7.1)。

对应工具:``inspect_media`` + ``analyze_media``。
职责:发现候选素材(单文件或目录扫)→ 对每个素材跑 ``inspect_media`` +
``analyze_media`` → 汇总写 ``media.json`` → 写 ``assembly_media_artifact``。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assembly_capabilities.media_probe import analyze_media, inspect_media
from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext

from nodes.storyline._common import (
    _resolve_outputs_root,
    append_error,
    append_status_tag,
)
from state import WorkflowState


def _discover_source_files(video_input_path: Path) -> list[Path]:
    """素材发现:单文件直接返回;目录扫一级(非递归)的常见视频文件。

    plan §7.1 注释:"这一步由 Skill 自己用普通文件系统操作完成,不是 MCP 工具,
    这里同样用普通 Python 实现,不调用任何外部服务"。
    """
    if video_input_path.is_file():
        return [video_input_path]
    if video_input_path.is_dir():
        suffixes = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
        out: list[Path] = []
        for child in sorted(video_input_path.iterdir()):
            if child.is_file() and child.suffix.lower() in suffixes:
                out.append(child)
        return out
    return []


def assembly_discover_and_probe_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"
    out_dir.mkdir(parents=True, exist_ok=True)

    video_input_path = state.get("video_input_path")
    if not video_input_path:
        return {
            "error_log": append_error(
                state, node_kind="assembly_discover", error_code="CONTRACT_INVALID",
                message="missing video_input_path",
            ),
            "status_log": append_status_tag(state, "assembly_discover_failed"),
        }

    candidates = _discover_source_files(Path(str(video_input_path)))
    if not candidates:
        return {
            "error_log": append_error(
                state, node_kind="assembly_discover", error_code="CONTRACT_INVALID",
                message=f"no source video found at {video_input_path}",
            ),
            "status_log": append_status_tag(state, "assembly_discover_failed"),
        }

    ctx = RunContext(session_kind="pipeline")
    media_reports: list[dict[str, Any]] = []
    try:
        for src in candidates:
            probe: ToolResult = inspect_media({"input_path": str(src)}, ctx)
            analysis: ToolResult = analyze_media({"input_path": str(src)}, ctx)
            media_reports.append({
                "source": str(src),
                "probe": probe.data,
                "analysis": analysis.data,
                "probe_text": probe.text,
                "analysis_text": analysis.text,
            })
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": append_error(
                state, node_kind="assembly_discover", error_code="TOOL_EXECUTION_FAILED",
                message=repr(e),
            ),
            "status_log": append_status_tag(state, "assembly_discover_failed"),
        }

    out_path = out_dir / "media.json"
    out_path.write_text(json.dumps(media_reports, ensure_ascii=False, default=str), encoding="utf-8")
    return {
        "assembly_media_artifact": str(out_path),
        "status_log": append_status_tag(state, "assembly_discover_done"),
    }
