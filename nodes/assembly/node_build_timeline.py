"""节点 3/6:``assembly_build_timeline``(第六期 P1-4 候选计划版)。

**这是 6 节点中唯一需要调用 LLM 的节点**。

P1-4(设计文档 §3.3 / F-05):本节点不再产出独立无关的 assembly timeline,
而是基于**原始 ``storyline_plan``(不可变基线)**生成候选编辑计划:

1. 读 manifest(P1-2)的候选片段 + 观察产物(P1-3)的候选级视觉证据;
2. LLM 选段(默认 Stub,选段失败走确定性证据 ops)+ manifest 黑帧区间的
   确定性 trim/remove ops → ``draft_patch.json``(DraftPatch);
3. ``apply_patch`` 应用到基线深拷贝 → ``candidate_plan.json``
   (候选 CanonicalTimeline,``CanonicalTimeline.model_validate`` 硬门);
4. ``timeline.json`` = 候选计划的投影(供 validate/render/qc 工具链沿用);
5. 影响 trace 追加本节点 contribution。

无有效候选 / LLM 失败 → 保留基线计划并标 ``no_safe_candidate``,**不再把
空候选统一转成"全片首尾相接"**(F-03 根治)。

state 无 ``storyline_plan`` 时(单元测试 / 无基线兜底)回退旧独立 timeline
路径,行为与 P1-4 之前一致。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assembly_capabilities.build_timeline import (
    build_candidate_plan_from_media,
    build_timeline_from_paths,
    candidate_projection_timeline,
    manifest_to_media_reports,
)
from assembly_capabilities.influence_trace import (
    add_contribution,
    canonical_json_sha256,
    load_or_create_trace,
    save_trace,
)
from assembly_capabilities.draft_patch import plan_sha256

from nodes.storyline._common import (
    _resolve_outputs_root,
    append_error,
    append_status_tag,
)
from state import WorkflowState


def _read_media_json(media_artifact: str) -> Any | None:
    try:
        return json.loads(Path(str(media_artifact)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _append_influence_trace(
    out_dir: Path,
    base_plan_sha256: str | None,
    *,
    node_id: str,
    artifact: str,
    consumed_by: list[str],
    affected_operations: list[str],
    final_clip_ids: list[str],
) -> str:
    """追加本节点 contribution 到 ``influence_trace.json``(P1-0 契约)。"""
    trace = load_or_create_trace(
        out_dir / "influence_trace.json",
        base_plan_sha256 or canonical_json_sha256({"base_plan_sha256": None}),
    )
    add_contribution(
        trace,
        node_id=node_id,
        artifact=artifact,
        consumed_by=consumed_by,
        affected_operations=affected_operations,
        final_clip_ids=final_clip_ids,
    )
    save_trace(out_dir / "influence_trace.json", trace)
    return str(out_dir / "influence_trace.json")


def assembly_build_timeline_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"
    out_dir.mkdir(parents=True, exist_ok=True)

    media_artifact = state.get("assembly_media_artifact")
    if not media_artifact:
        return {
            "error_log": append_error(
                state, node_kind="assembly_build_timeline", error_code="CONTRACT_INVALID",
                message="missing assembly_media_artifact (run assembly_discover_and_probe first)",
            ),
            "status_log": append_status_tag(state, "assembly_build_timeline_failed"),
        }

    # 节点层先做轻量校验:media_artifact 必须是合法 JSON(2026-11-15 P1-2 起
    # 为 manifest dict,兼容旧 list 产物)。下游内部会再读一次,这里提前
    # fail-fast,把"损坏的产物"显式记到 error_log,而不是静默走 fallback。
    parsed = _read_media_json(str(media_artifact))
    if parsed is None:
        return {
            "error_log": append_error(
                state, node_kind="assembly_build_timeline", error_code="CONTRACT_INVALID",
                message=f"failed to read media.json: {media_artifact}",
            ),
            "status_log": append_status_tag(state, "assembly_build_timeline_failed"),
        }
    if not isinstance(parsed, (list, dict)):
        return {
            "error_log": append_error(
                state, node_kind="assembly_build_timeline", error_code="CONTRACT_INVALID",
                message=f"media.json must be a JSON list or manifest dict, got {type(parsed).__name__}",
            ),
            "status_log": append_status_tag(state, "assembly_build_timeline_failed"),
        }

    # P1-3:候选级视觉观察是选段的第一证据源(旧整段 ingest 仅作回落)
    observation_artifact = (
        state.get("assembly_observation_artifact") or state.get("assembly_ingest_artifact")
    )
    observation_data = None
    if observation_artifact:
        observation_data = _read_media_json(str(observation_artifact))

    base_plan = state.get("storyline_plan")
    has_baseline = (
        isinstance(base_plan, dict)
        and isinstance(base_plan.get("clips"), list)
        and bool(base_plan["clips"])
    )

    # ---- P1-4 主路径:有 storyline_plan 基线 → DraftPatch + 候选计划 ----
    if has_baseline:
        media_reports = manifest_to_media_reports(parsed)
        base_sha = (
            str(state.get("assembly_base_plan_sha256"))
            if state.get("assembly_base_plan_sha256")
            else plan_sha256(base_plan)
        )
        try:
            result = build_candidate_plan_from_media(
                base_plan=base_plan,
                base_plan_sha256=base_sha,
                media_reports=media_reports,
                observation_data=observation_data,
                lang="zh",
                client=None,
            )
        except Exception as e:  # noqa: BLE001
            return {
                "error_log": append_error(
                    state, node_kind="assembly_build_timeline", error_code="TOOL_EXECUTION_FAILED",
                    message=repr(e),
                ),
                "status_log": append_status_tag(state, "assembly_build_timeline_failed"),
            }

        if not result["validation"]["pass"]:
            # 硬门失败:候选与基线都可能非法 —— 显式失败,不静默放行
            return {
                "error_log": append_error(
                    state, node_kind="assembly_build_timeline", error_code="CONTRACT_INVALID",
                    message=f"candidate plan invalid: {result['validation']['error']}",
                ),
                "status_log": append_status_tag(state, "assembly_build_timeline_failed"),
            }

        candidate_path = out_dir / "candidate_plan.json"
        candidate_path.write_text(
            json.dumps(result["candidate_plan"], ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        patch_path = out_dir / "draft_patch.json"
        patch_path.write_text(
            json.dumps(result["patch"], ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        projection = candidate_projection_timeline(result["candidate_plan"], media_reports)
        timeline_path = out_dir / "timeline.json"
        timeline_path.write_text(
            json.dumps(projection, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        trace_path = _append_influence_trace(
            out_dir, base_sha,
            node_id="assembly_build_timeline",
            artifact="assembly/candidate_plan.json",
            consumed_by=["assembly_validate_render_qc", "generate_draft"],
            affected_operations=[
                str(op.get("operation_id")) for op in result["patch"].get("operations", [])
            ],
            final_clip_ids=[str(c.get("clip_id")) for c in result["candidate_plan"].get("clips", [])],
        )

        tags = ["assembly_build_timeline_done"]
        if result["no_safe_candidate"]:
            tags.append("assembly_build_timeline_no_safe_candidate")
        if result["patch_report"]["skipped_operations"]:
            tags.append("assembly_build_timeline_ops_skipped")

        return {
            "assembly_candidate_plan": result["candidate_plan"],
            "assembly_candidate_plan_path": str(candidate_path),
            "assembly_candidate_plan_sha256": result["candidate_plan_sha256"],
            "assembly_draft_patch": result["patch"],
            "assembly_base_plan_sha256": base_sha,
            "assembly_timeline_path": str(timeline_path),
            "assembly_influence_trace_path": trace_path,
            "status_log": append_status_tag(state, *tags),
        }

    # ---- 旧路径兜底:无 storyline_plan 基线(单元测试 / 非主图调用) ----
    try:
        timeline_data = build_timeline_from_paths(
            media_artifact=media_artifact,
            observation_artifact=observation_artifact,
            lang="zh",
            client=None,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": append_error(
                state, node_kind="assembly_build_timeline", error_code="TOOL_EXECUTION_FAILED",
                message=repr(e),
            ),
            "status_log": append_status_tag(state, "assembly_build_timeline_failed"),
        }

    out_path = out_dir / "timeline.json"
    out_path.write_text(
        json.dumps(timeline_data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return {
        "assembly_timeline_path": str(out_path),
        "status_log": append_status_tag(state, "assembly_build_timeline_done"),
    }


__all__ = ["assembly_build_timeline_node"]


# 阶段二占位实现保留为内部 helper,以便测试用例显式覆盖"无 LLM 选段时的回退路径"。
# 阶段三默认走 build_timeline_from_paths(LLM 优先 + fallback);以下 helper
# 仅在单测/回退脚本里调用,不在节点主入口出现。
def _build_placeholder_timeline(media_reports: list[dict[str, Any]]) -> dict[str, Any]:
    """阶段二占位实现(由 ``assembly_capabilities.build_timeline._fallback_chosen``
    提供等价行为;此处保留以便直接构造测试 fixture)。"""
    from assembly_capabilities.build_timeline import (
        _collect_candidate_segments,
        _build_assets,
        _build_clips,
        _fallback_chosen,
        _resolve_canvas,
        DEFAULT_FPS,
        DEFAULT_CANVAS_HEIGHT,
        DEFAULT_CANVAS_WIDTH,
    )

    candidates = _collect_candidate_segments(media_reports)
    chosen = _fallback_chosen(candidates)
    assets, idx_map = _build_assets(media_reports)
    clips = _build_clips(chosen, media_index_to_asset_id=idx_map)
    canvas = _resolve_canvas({})
    total_duration = sum((c["end"] - c["start"]) for c in clips)
    return {
        "project": {"name": "assembly-placeholder"},
        "assets": assets,
        "sequence": {
            "duration": total_duration,
            "fps": canvas.get("fps", DEFAULT_FPS),
            "canvas": {
                "width": canvas.get("width", DEFAULT_CANVAS_WIDTH),
                "height": canvas.get("height", DEFAULT_CANVAS_HEIGHT),
                "fps": canvas.get("fps", DEFAULT_FPS),
            },
        },
        "tracks": [
            {
                "type": "video",
                "name": "video_main",
                "enabled": True,
                "visible": True,
                "order": 0,
                "clips": clips,
            }
        ],
        "metadata": {"assembly_placeholder": True},
    }
