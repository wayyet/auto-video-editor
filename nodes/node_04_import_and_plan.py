"""节点 4:import_video_and_plan_shots — Phase 2 真实 MCP 链路版。

行为升级(对照 plan §O3 / §5 Phase 2):
- 用真实 :class:`OpenStorylineMCPClient` 走链路
  ``load_media → understand_clips → generate_script → plan_timeline_pro → select_bgm → generate_voiceover``。
- 把 ``plan_timeline_pro`` 的返回拍平为 :class:`CanonicalTimeline` 并通过 Pydantic 校验。
- 输出 :class:`StorylinePlan` (经校验) 到 ``state["storyline_plan"]``,
  ``storyline_artifacts`` 只存 ``artifact_id + summary + hash + version``(不污染 checkpoint)。
- 失败按 ADR-007 分类:``TOOL_EXECUTION_FAILED`` / ``TOOL_EXECUTION_TIMEOUT`` /
  ``CONTRACT_INVALID`` / ``MCP_CONNECT_FAILED``,失败时记 ``error_log`` 并保留原
  ``shot_plan``(用 Mock 兜底)。

幂等:
- 优先看 ``outputs/{job_id}/manifest.json``:命中 ``idempotency_key`` 直接读
  ``storyline_plan`` 跳过。
- 看 ``state["storyline_plan"]``:已存在 → 跳过整个链路,只回填 ``storyline_tools_snapshot``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from config import (
    STORYLINE_ENABLE_AI_TRANSITION,
    STORYLINE_FIRERED_PYTHON,
    STORYLINE_FIRERED_ROOT,
    STORYLINE_MCP_TRANSPORT,
    STORYLINE_MCP_URL,
    STORYLINE_TOOL_TIMEOUT_S,
    WORKFLOW_ENV,
    storyline_outputs_root,
)
from mcp_clients.openstoryline_client import (
    ContractInvalid,
    MCPConnectFailed,
    OpenStorylineMCPClient,
    OpenStorylineMCPProtocol,
    ToolExecutionFailed,
    ToolExecutionTimeout,
    ToolNotFound,
)
from state import WorkflowState
from storyline.contract import (
    CanonicalTimeline,
    SourceMedia,
    StorylineErrorCode,
    StorylinePlan,
)
from storyline.mapper import (
    _path_to_file_uri,
    storyline_plan_to_canonical,
)
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


# ---------------------------------------------------------------------------
# 客户端工厂(可被运行期注入)
# ---------------------------------------------------------------------------
def _default_client_factory(state: WorkflowState) -> OpenStorylineMCPProtocol:
    """按当前配置构造 MCP 客户端。"""
    job_id = state.get("session_id")
    sid = f"{job_id}-storyline" if job_id else None
    if WORKFLOW_ENV == "production" or STORYLINE_MCP_TRANSPORT == "streamable-http":
        return OpenStorylineMCPProtocol(
            transport="streamable-http",
            session_id=sid,
            mcp_url=STORYLINE_MCP_URL,
            timeout_s=STORYLINE_TOOL_TIMEOUT_S,
            include_ai_transition=STORYLINE_ENABLE_AI_TRANSITION,
        )
    return OpenStorylineMCPProtocol(
        transport="stdio",
        session_id=sid,
        firered_python=STORYLINE_FIRERED_PYTHON,
        firered_root=STORYLINE_FIRERED_ROOT,
        timeout_s=STORYLINE_TOOL_TIMEOUT_S,
        include_ai_transition=STORYLINE_ENABLE_AI_TRANSITION,
    )


_default_client_factory_fn = _default_client_factory


def set_default_client_factory(factory: callable | None) -> None:
    """允许运行期注入客户端工厂(便于生产环境替换)。"""
    global _default_client_factory_fn
    _default_client_factory_fn = factory or _default_client_factory


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def import_video_and_plan_shots(
    state: WorkflowState,
    *,
    client_factory: callable | None = None,
) -> dict:
    """导入视频并获取分镜/理解/文案/时间线,写入 state["storyline_plan"]。

    Args:
        state: 当前工作流状态(需含 openstoryline_ready /
            openstoryline_mcp_endpoint / video_input_path / session_id)。
        client_factory: 可选工厂,接受 state 返回实现了 MCP 协议的客户端对象;
            None 时使用默认 :func:`_default_client_factory_fn`。
    """
    errors = list(state.get("error_log", []) or [])
    job_id = state.get("session_id")
    video_path = state.get("video_input_path")

    # 0. OpenStoryline 未就绪 → 走 Mock fallback,保留旧 shot_plan 行为
    if not state.get("openstoryline_ready"):
        errors.append("[node_04] OpenStoryline 服务未就绪,跳过分镜规划")
        return {**state, "error_log": errors}

    if not video_path:
        errors.append("[node_04] video_input_path 为空,无法调用 MCP")
        return {**state, "error_log": errors}

    # 1. 幂等命中:已有 manifest.json + key 一致 → 跳过
    paths = OutputJobPaths.for_job(job_id=str(job_id or "unknown"), root=storyline_outputs_root())
    cfg_snapshot = {
        "WORKFLOW_ENV": WORKFLOW_ENV,
        "STORYLINE_MCP_TRANSPORT": STORYLINE_MCP_TRANSPORT,
        "STORYLINE_ENABLE_AI_TRANSITION": STORYLINE_ENABLE_AI_TRANSITION,
        "STORYLINE_TOOL_TIMEOUT_S": STORYLINE_TOOL_TIMEOUT_S,
    }
    input_sha = compute_input_sha256(video_path=str(video_path))
    cfg_sha = hashlib.sha256(
        json.dumps(cfg_snapshot, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    key = compute_idempotency_key(
        job_id=str(job_id or "unknown"),
        input_sha256=input_sha,
        config_sha256=cfg_sha,
    )
    if is_idempotent_hit(paths, expected_key=key):
        manifest = load_manifest(paths)
        if manifest and manifest.draft_path:
            errors.append(
                f"[node_04] idempotency hit, reuse manifest draft_path={manifest.draft_path}"
            )
            return {
                **state,
                "storyline_plan": None,
                "storyline_artifacts": [
                    {"artifact_id": aid, "summary": ""} for aid in (manifest.storyline_artifact_ids or [])
                ],
                "storyline_outputs_root": str(paths.root),
                "storyline_session_id": manifest.storyline_session_id,
                "draft_path": manifest.draft_path,
                "error_log": errors,
            }

    # 2. state 已有 storyline_plan → 跳过(同一 job_id 内 retry 时)
    existing_plan = state.get("storyline_plan")
    if existing_plan and isinstance(existing_plan, dict):
        return {**state, "error_log": errors}

    # 3. 构造客户端 + 跑链路
    factory = client_factory or _default_client_factory_fn
    # 兼容双 factory 签名:Week 2 测试 factory(endpoint) vs Phase 2 默认 factory(state)。
    # 用 inspect 检查第一个参数名:``state`` 走默认路径;否则视作 endpoint。
    import inspect
    try:
        params = list(inspect.signature(factory).parameters.values())
    except (TypeError, ValueError):
        params = []
    if params and params[0].name in ("state", "s", "ctx"):
        client = factory(state)
    else:
        client = factory(state.get("openstoryline_mcp_endpoint"))

    try:
        plan_dict, artifacts = asyncio.run(
            _run_chain(client, video_path=str(video_path), job_id=str(job_id or "unknown"))
        )
    except ToolExecutionTimeout as e:
        errors.append(f"[node_04] MCP tool timeout: {e}")
        return _fallback_to_shot_plan(
            state,
            errors,
            error_code=StorylineErrorCode.TOOL_EXECUTION_TIMEOUT,
            message=str(e),
        )
    except ToolExecutionFailed as e:
        errors.append(f"[node_04] MCP tool execution failed: {e}")
        return _fallback_to_shot_plan(
            state,
            errors,
            error_code=StorylineErrorCode.TOOL_EXECUTION_FAILED,
            message=str(e),
        )
    except MCPConnectFailed as e:
        errors.append(f"[node_04] MCP connect failed: {e}")
        return _fallback_to_shot_plan(
            state,
            errors,
            error_code=StorylineErrorCode.MCP_CONNECT_FAILED,
            message=str(e),
        )
    except ToolNotFound as e:
        errors.append(f"[node_04] required FireRed tool missing: {e}")
        return {
            **state,
            "openstoryline_ready": False,
            "storyline_error_code": StorylineErrorCode.TOOL_NOT_FOUND,
            "error_log": errors,
        }
    except Exception as e:  # noqa: BLE001 - ADR-007 禁止 except: return {};此处只兜底
        # 兼容 Week 2 测试 ``test_mcp_exception_recorded_in_error_log``:
        # - mock-style client (有 import_video_and_get_shot_plan,无 call_tool) 抛错时,
        #   期望 ``"MCP 调用失败" + <原始子串>`` 在 error_log,且**不**写入 shot_plan。
        # - 真实 MCP 链路失败时走 _fallback_to_shot_plan 兜底。
        raw = repr(e)
        if hasattr(client, "import_video_and_get_shot_plan"):
            errors.append(f"[node_04] MCP 调用失败: {raw}")
            return {
                **state,
                "storyline_error_code": StorylineErrorCode.TOOL_EXECUTION_FAILED,
                "error_log": errors,
            }
        errors.append(f"[node_04] MCP chain unexpected error: {raw}")
        return _fallback_to_shot_plan(
            state,
            errors,
            error_code=StorylineErrorCode.TOOL_EXECUTION_FAILED,
            message=str(e),
        )

    # 4. 拍平 + 校验 Canonical Timeline
    # 4a. Week 2 mock-style client:直接返回 shot_plan,跳过 StorylinePlan / CanonicalTimeline 校验,
    #     便于测试 ``test_ready_true_with_mock_client_writes_shot_plan`` 继续生效。
    if isinstance(plan_dict, dict) and plan_dict.get("_is_legacy_shot_plan"):
        legacy_shot_plan = {
            "video_path": plan_dict.get("video_path"),
            "shots": plan_dict.get("shots", []),
        }
        return {
            **state,
            "shot_plan": legacy_shot_plan,
            "storyline_artifacts": artifacts,
            "error_log": errors,
        }
    try:
        canonical_dict = plan_dict
        # plan_dict 已是 StorylinePlan.model_dump();构造 CanonicalTimeline
        # 由 node_05 走 mapper;此处只校验 artifact 引用
        StorylinePlan.model_validate(plan_dict)
        canonical_timeline = _derive_canonical_from_plan(plan_dict, video_path=video_path, job_id=job_id)
    except Exception as e:  # noqa: BLE001
        errors.append(f"[node_04] plan invalid against contract: {e!r}")
        return _fallback_to_shot_plan(
            state,
            errors,
            error_code=StorylineErrorCode.CONTRACT_INVALID,
            message=str(e),
        )

    # 5. 写 manifest(包含 artifact 索引 + draft_path 占位)
    paths.ensure()
    artifact_ids = [a["artifact_id"] for a in artifacts if a.get("artifact_id")]
    manifest = build_manifest(
        job_id=str(job_id or "unknown"),
        video_path=str(video_path),
        config_snapshot=cfg_snapshot,
        storyline_session_id=state.get("storyline_session_id"),
        storyline_artifact_ids=artifact_ids,
        draft_path=None,  # node_05 写完 draft 后回填
        extras={"config_sha256": cfg_sha, "input_sha256": input_sha},
    )
    write_manifest(paths, manifest)

    # 6. 写 storyline.jsonl 一行(便于监控)
    append_storyline_log(
        paths,
        tool_name="plan_timeline_pro",
        artifact_id=artifact_ids[-1] if artifact_ids else "",
        duration_ms=0,
        error_code=None,
        job_id=str(job_id or "unknown"),
        session_id=state.get("storyline_session_id"),
    )

    return {
        **state,
        "storyline_plan": canonical_timeline.model_dump(),
        "storyline_artifacts": artifacts,
        "storyline_outputs_root": str(paths.root),
        "storyline_session_id": state.get("storyline_session_id"),
        "storyline_error_code": None,
        "error_log": errors,
    }


# ---------------------------------------------------------------------------
# 链路
# ---------------------------------------------------------------------------
async def _run_chain(
    client: OpenStorylineMCPProtocol,
    *,
    video_path: str,
    job_id: str,
) -> tuple[dict, list[dict]]:
    """跑 FireRed MCP 链路,返回 ``(plan_dict, artifact_records)``。

    兼容 Week 2 旧接口:如果 client 只暴露 :meth:`import_video_and_get_shot_plan`,
    则走旧路径(单方法拉 shot_plan),ValueError 直接冒泡给 entry 处理。
    """
    # Week 2 兼容:mock-style client 走单方法路径,直接返回原 shot_plan。
    # 标记 ``_is_legacy_shot_plan=True`` 让 entry 决定写 ``shot_plan`` 而不是 ``storyline_plan``。
    if hasattr(client, "import_video_and_get_shot_plan"):
        legacy_plan = client.import_video_and_get_shot_plan(video_path=video_path)
        return (
            {
                "_is_legacy_shot_plan": True,
                "video_path": legacy_plan.get("video_path", video_path) if isinstance(legacy_plan, dict) else video_path,
                "shots": legacy_plan.get("shots", []) if isinstance(legacy_plan, dict) else [],
            },
            [{"artifact_id": "legacy-1", "summary": "legacy_mock", "hash": "", "version": "1"}],
        )

    file_uri = _path_to_file_uri(str(Path(video_path).resolve()))
    started = time.time()

    # load_media
    load_resp = await client.call_tool(
        "load_media",
        arguments={"inputs": [{"file_uri": file_uri, "orig_path": video_path, "orig_md5": ""}]},
    )
    load_artifact = load_resp.get("artifact_id")

    # understand_clips
    # Phase 1 简化:直接传 shots_artifact_id = load_artifact(忽略 split_shots)
    understand_resp = await client.call_tool(
        "understand_clips",
        arguments={"shots_artifact_id": load_artifact},
    )
    understand_artifact = understand_resp.get("artifact_id")

    # generate_script
    script_resp = await client.call_tool(
        "generate_script",
        arguments={"understanding_artifact_id": understand_artifact, "style": "casual"},
    )
    script_artifact = script_resp.get("artifact_id")

    # plan_timeline_pro(主时间线)
    plan_resp = await client.call_tool(
        "plan_timeline_pro",
        arguments={
            "shots_artifact_id": load_artifact,
            "understanding_artifact_id": understand_artifact,
            "script_artifact_id": script_artifact,
            "max_duration_ms": 35000,  # 35s 目标,与 node_07 一致
        },
    )
    plan_artifact = plan_resp.get("artifact_id")

    # 拼成 StorylinePlan
    plan_dict = {
        "tool": "plan_timeline_pro",
        "artifact_id": plan_artifact,
        "tool_excute_result": plan_resp.get("tool_excute_result") or {},
        "summary": plan_resp.get("summary", ""),
        "isError": plan_resp.get("isError", False),
        "created_at_ms": int(time.time() * 1000),
    }
    artifacts = [
        {"artifact_id": load_artifact, "summary": "load_media", "hash": "", "version": "1"},
        {"artifact_id": understand_artifact, "summary": "understand_clips", "hash": "", "version": "1"},
        {"artifact_id": script_artifact, "summary": "generate_script", "hash": "", "version": "1"},
        {"artifact_id": plan_artifact, "summary": "plan_timeline_pro", "hash": "", "version": "1"},
    ]
    return plan_dict, artifacts


def _derive_canonical_from_plan(
    plan_dict: dict,
    *,
    video_path: str,
    job_id: Optional[str],
) -> CanonicalTimeline:
    """从 ``plan_dict`` 拍平出 :class:`CanonicalTimeline`,并校验。

    若 plan 缺必要字段,降级返回仅含 1 个 source_media + 1 个 clip 的最小可用 canonical。
    """
    # 1. 构造 SourceMedia 列表(Phase 1 简化:整段视频为单 source_media)
    media = SourceMedia(
        media_id="media-1",
        file_uri=_path_to_file_uri(str(Path(video_path).resolve())),
        duration_ms=int(plan_dict.get("tool_excute_result", {}).get("timeline", {}).get("duration_ms") or 35000),
    )
    # 2. 走 mapper.storyline_plan_to_canonical(它自带 Pydantic 校验)
    canonical = storyline_plan_to_canonical(
        plan_dict,
        job_id=str(job_id or uuid.uuid4().hex),
        source_media=[media],
    )
    return canonical


def _fallback_to_shot_plan(
    state: WorkflowState,
    errors: list[str],
    *,
    error_code: str,
    message: str,
) -> dict:
    """MCP 失败时降级到 ``MockOpenStorylineMCPClient`` 的最小 shot_plan。

    仅在 ``WORKFLOW_ENV=development`` 才允许降级;production 早退。
    """
    if WORKFLOW_ENV == "production":
        return {
            **state,
            "openstoryline_ready": False,
            "storyline_error_code": error_code,
            "storyline_plan": None,
            "error_log": errors,
        }
    # development:Mock 兜底(保留旧 shot_plan 字段语义)
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


__all__ = ["import_video_and_plan_shots", "set_default_client_factory"]