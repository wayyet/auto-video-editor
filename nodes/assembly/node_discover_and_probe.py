"""节点 1/6:``assembly_discover_and_probe``(第六期 P1-2 manifest 版)。

对应工具:``inspect_media`` + ``analyze_media``。
职责:发现本轮 Storyline 计划引用的源素材 → 逐素材 ``inspect_media`` +
``analyze_media`` → 写素材 manifest ``media.json`` → 路径写
``assembly_media_artifact``。

P1-2 变更(设计文档 §3.1 / F-02 / F-11 / F-12):
- 素材来源优先 ``state.storyline_plan`` 的 ``source_media[].file_uri``
  (:func:`storyline.mapper.file_uri_to_path` 还原本机路径),plan 缺失 /
  无可定位路径时回落 ``resolve_video_input_paths``(stub 模式与旧链兜底)
  —— 发现的素材与 storyline 计划一致,不再只依赖通用 video_input_path;
- 每素材计算稳定 ``asset_id``(:func:`media_probe.compute_asset_id` =
  规范化路径+size+mtime 的 sha256 前 16 位),probe/analysis sidecar 全部
  落 ``media_analysis/<asset_id>*.json`` —— 多素材 / 同名文件不再互相覆盖;
- ``media.json`` 从"逐素材摘要 list"升级为 manifest dict(v1):
  ``{schema_version, job_id, base_plan_sha256, sources[], degraded}``,
  每个 source 携带完整 ``candidate_segments`` / 场景切点 / 黑帧 / 静音明细
  (F-02:候选明细进正式契约,不再依赖被覆盖的 sidecar);
- ToolResult ``[ERROR]`` 文本与 data 必需字段显式校验(F-12):失败素材
  ``probe_status`` / ``analysis_status`` = failed + ``degraded_reason``,
  绝不伪装成功;部分失败 → 节点降级完成(tag ``assembly_discover_degraded``),
  全部失败才节点失败。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from assembly_capabilities.influence_trace import canonical_json_sha256
from assembly_capabilities.media_probe import analyze_media, compute_asset_id, inspect_media
from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext
from storyline.mapper import file_uri_to_path

from nodes.storyline._common import (
    _resolve_outputs_root,
    append_error,
    append_status_tag,
    resolve_video_input_paths,
)
from state import WorkflowState

#: manifest 结构版本(字段变更时 bump 并同步测试)
MEDIA_MANIFEST_SCHEMA_VERSION = 1


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _plan_sources(state: WorkflowState) -> tuple[list[dict[str, Any]], dict[str, list[str]]] | None:
    """从 ``storyline_plan`` 解析本轮真实素材引用(plan §P1-2 任务 6)。

    Returns:
        (plan_entries, clip_map):
        - plan_entries: ``[{"media_id", "path", "duration_ms"}]``(按 plan 顺序);
        - clip_map: ``media_id -> [clip_id, ...]``(素材↔原始计划 clip 映射)。
        plan 缺失 / 形状不对 / source_media 为空时返回 ``None``(调用方回落
        video_input_paths)。
    """
    plan = state.get("storyline_plan")
    if not isinstance(plan, dict):
        return None
    sources = plan.get("source_media")
    if not isinstance(sources, list) or not sources:
        return None
    entries: list[dict[str, Any]] = []
    for m in sources:
        if not isinstance(m, dict):
            continue
        media_id = str(m.get("media_id") or "")
        uri = str(m.get("file_uri") or "")
        path: str | None = None
        if uri:
            try:
                path = file_uri_to_path(uri)
            except ValueError:
                path = None
        entries.append(
            {
                "media_id": media_id,
                "path": path,
                "duration_ms": m.get("duration_ms"),
            }
        )
    if not entries:
        return None
    clip_map: dict[str, list[str]] = {}
    for clip in plan.get("clips") or []:
        if isinstance(clip, dict):
            sid = str(clip.get("source_media_id") or "")
            cid = str(clip.get("clip_id") or "")
            if sid and cid:
                clip_map.setdefault(sid, []).append(cid)
    return entries, clip_map


def _collect_targets(state: WorkflowState) -> list[dict[str, Any]] | None:
    """汇总本轮待探测素材:plan 主导(优先),plan 缺失/无有效路径时回落旧发现。

    plan 存在时**只用** plan 引用的素材(设计要求:发现的素材与 storyline
    计划一致);plan 引用的所有路径都无法定位时回落 video_input_paths 展开
    (stub 模式 / 计划引用漂移的兜底)。

    Returns:
        ``[{"path", "plan_media_id", "plan_clip_ids"}]``(去重,保持首次出现
        顺序;同一素材被多个 media_id 引用时合并 clip 映射);无可定位素材
        返回 ``None``。
    """
    parsed = _plan_sources(state)
    targets: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    if parsed is not None:
        plan_entries, clip_map = parsed
        for entry in plan_entries:
            path = entry["path"]
            if not path:
                continue
            key = _norm_path(path)
            if not key:
                continue
            if key in seen:
                targets[seen[key]]["plan_clip_ids"].extend(
                    clip_map.get(entry["media_id"], [])
                )
                continue
            seen[key] = len(targets)
            targets.append(
                {
                    "path": path,
                    "plan_media_id": entry["media_id"],
                    "plan_clip_ids": list(clip_map.get(entry["media_id"], [])),
                }
            )
        # plan 素材至少一个可定位 → 以 plan 为准
        if targets:
            return targets

    # 回落:旧发现逻辑(video_input_path / video_input_paths,目录扫一级)
    video_input_path = state.get("video_input_path")
    source_paths = resolve_video_input_paths(state)
    if not source_paths:
        return None
    for raw in source_paths:
        for src in _discover_source_files(Path(raw)):
            key = _norm_path(str(src))
            if key in seen:
                continue
            seen[key] = len(targets)
            targets.append(
                {"path": str(src), "plan_media_id": None, "plan_clip_ids": []}
            )
    if not targets:
        return None
    return targets


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


def _read_analysis_report(path: Path) -> dict[str, Any]:
    """读 analyze_media 落盘的完整 report(候选明细在文件里,summary 不带)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _probe_source(
    target: dict[str, Any], ctx: RunContext, media_analysis_dir: Path
) -> dict[str, Any]:
    """对单个素材跑 inspect_media + analyze_media,产出 manifest source 条目。"""
    path = str(target["path"])
    entry: dict[str, Any] = {
        "asset_id": None,
        "source_path": path,
        "plan_media_id": target.get("plan_media_id"),
        "plan_clip_ids": list(target.get("plan_clip_ids") or []),
        "duration_seconds": None,
        "probe_status": "failed",
        "analysis_status": "skipped",
        "degraded_reason": None,
        "candidate_segments": [],
        "scene_change_times": [],
        "black_ranges": [],
        "silence_ranges": [],
        "technical_risks": [],
        "probe_summary": {},
        "analysis_summary": {},
        "probe_text": "",
        "analysis_text": "",
    }

    asset_id = compute_asset_id(Path(path))
    if asset_id is None:
        entry["degraded_reason"] = "file_stat_failed(文件不存在或不可读)"
        entry["probe_text"] = "[ERROR] compute_asset_id failed: file not readable"
        return entry
    entry["asset_id"] = asset_id

    probe_sidecar = media_analysis_dir / f"{asset_id}_probe.json"
    analysis_out = media_analysis_dir / f"{asset_id}.json"

    try:
        probe: ToolResult = inspect_media({"input_path": path, "output_json": str(probe_sidecar)}, ctx)
    except Exception as exc:  # noqa: BLE001 — 工具异常按失败处理,不中断逐素材循环
        entry["degraded_reason"] = f"probe exception: {exc!r}"
        return entry
    entry["probe_text"] = probe.text
    probe_data = probe.data if isinstance(probe.data, dict) else {}
    entry["probe_summary"] = probe_data.get("summary") or {}
    entry["technical_risks"] = list(probe_data.get("technical_risks") or [])
    if probe.text.startswith("[ERROR]"):
        entry["degraded_reason"] = f"probe failed: {probe.text}"
        return entry
    if probe_data.get("duration_seconds") is None:
        entry["degraded_reason"] = "probe data missing duration_seconds"
        return entry
    entry["probe_status"] = "ok"
    entry["duration_seconds"] = probe_data.get("duration_seconds")

    try:
        analysis: ToolResult = analyze_media(
            {"input_path": path, "asset_id": asset_id, "output_json": str(analysis_out)},
            ctx,
        )
    except Exception as exc:  # noqa: BLE001
        entry["analysis_status"] = "failed"
        entry["degraded_reason"] = f"analysis exception: {exc!r}"
        entry["analysis_text"] = repr(exc)
        return entry
    entry["analysis_text"] = analysis.text
    analysis_data = analysis.data if isinstance(analysis.data, dict) else {}
    entry["analysis_summary"] = analysis_data
    if analysis.text.startswith("[ERROR]"):
        entry["analysis_status"] = "failed"
        entry["degraded_reason"] = f"analysis failed: {analysis.text}"
        return entry

    # 完整候选明细来自落盘 report(F-02);报告读不到时退回 summary data
    report = _read_analysis_report(analysis_out)
    detail = report if report else analysis_data
    entry["analysis_status"] = "ok"
    entry["candidate_segments"] = list(detail.get("candidate_segments") or [])
    entry["scene_change_times"] = list(detail.get("scene_change_times") or [])
    entry["black_ranges"] = list(detail.get("black_ranges") or [])
    entry["silence_ranges"] = list(detail.get("silence_ranges") or [])
    return entry


def assembly_discover_and_probe_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"
    media_analysis_dir = out_dir / "media_analysis"
    media_analysis_dir.mkdir(parents=True, exist_ok=True)

    targets = _collect_targets(state)
    if not targets:
        return {
            "error_log": append_error(
                state, node_kind="assembly_discover", error_code="CONTRACT_INVALID",
                message="no source video found (storyline_plan 与 video_input_path 均无可定位素材)",
            ),
            "status_log": append_status_tag(state, "assembly_discover_failed"),
        }

    plan = state.get("storyline_plan")
    base_plan_sha256 = canonical_json_sha256(plan) if isinstance(plan, dict) else None

    ctx = RunContext(session_kind="pipeline")
    sources: list[dict[str, Any]] = []
    try:
        for target in targets:
            sources.append(_probe_source(target, ctx, media_analysis_dir))
    except Exception as e:  # noqa: BLE001 — 逐素材循环外的异常按整体失败处理
        return {
            "error_log": append_error(
                state, node_kind="assembly_discover", error_code="TOOL_EXECUTION_FAILED",
                message=repr(e),
            ),
            "status_log": append_status_tag(state, "assembly_discover_failed"),
        }

    failed = [s for s in sources if s["probe_status"] != "ok" or s["analysis_status"] == "failed"]
    degraded = bool(failed)
    all_failed = len(failed) == len(sources)

    manifest: dict[str, Any] = {
        "schema_version": MEDIA_MANIFEST_SCHEMA_VERSION,
        "job_id": str(state.get("session_id") or "unknown"),
        "base_plan_sha256": base_plan_sha256,
        "sources": sources,
        "source_count": len(sources),
        "degraded": degraded,
    }

    out_path = out_dir / "media.json"
    out_path.write_text(json.dumps(manifest, ensure_ascii=False, default=str), encoding="utf-8")

    delta: dict[str, Any] = {
        "assembly_media_artifact": str(out_path),
    }
    if base_plan_sha256:
        delta["assembly_base_plan_sha256"] = base_plan_sha256

    if all_failed:
        delta["error_log"] = append_error(
            state, node_kind="assembly_discover", error_code="TOOL_EXECUTION_FAILED",
            message="all sources failed probe/analysis (见 media.json sources[].degraded_reason)",
        )
        delta["status_log"] = append_status_tag(state, "assembly_discover_failed")
        return delta

    tags = ["assembly_discover_done"]
    if degraded:
        tags.append("assembly_discover_degraded")
    delta["status_log"] = append_status_tag(state, *tags)
    return delta
