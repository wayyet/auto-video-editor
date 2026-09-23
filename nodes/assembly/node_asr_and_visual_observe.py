"""节点 2/6:``assembly_asr_and_visual_observe``(plan §7.2)。

对应工具:``speech_transcribe`` + ``video_ingest``。
职责:
1. ``speech_transcribe`` —— 若 ``transcript_path`` / ``inline_text`` 已存在
   则透传;否则返回明确错误(本仓库未配置云端 ASR,见
   ``assembly_capabilities/speech_asr.py``)。
2. ``video_ingest`` —— 标准流程是先转写、再把转写路径传给 ``video_ingest``
   做"每张截图配上对应语音文字"。如果第一步没成功拿到 transcript,此步
   仍可跑(只是 contact sheet 不配文字),不阻塞后续节点。
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
)
from state import WorkflowState


def assembly_asr_and_visual_observe_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"
    out_dir.mkdir(parents=True, exist_ok=True)

    video_input_path = state.get("video_input_path")
    media_artifact = state.get("assembly_media_artifact")
    if not video_input_path:
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

    # ---- 1. ASR:本仓库未配置云端 ASR(详见 speech_asr 模块)。----
    # 若上游已传 transcript_path(由外部脚本喂入),透传;否则报错但不阻断,
    # 因为 video_ingest 仍可单独跑(无对应文字)。
    transcript_artifact: str | None = None
    transcript_error: str | None = None
    external_transcript = state.get("storyline_transcript_external_path")
    asr_args: dict[str, Any] = {
        "input_path": str(video_input_path),
        "output_json": str(transcript_out),
    }
    if external_transcript:
        asr_args["transcript_path"] = str(external_transcript)
    asr_result: ToolResult = speech_transcribe(asr_args, ctx)
    if asr_result.text.startswith("[ERROR]"):
        transcript_error = asr_result.text
    elif transcript_out.is_file():
        transcript_artifact = str(transcript_out)

    # ---- 2. visual_observe:抽帧 + 生成 contact sheet。----
    ingest_args: dict[str, Any] = {
        "video_path": str(video_input_path),
        "output_json": str(ingest_out),
    }
    if transcript_artifact:
        ingest_args["transcript_path"] = transcript_artifact
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
