"""Phase 4 A 类本地化节点:storyline_split_shots(plan_v4 §5 阶段 1)。

检测器两级(2026-11-08 选项C):``STORYLINE_SPLIT_SHOTS_DETECTOR=auto`` 时
TransNetV2 主 venv 进程内推理优先(切镜质量治本),torch/权重缺失或推理失败
自动回退帧差版,回退原因写 error_log 不静默。
"""
from __future__ import annotations

import json
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.split_shots import split_shots as cap_split_shots
from nodes.storyline._common import append_status_tag, _resolve_outputs_root


def storyline_split_shots_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    media_artifact = state.get("storyline_media_artifact")
    if not media_artifact:
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                "[storyline:split_shots] CONTRACT_INVALID: no storyline_media_artifact",
            ],
            "status_log": append_status_tag(
                state, "storyline_split_shots_failed_no_artifact"
            ),
        }

    # 读 manifest(可能是 load_media 或 search_media 输出)
    media_artifact_path = Path(str(media_artifact))
    if not media_artifact_path.exists():
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:split_shots] TOOL_NOT_FOUND: {media_artifact_path}",
            ],
            "status_log": append_status_tag(
                state, "storyline_split_shots_failed_no_file"
            ),
        }

    try:
        manifest = json.loads(media_artifact_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:split_shots] CONTRACT_INVALID: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_split_shots_failed_bad_artifact"
            ),
        }

    media_list = manifest.get("media", []) if isinstance(manifest, dict) else []
    try:
        result = cap_split_shots(media_list)  # list 输入
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:split_shots] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(state, "storyline_split_shots_failed"),
        }

    # 检测器回退不静默(2026-11-08 选项C):TransNetV2 不可用/失败时自动降级
    # 帧差版继续流水线,但原因必须进监控事件流。
    extra_tags: list[str] = []
    extra_errors: list[str] = []
    fallbacks = result.get("detector_fallbacks") or []
    if fallbacks:
        per_media = "; ".join(
            f"{f.get('media_id')}: {f.get('reason')}" for f in fallbacks
        )
        extra_tags.append("storyline_split_shots_transnet_fallback")
        extra_errors.append(
            f"[storyline:split_shots] TRANSNET_FALLBACK: 已回退帧差检测({per_media})"
        )

    # 空切分诊断标记(2026-11-07 实机回归):0 镜头继续流转只会级联出空 storyline
    # (understand/filter/plan 全 no_input),最终在 node_05 fail-closed 终止。
    # 这里先把原因写进 error_log,监控事件流能直接看到切镜层的原因。
    shots = result.get("shots") or {}
    total_clips = sum(len(v or []) for v in shots.values())
    video_count = sum(
        1 for m in media_list if isinstance(m, dict) and m.get("media_type") == "video"
    )
    if video_count and total_clips == 0:
        per_media = ", ".join(
            f"{mid}: {len(v or [])} clips" for mid, v in shots.items()
        )
        return {
            "status_log": append_status_tag(
                state, "storyline_split_shots_empty", *extra_tags
            ),
            "error_log": [
                *list(state.get("error_log", []) or []),
                *extra_errors,
                f"[storyline:split_shots] SPLIT_SHOTS_EMPTY: "
                f"{video_count} 个视频切出 0 个镜头({per_media});"
                f"node_05 将拒绝发布空草稿",
            ],
        }

    out_path = out_dir / "split_shots.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )

    return {
        "storyline_shots_artifact": str(out_path),
        "status_log": append_status_tag(state, "storyline_split_shots_done", *extra_tags),
        "error_log": [
            *list(state.get("error_log", []) or []),
            *extra_errors,
        ],
    }
