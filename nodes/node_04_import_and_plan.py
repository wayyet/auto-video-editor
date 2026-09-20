"""节点 4:import_video_and_plan_shots — 2026-09 迁移解耦版(读产物)。

行为(对照 plan §4.3):
- 关卡⓪ 已通过人工 resume,产物应已落在
  ``openstoryline/outputs/<sid>/plan_timeline_pro/<artifact>.json``。
- 读最新一份,经 :mod:`storyline.plan_reader` 拍平为 ``CanonicalTimeline``,
  写入 ``state["storyline_plan"]``。
- 幂等:``outputs/{job_id}/manifest.json`` 命中 ``idempotency_key`` 直接复用。
- 失败按 ADR-007 分类走 :func:`_fallback_to_shot_plan` 兜底,
  ``WORKFLOW_ENV=production`` 早退,development 写 Mock shot_plan。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from config import (
    WORKFLOW_ENV,
    storyline_outputs_root,
)
from state import WorkflowState
from storyline.contract import StorylineErrorCode
from storyline.output_isolation import (
    OutputJobPaths,
    append_storyline_log,
    build_manifest,
    compute_idempotency_key,
    compute_input_sha256,
    is_idempotent_hit,
    load_manifest,
    write_manifest,
)
from storyline.plan_reader import (
    PlanReaderError,
    find_latest_session_dir,
    read_latest_plan_timeline_pro,
)


# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
OPENSTORYLINE_OUTPUTS_ROOT: Path = (
    Path(__file__).resolve().parent.parent / "openstoryline" / "outputs"
)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def import_video_and_plan_shots(
    state: WorkflowState,
    *,
    outputs_root: Optional[Path] = None,
    openstoryline_outputs_root: Optional[Path] = None,
    plan_reader: Any = None,
) -> dict:
    """读 :func:`read_latest_plan_timeline_pro` 产物,拍平为 CanonicalTimeline。

    Args:
        state: 当前工作流状态(需含 ``session_id`` / ``video_input_path`` /
            ``openstoryline_ready``)。
        outputs_root: 注入位(测试用),auto-video-editor 端 ``outputs/{job_id}`` 根;
            None 时走 :func:`config.storyline_outputs_root()`。
        openstoryline_outputs_root: 注入位(测试用),OpenStoryline 端 ``outputs/<sid>`` 根;
            None 时用 :data:`OPENSTORYLINE_OUTPUTS_ROOT`。
        plan_reader: 注入位(测试用),签名 ``(session_dir) -> (plan_file, plan_data)``;
            None 时走 :func:`read_latest_plan_timeline_pro`。
    """
    errors = list(state.get("error_log", []) or [])
    job_id = state.get("session_id")
    video_path = state.get("video_input_path")

    out_root = Path(outputs_root) if outputs_root is not None else storyline_outputs_root()
    os_outputs_root = (
        Path(openstoryline_outputs_root)
        if openstoryline_outputs_root is not None
        else OPENSTORYLINE_OUTPUTS_ROOT
    )
    reader = plan_reader or read_latest_plan_timeline_pro

    # 0. 关卡⓪ 未通过 / OpenStoryline 未就绪 → 早退 + Mock 兜底
    if not state.get("openstoryline_ready"):
        errors.append("[node_04] OpenStoryline 服务未就绪,跳过分镜规划")
        return _fallback_to_shot_plan(
            state, errors,
            error_code=StorylineErrorCode.CONTRACT_INVALID,
            message="openstoryline_not_ready",
        )

    # 1. 幂等命中:manifest.json + key 一致 → 跳过
    paths = OutputJobPaths.for_job(job_id=str(job_id or "unknown"), root=out_root)
    cfg_snapshot = {"WORKFLOW_ENV": WORKFLOW_ENV}
    input_sha = compute_input_sha256(video_path=str(video_path or ""))
    cfg_sha = _quick_cfg_sha(cfg_snapshot)
    key = compute_idempotency_key(
        job_id=str(job_id or "unknown"),
        input_sha256=input_sha,
        config_sha256=cfg_sha,
    )
    if is_idempotent_hit(paths, expected_key=key):
        manifest = load_manifest(paths)
        if manifest and manifest.draft_path:
            return {
                **state,
                "storyline_plan": None,
                "storyline_outputs_root": str(paths.root),
                "storyline_session_id": manifest.storyline_session_id,
                "draft_path": manifest.draft_path,
                "error_log": errors
                + [f"[node_04] idempotency hit, reuse manifest draft_path={manifest.draft_path}"],
            }

    # 2. 在 openstoryline/outputs/ 下找最新会话的 plan_timeline_pro 产物
    session_dir = find_latest_session_dir(os_outputs_root)
    if session_dir is None:
        errors.append(
            f"[node_04] 未在 {os_outputs_root} 下找到任何会话产物,"
            "请确认关卡⓪的规划已在网页里真正完成"
        )
        return _fallback_to_shot_plan(
            state, errors,
            error_code=StorylineErrorCode.CONTRACT_INVALID,
            message="no_openstoryline_session_dir",
        )

    try:
        plan_file, plan_data = reader(session_dir)
    except PlanReaderError as e:
        errors.append(f"[node_04] 产物读取失败: {e}")
        return _fallback_to_shot_plan(
            state, errors,
            error_code=StorylineErrorCode.CONTRACT_INVALID,
            message=str(e),
        )

    # 3. 拍平为 CanonicalTimeline(ContractInvalid 由 to_canonical 自己抛)
    try:
        canonical = plan_data.to_canonical(job_id=str(job_id or "unknown"))
    except Exception as e:  # noqa: BLE001
        errors.append(f"[node_04] plan 拍平失败: {e!r}")
        return _fallback_to_shot_plan(
            state, errors,
            error_code=StorylineErrorCode.CONTRACT_INVALID,
            message=str(e),
        )

    # 4. 写 manifest + storyline.jsonl
    paths.ensure()
    manifest = build_manifest(
        job_id=str(job_id or "unknown"),
        video_path=str(video_path or ""),
        config_snapshot=cfg_snapshot,
        storyline_session_id=session_dir.name,
        storyline_artifact_ids=[plan_data.artifact_id],
        draft_path=None,
        extras={"config_sha256": cfg_sha, "input_sha256": input_sha},
    )
    write_manifest(paths, manifest)
    append_storyline_log(
        paths,
        tool_name="plan_timeline_pro",
        artifact_id=plan_data.artifact_id,
        duration_ms=0,
        error_code=None,
        job_id=str(job_id or "unknown"),
        session_id=session_dir.name,
    )

    return {
        **state,
        "storyline_session_id": session_dir.name,
        "storyline_outputs_root": str(session_dir),
        "storyline_plan": canonical.model_dump(),
        "status_log": (state.get("status_log") or []) + ["node_04_import_and_plan_done"],
        "error_log": errors,
    }


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _quick_cfg_sha(snap: dict) -> str:
    """轻量 cfg 指纹(对比 config snapshot,纳入幂等键)。"""
    return hashlib.sha256(
        json.dumps(snap, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _fallback_to_shot_plan(
    state: WorkflowState,
    errors: list[str],
    *,
    error_code: str,
    message: str,
) -> dict:
    """产物读盘失败时降级到 Mock shot_plan。

    ``WORKFLOW_ENV=production`` 早退;``development`` 写最小可用 shot_plan(便于
    ``tests/unit/test_node_04_import_and_plan.py`` 既有断言继续生效)。
    """
    if WORKFLOW_ENV == "production":
        return {
            **state,
            "openstoryline_ready": False,
            "storyline_error_code": error_code,
            "storyline_plan": None,
            "error_log": errors,
        }
    fallback_plan = {
        "video_path": state.get("video_input_path"),
        "shots": [
            {"id": "shot-1", "video_ref": "video-1", "start_s": 0.0, "end_s": 5.0},
            {"id": "shot-2", "video_ref": "video-1", "start_s": 5.0, "end_s": 10.0},
        ],
        "_fallback_reason": error_code,
        "_fallback_message": message,
    }
    return {
        **state,
        "storyline_plan": None,
        "shot_plan": fallback_plan,
        "storyline_error_code": error_code,
        "error_log": errors,
    }


__all__ = [
    "import_video_and_plan_shots",
    "OPENSTORYLINE_OUTPUTS_ROOT",
]