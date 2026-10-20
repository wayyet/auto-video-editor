"""节点 2/6:``assembly_asr_and_visual_observe``(plan §7.2)。

对应工具:``speech_transcribe`` + ``video_ingest``。
职责:
1. ``speech_transcribe`` —— 若 ``transcript_path`` / ``inline_text`` 已存在
   则透传;否则返回明确错误(本仓库未配置云端 ASR,见
   ``assembly_capabilities/speech_asr.py``)。
2. ``video_ingest`` —— 标准流程是先转写、再把转写路径传给 ``video_ingest``
   做"每张截图配上对应语音文字"。如果第一步没成功拿到 transcript,此步
   仍可跑(只是 contact sheet 不配文字),不阻塞后续节点。

多视频(2026-10 拆分 node_04 多选导入):
- 逐个源视频跑 ASR + 抽帧 → ``transcript_<i>.json`` / ``video_ingest_<i>.json``
- 再合并出 ``transcript.json`` / ``video_ingest.json``(下游
  ``build_timeline_from_paths`` 的入参签名**不变**)
- **单视频时走原路径**:产物文件名、内容与改动前完全一致。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext
from assembly_capabilities.speech_asr import speech_transcribe
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


def _merge_transcripts(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """把多个源视频的 transcript 合成一份(下游只读 ``segments`` / ``text``)。

    每条 segment 额外带 ``source_media``(源视频路径),便于追溯是哪段语音。
    """
    merged_segments: list[dict[str, Any]] = []
    texts: list[str] = []
    words: list[Any] = []
    for doc in docs:
        src = str(doc.get("source_media") or "")
        for seg in doc.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            item = dict(seg)
            item["source_media"] = src
            merged_segments.append(item)
        for w in doc.get("words") or []:
            words.append(w)
        t = str(doc.get("text") or doc.get("transcript_text") or "").strip()
        if t:
            texts.append(t)
    return {
        "tool": "speech_transcribe",
        "provider": "external",
        "language": "zh",
        "segments": merged_segments,
        "words": words,
        "text": "\n".join(texts),
        "source_count": len(docs),
    }


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


def assembly_asr_and_visual_observe_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"
    out_dir.mkdir(parents=True, exist_ok=True)

    source_paths = resolve_video_input_paths(state)
    media_artifact = state.get("assembly_media_artifact")
    if not source_paths:
        return {
            "error_log": append_error(
                state, node_kind="assembly_asr_observe", error_code="CONTRACT_INVALID",
                message="missing video_input_path",
            ),
            "status_log": append_status_tag(state, "assembly_asr_observe_failed"),
        }
    if not media_artifact:
        return {
            "error_log": append_error(
                state, node_kind="assembly_asr_observe", error_code="CONTRACT_INVALID",
                message="missing assembly_media_artifact (run assembly_discover_and_probe first)",
            ),
            "status_log": append_status_tag(state, "assembly_asr_observe_failed"),
        }

    ctx = RunContext(session_kind="pipeline")
    transcript_out = out_dir / "transcript.json"
    ingest_out = out_dir / "video_ingest.json"
    multi = len(source_paths) > 1

    # ---- 1. ASR:本仓库未配置云端 ASR(详见 speech_asr 模块)。----
    # 若上游已传 transcript_path(由外部脚本喂入),透传;否则报错但不阻断,
    # 因为 video_ingest 仍可单独跑(无对应文字)。
    transcript_artifact: str | None = None
    transcript_error: str | None = None
    external_transcript = state.get("storyline_transcript_external_path")

    # ---- 2. visual_observe:抽帧 + 生成 contact sheet。----
    transcript_docs: list[dict[str, Any]] = []
    ingest_docs: list[dict[str, Any]] = []

    for idx, src_path in enumerate(source_paths):
        per_transcript = (
            (out_dir / f"transcript_{idx}.json") if multi else transcript_out
        )
        per_ingest = (
            (out_dir / f"video_ingest_{idx}.json") if multi else ingest_out
        )

        asr_args: dict[str, Any] = {
            "input_path": str(src_path),
            "output_json": str(per_transcript),
        }
        if external_transcript:
            asr_args["transcript_path"] = str(external_transcript)
        asr_result: ToolResult = speech_transcribe(asr_args, ctx)
        current_transcript: str | None = None
        if asr_result.text.startswith("[ERROR]"):
            if transcript_error is None:
                transcript_error = asr_result.text
        elif per_transcript.is_file():
            current_transcript = str(per_transcript)
            if transcript_artifact is None:
                transcript_artifact = current_transcript

        ingest_args: dict[str, Any] = {
            "video_path": str(src_path),
            "output_json": str(per_ingest),
        }
        if current_transcript:
            ingest_args["transcript_path"] = current_transcript
        try:
            ingest_result: ToolResult = video_ingest(ingest_args, ctx)
        except Exception as e:  # noqa: BLE001
            return {
                "error_log": append_error(
                    state, node_kind="assembly_asr_observe", error_code="TOOL_EXECUTION_FAILED",
                    message=repr(e),
                ),
                "status_log": append_status_tag(state, "assembly_asr_observe_failed"),
            }

        if ingest_result.text.startswith("[ERROR]"):
            return {
                "error_log": append_error(
                    state, node_kind="assembly_asr_observe", error_code="TOOL_EXECUTION_FAILED",
                    message=ingest_result.text,
                ),
                "status_log": append_status_tag(state, "assembly_asr_observe_failed"),
            }

        tdoc = _read_json(per_transcript)
        if tdoc is not None:
            tdoc.setdefault("source_media", str(src_path))
            transcript_docs.append(tdoc)
        idoc = _read_json(per_ingest)
        if idoc is not None:
            ingest_docs.append(idoc)

    # 多视频:合并出下游读的那两份产物(签名不变)
    if multi:
        if transcript_docs:
            transcript_out.write_text(
                json.dumps(_merge_transcripts(transcript_docs), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            transcript_artifact = str(transcript_out)
        if ingest_docs:
            ingest_out.write_text(
                json.dumps(_merge_ingests(ingest_docs), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    # ASR 错误不阻断(本仓库无云 ASR 是预期);只在 transcript_artifact 缺失时
    # 记一条 warning 到 error_log。
    err_log_patch: list[str] = []
    if transcript_error and not transcript_artifact:
        err_log_patch = append_error(
            state, node_kind="assembly_asr_observe",
            error_code="ASR_NOT_CONFIGURED",
            message=transcript_error,
        )

    return {
        "assembly_transcript_artifact": transcript_artifact,
        "assembly_ingest_artifact": str(ingest_out),
        "error_log": err_log_patch,
        "status_log": append_status_tag(state, "assembly_asr_observe_done"),
    }
