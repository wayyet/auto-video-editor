"""节点 2/6:``assembly_visual_observe``(plan §7.2;B1 ASR 移除后为纯视觉观察节点)。

对应工具:``video_ingest``。
职责:对源视频按时间轴抽帧 + 生成 timestamped contact sheet,写
``video_ingest.json`` 供 ``assembly_build_timeline`` 读盘做视觉判断。

(B1 ASR 移除,2026-11-08)本节点原为 ``assembly_asr_and_visual_observe``
复合节点,``speech_transcribe`` 转写透传与 ``transcript.json`` 产物已整体
删除,不再返回 ``assembly_transcript_artifact``;``storyline_transcript_external_path``
外部转写透传入口随之取消。

多视频(2026-10 拆分 node_04 多选导入):
- 逐个源视频抽帧 → ``video_ingest_<i>.json``
- 再合并出 ``video_ingest.json``(``sheet_paths`` / ``frame_paths`` /
  ``per_source``),下游 ``build_timeline_from_paths`` 的入参签名**不变**
- **单视频时走原路径**:产物文件名、内容与改动前完全一致。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext
from assembly_capabilities.visual_observe import video_ingest

from nodes.storyline._common import (
    _resolve_outputs_root,
    append_error,
    append_status_tag,
    resolve_video_input_paths,
)
from state import WorkflowState


def _read_json(path: Path) -> dict[str, Any] | None:
    """读 JSON 文件;不存在 / 非法 → None(降级,不抛异常)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _merge_ingests(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """把多个源视频的 video_ingest 合成一份(下游读 ``sheet_paths`` / ``frame_paths``)。"""
    sheet_paths: list[str] = []
    frame_paths: list[str] = []
    per_source: list[dict[str, Any]] = []
    for doc in docs:
        for p in doc.get("sheet_paths") or []:
            if p:
                sheet_paths.append(str(p))
        for p in doc.get("frame_paths") or []:
            if p:
                frame_paths.append(str(p))
        per_source.append({
            "video_path": str(doc.get("video_path") or ""),
            "sheet_paths": [str(p) for p in (doc.get("sheet_paths") or []) if p],
            "frame_paths": [str(p) for p in (doc.get("frame_paths") or []) if p],
            "media": doc.get("media") if isinstance(doc.get("media"), dict) else {},
        })
    return {
        "tool": "video_ingest",
        "sheet_paths": sheet_paths,
        "frame_paths": frame_paths,
        "per_source": per_source,
        "source_count": len(docs),
    }


def assembly_visual_observe_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"
    out_dir.mkdir(parents=True, exist_ok=True)

    source_paths = resolve_video_input_paths(state)
    media_artifact = state.get("assembly_media_artifact")
    if not source_paths:
        return {
            "error_log": append_error(
                state, node_kind="assembly_visual_observe", error_code="CONTRACT_INVALID",
                message="missing video_input_path",
            ),
            "status_log": append_status_tag(state, "assembly_visual_observe_failed"),
        }
    if not media_artifact:
        return {
            "error_log": append_error(
                state, node_kind="assembly_visual_observe", error_code="CONTRACT_INVALID",
                message="missing assembly_media_artifact (run assembly_discover_and_probe first)",
            ),
            "status_log": append_status_tag(state, "assembly_visual_observe_failed"),
        }

    ctx = RunContext(session_kind="pipeline")
    ingest_out = out_dir / "video_ingest.json"
    multi = len(source_paths) > 1

    ingest_docs: list[dict[str, Any]] = []

    for idx, src_path in enumerate(source_paths):
        per_ingest = (
            (out_dir / f"video_ingest_{idx}.json") if multi else ingest_out
        )

        ingest_args: dict[str, Any] = {
            "video_path": str(src_path),
            "output_json": str(per_ingest),
        }
        try:
            ingest_result: ToolResult = video_ingest(ingest_args, ctx)
        except Exception as e:  # noqa: BLE001
            return {
                "error_log": append_error(
                    state, node_kind="assembly_visual_observe", error_code="TOOL_EXECUTION_FAILED",
                    message=repr(e),
                ),
                "status_log": append_status_tag(state, "assembly_visual_observe_failed"),
            }

        if ingest_result.text.startswith("[ERROR]"):
            return {
                "error_log": append_error(
                    state, node_kind="assembly_visual_observe", error_code="TOOL_EXECUTION_FAILED",
                    message=ingest_result.text,
                ),
                "status_log": append_status_tag(state, "assembly_visual_observe_failed"),
            }

        idoc = _read_json(per_ingest)
        if idoc is not None:
            ingest_docs.append(idoc)

    # 多视频:合并出下游读的产物(签名不变)
    if multi and ingest_docs:
        ingest_out.write_text(
            json.dumps(_merge_ingests(ingest_docs), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return {
        "assembly_ingest_artifact": str(ingest_out),
        "error_log": [],
        "status_log": append_status_tag(state, "assembly_visual_observe_done"),
    }
