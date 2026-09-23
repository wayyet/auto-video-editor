"""节点 3/6:``assembly_build_timeline``(plan §7.3)。

**这是 6 节点中唯一需要调用 LLM 的节点**。输入是前两步产物
``media.json`` / ``transcript.json`` / ``video_ingest.json``,让模型看截图
+ 读转写文字,按 ``video-edit-assembly`` 第 3-5 阶段规则(分组评分选段 →
定版式与结构 → 搭建 project 风格 timeline)做选段和排序判断,按
``schemas/timeline.schema.json`` 写出 ``timeline.json``。

按 plan §3.3:"本方案里要做一个**会调用 LLM/VLM 的节点**(跟仓库里现成的
``storyline_generate_script`` 节点做法一样),不是零 AI 调用的确定性代码"。

**阶段三实施范围**:本节点通过 ``assembly_capabilities.build_timeline`` 复用
``storyline_capabilities.vlm_client`` 网关调用 LLM;LLM 输出合法选段 JSON
时优先用 LLM 结果,LLM 失败/空输出时按 ADR-3 的"软降级不阻断"原则回退到
确定性占位算法(从 candidate_segments 按顺序取前 N 段),保证下游
``validate_timeline`` / ``render_preview`` / ``qc_preview`` 链路总有合法
timeline 可跑。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assembly_capabilities.build_timeline import build_timeline_from_paths

from nodes.storyline._common import (
    _resolve_outputs_root,
    append_error,
    append_status_tag,
)
from state import WorkflowState


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

    # 节点层先做轻量校验:media_artifact 必须是合法 JSON list(plan §7.3 + §11)。
    # 下游 ``build_timeline_from_paths`` 内部会再读一次,这里提前 fail-fast,
    # 把"损坏的产物"显式记到 error_log,而不是静默走 fallback。
    try:
        raw_media = Path(str(media_artifact)).read_text(encoding="utf-8")
        parsed = json.loads(raw_media)
    except (OSError, json.JSONDecodeError) as e:
        return {
            "error_log": append_error(
                state, node_kind="assembly_build_timeline", error_code="CONTRACT_INVALID",
                message=f"failed to read media.json: {e!r}",
            ),
            "status_log": append_status_tag(state, "assembly_build_timeline_failed"),
        }
    if not isinstance(parsed, list):
        return {
            "error_log": append_error(
                state, node_kind="assembly_build_timeline", error_code="CONTRACT_INVALID",
                message=f"media.json must be a JSON list, got {type(parsed).__name__}",
            ),
            "status_log": append_status_tag(state, "assembly_build_timeline_failed"),
        }

    transcript_artifact = state.get("assembly_transcript_artifact")
    ingest_artifact = state.get("assembly_ingest_artifact")

    try:
        timeline_data = build_timeline_from_paths(
            media_artifact=media_artifact,
            transcript_artifact=transcript_artifact,
            ingest_artifact=ingest_artifact,
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