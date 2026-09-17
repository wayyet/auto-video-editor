"""节点 5:generate_initial_jianying_draft — Phase 2 mapper 接入版。

行为升级(对照 plan §O5 / §5 Phase 2):
- 用 :func:`storyline.mapper.canonical_to_draft` 替换旧
  ``_build_draft_content(shot_plan, video_path)``。
- 输入优先级:
    1. ``state["storyline_plan"]``(来自 node_04 真实 MCP 链路)→ mapper
    2. ``state["shot_plan"]``(Mock 兜底)→ 旧 ``_build_draft_content`` 兜底
- 原子写入 ``draft_content.json``(沿用 ``draft_ops.atomic_writer.atomic_write_draft``)。
- 写完回填 ``manifest.draft_path``(幂等键下一次直接命中)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from config import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    JIANYING_VERSION,
    WORKFLOW_ENV,
    storyline_outputs_root,
)
from draft_ops.atomic_writer import atomic_write_draft
from draft_ops.encryption_detector import DraftStatus, detect_draft_encryption
from draft_ops.version_strategy import resolve_strategy
from mcp_clients.openstoryline_client import ContractInvalid
from state import WorkflowState
from storyline.contract import CanonicalTimeline
from storyline.mapper import canonical_to_draft
from storyline.output_isolation import (
    OutputJobPaths,
    build_manifest,
    compute_idempotency_key,
    compute_input_sha256,
    is_idempotent_hit,
    load_manifest,
    write_manifest,
)


def generate_initial_jianying_draft(
    state: WorkflowState,
    draft_dir: Path,
    *,
    encrypt_detector=detect_draft_encryption,
    writer=atomic_write_draft,
) -> dict:
    """生成剪映初始草稿文件(Phase 2 接入 Canonical Timeline mapper)。

    Args:
        state: 当前工作流状态(优先 ``storyline_plan``;fallback 到 ``shot_plan``)。
        draft_dir: 剪映草稿目录(含或待生成 draft_content.json)。
        encrypt_detector: 可注入的加密检测函数,默认 detect_draft_encryption。
        writer: 可注入的原子写入函数,默认 atomic_write_draft(必须用)。
    """
    errors = list(state.get("error_log", []) or [])

    # 1. 加密检测
    status = encrypt_detector(draft_dir)
    if status == DraftStatus.ENCRYPTED:
        errors.append(
            "[node_05] 检测到已加密草稿,中止写入,需人工核查剪映版本"
        )
        return {
            **state,
            "draft_path": None,
            "draft_encryption_status": status.value,
            "error_log": errors,
        }

    # 2. 策略选择
    strategy = resolve_strategy(jianying_version=JIANYING_VERSION)

    # 3. 优先 storyline_plan(真实 MCP);fallback shot_plan(Mock)
    storyline_plan = state.get("storyline_plan")
    shot_plan = state.get("shot_plan")
    video_path = state.get("video_input_path") or ""
    job_id = state.get("session_id")

    try:
        if storyline_plan:
            draft_content = _build_from_canonical(storyline_plan)
        elif shot_plan:
            draft_content = _build_draft_content_legacy(shot_plan=shot_plan, video_path=video_path)
        else:
            # 两个都空:写空草稿(materials.audios 留空,tracks 给占位空)
            draft_content = {
                "canvas_config": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
                "materials": {"videos": [], "audios": [], "texts": []},
                "tracks": [],
            }
    except (ContractInvalid, ValueError) as e:
        errors.append(f"[node_05] canonical plan invalid: {e!r}")
        # mapper 失败:走早退,不污染 draft_content.json
        return {
            **state,
            "draft_path": None,
            "draft_encryption_status": status.value,
            "draft_version_strategy": strategy.value,
            "error_log": errors,
        }

    # 4. 原子写入
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_file = draft_dir / "draft_content.json"
    writer(draft_file, draft_content)

    # 5. 回填 manifest.draft_path(Phase 2 幂等键)
    if job_id:
        paths = OutputJobPaths.for_job(job_id=str(job_id), root=storyline_outputs_root())
        paths.ensure()
        manifest = load_manifest(paths) or build_manifest(
            job_id=str(job_id),
            video_path=str(video_path),
            config_snapshot={
                "WORKFLOW_ENV": WORKFLOW_ENV,
                "CANVAS_WIDTH": CANVAS_WIDTH,
                "CANVAS_HEIGHT": CANVAS_HEIGHT,
                "JIANYING_VERSION": JIANYING_VERSION,
            },
            storyline_session_id=state.get("storyline_session_id"),
            storyline_artifact_ids=[
                a.get("artifact_id", "") for a in (state.get("storyline_artifacts") or [])
            ],
            draft_path=str(draft_file),
        )
        manifest.draft_path = str(draft_file)
        # 重新算幂等键(因为 draft_path 进入 candidates)
        manifest.idempotency_key = compute_idempotency_key(
            job_id=str(job_id),
            input_sha256=manifest.input_sha256,
            config_sha256=manifest.config_sha256,
        )
        write_manifest(paths, manifest)

    return {
        **state,
        "draft_path": str(draft_file),
        "draft_encryption_status": status.value,
        "draft_version_strategy": strategy.value,
        "error_log": errors,
    }


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _build_from_canonical(storyline_plan: dict) -> dict:
    """``state['storyline_plan']`` → draft_content.json dict(走 mapper)。

    Phase 2 必跑 Pydantic 校验;失败抛 :class:`ContractInvalid` 让 node 早退。
    """
    # CanonicalTimeline.model_validate 会跑不变量校验(单调用,慢路径)
    canonical = CanonicalTimeline.model_validate(storyline_plan)
    return canonical_to_draft(canonical)


def _build_draft_content_legacy(shot_plan: dict, video_path: str) -> dict:
    """旧 ``_build_draft_content`` 行为保留(shot_plan Mock fallback 用)。"""
    return {
        "canvas_config": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "materials": {"videos": _shot_plan_to_materials(shot_plan, video_path)},
        "tracks": _init_empty_tracks(),
    }


# 向后兼容别名:既有测试 ``test_node_05_generate_draft.py`` 仍以
# ``_build_draft_content`` 名字引用该函数,沿用即可。
_build_draft_content = _build_draft_content_legacy


def _shot_plan_to_materials(shot_plan: dict, video_path: str) -> list[dict]:
    """最小可用版:把每个 shot 的 video_ref 转为一个 material 条目。"""
    shots = shot_plan.get("shots", []) if isinstance(shot_plan, dict) else []
    materials: list[dict] = []
    for idx, shot in enumerate(shots):
        materials.append(
            {
                "id": f"video-{idx + 1}",
                "type": "video",
                "path": video_path,
                "shot_id": shot.get("id") if isinstance(shot, dict) else None,
                "start_s": shot.get("start_s") if isinstance(shot, dict) else None,
                "end_s": shot.get("end_s") if isinstance(shot, dict) else None,
            }
        )
    return materials


def _init_empty_tracks() -> list[dict]:
    """最小可用版:返回空轨道结构,片段填充留给后续周次(步骤 7 变速节点等)。"""
    return []


__all__ = ["generate_initial_jianying_draft"]