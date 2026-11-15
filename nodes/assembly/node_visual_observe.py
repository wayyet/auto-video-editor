"""节点 2/6:``assembly_visual_observe``(第六期 P1-3 候选级观察版)。

对应工具:``build_candidate_observation``(内部复用 ``build_video_observation``)。
职责:读取 P1-2 素材 manifest 的 ``candidate_segments``,对**每个候选片段**
独立抽帧 + 生成 timestamped contact sheet,按 ``candidate_id`` 关联帧索引,
写 ``visual_observation.json`` 供 ``assembly_build_timeline`` 做视觉判断。

(P1-3,设计文档 §3.2 / F-04)本节点不再是"生产拼贴图的旁观者":产物按
candidate_id 一一关联,选段节点拿到的每条候选都带自己的帧证据;抽帧失败
的候选标 ``degraded/unknown``,绝不假装"没有风险"。

(B1 ASR 移除,2026-11-08)本节点原为 ``assembly_asr_and_visual_observe``
复合节点,转写透传已整体删除。

历史形态:整段抽帧 + ``video_ingest.json`` 合并产物随 P1-3 停用 —— 全片
拼贴图与候选无映射关系,正是 F-04 指出的"视觉证据不进决策"的根源。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assembly_capabilities.run_context import RunContext
from assembly_capabilities.visual_observe import build_candidate_observation

from nodes.storyline._common import (
    _resolve_outputs_root,
    append_error,
    append_status_tag,
)
from state import WorkflowState


def _read_json_any(path: Path) -> tuple[Any, str | None]:
    """读 JSON 文件。返回 (data, error);读盘/解析失败时 data=None、error 非空。"""
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as e:
        return None, repr(e)


def assembly_visual_observe_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"
    out_dir.mkdir(parents=True, exist_ok=True)

    media_artifact = state.get("assembly_media_artifact")
    if not media_artifact:
        return {
            "error_log": append_error(
                state, node_kind="assembly_visual_observe", error_code="CONTRACT_INVALID",
                message="missing assembly_media_artifact (run assembly_discover_and_probe first)",
            ),
            "status_log": append_status_tag(state, "assembly_visual_observe_failed"),
        }

    manifest, read_error = _read_json_any(Path(str(media_artifact)))
    if manifest is None:
        return {
            "error_log": append_error(
                state, node_kind="assembly_visual_observe", error_code="CONTRACT_INVALID",
                message=f"failed to read media manifest: {media_artifact} ({read_error})",
            ),
            "status_log": append_status_tag(state, "assembly_visual_observe_failed"),
        }
    if not isinstance(manifest, dict):
        return {
            "error_log": append_error(
                state, node_kind="assembly_visual_observe", error_code="CONTRACT_INVALID",
                message=f"media.json is not a P1-2 manifest (got {type(manifest).__name__}); rerun assembly_discover_and_probe",
            ),
            "status_log": append_status_tag(state, "assembly_visual_observe_failed"),
        }
    if not isinstance(manifest.get("sources"), list):
        return {
            "error_log": append_error(
                state, node_kind="assembly_visual_observe", error_code="CONTRACT_INVALID",
                message="media.json is not a P1-2 manifest (missing sources[]); rerun assembly_discover_and_probe",
            ),
            "status_log": append_status_tag(state, "assembly_visual_observe_failed"),
        }

    ctx = RunContext(session_kind="pipeline")
    try:
        observation = build_candidate_observation(manifest, ctx, out_dir)
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": append_error(
                state, node_kind="assembly_visual_observe", error_code="TOOL_EXECUTION_FAILED",
                message=repr(e),
            ),
            "status_log": append_status_tag(state, "assembly_visual_observe_failed"),
        }

    obs_path = out_dir / "visual_observation.json"
    tags = ["assembly_visual_observe_done"]
    if observation.get("degraded"):
        tags.append("assembly_visual_observe_degraded")

    # ``assembly_ingest_artifact`` 保留指向观察产物(旧 checkpoint 兼容读),
    # 新消费方统一读 ``assembly_observation_artifact``。
    return {
        "assembly_ingest_artifact": str(obs_path),
        "assembly_observation_artifact": str(obs_path),
        "error_log": [],
        "status_log": append_status_tag(state, *tags),
    }
