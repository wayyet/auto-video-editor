"""Phase 4 C 类本地化节点:storyline_generate_voiceover(plan_v4 §5 阶段 4)。

阶段 0 时是 ``_mcp_passthrough`` 壳子;阶段 4 起改为调本地化的
``storyline_capabilities.generate_voiceover.generate_voiceover`` — 无 MCP、
无 vendored venv 依赖。

输入:
- ``storyline_script_artifact`` (JSON 路径,含 ``group_scripts`` 数组)
- ``storyline_targets.voiceover_request`` (用户对音色的要求)

输出:
- ``storyline_voiceover_artifact`` (JSON 路径,供 plan_timeline_pro 读)
- ``status_log`` append ``storyline_generate_voiceover_done``
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from state import WorkflowState
from storyline.contract import StorylineErrorCode
from storyline_capabilities.generate_voiceover import generate_voiceover
from nodes.storyline._common import append_status_tag, _resolve_outputs_root

logger = logging.getLogger(__name__)


def _read_script_dict(state: WorkflowState) -> dict | None:
    p = state.get("storyline_script_artifact")
    if not p:
        return None
    try:
        return json.loads(Path(str(p)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _extract_user_request(state: WorkflowState) -> str:
    targets = state.get("storyline_targets") or {}
    if isinstance(targets, dict):
        for k in ("voiceover_request", "tts_request", "user_request"):
            v = targets.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def storyline_generate_voiceover_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    script_doc = _read_script_dict(state)
    group_scripts: list[dict] = []
    if isinstance(script_doc, dict):
        group_scripts = list(script_doc.get("group_scripts") or [])
    user_request = _extract_user_request(state)

    if not group_scripts:
        # 没有脚本:返回最小空 artifact 满足 join_storyline mapper 兼容;
        # 不写 error_log,因为 generate_script 失败时已记。
        out_path = out_dir / "voiceover.json"
        out_path.write_text(
            json.dumps(
                {"voiceover": [], "provider": "stub", "params": {}, "errors": []},
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
        return {
            "storyline_voiceover_artifact": str(out_path),
            "status_log": append_status_tag(
                state, "storyline_generate_voiceover_skipped"
            ),
        }

    try:
        result = generate_voiceover(
            group_scripts=group_scripts,
            output_dir=out_dir,
            user_request=user_request,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:generate_voiceover] {StorylineErrorCode.TOOL_EXECUTION_FAILED}: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_generate_voiceover_failed"
            ),
        }

    out_path = out_dir / "voiceover.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )

    delta_log: list[str] = ["storyline_generate_voiceover_done"]
    if result.get("stub_count", 0) > 0:
        delta_log.append(
            f"storyline_generate_voiceover_stub={result['stub_count']}"
        )

    delta: dict = {
        "storyline_voiceover_artifact": str(out_path),
        "status_log": append_status_tag(state, *delta_log),
    }

    # 单段失败时,append error_log 告知调试但不阻断下游
    if result.get("errors"):
        delta["error_log"] = [
            *list(state.get("error_log", []) or []),
            *[
                f"[storyline:generate_voiceover] {StorylineErrorCode.TOOL_EXECUTION_FAILED}: {e}"
                for e in result["errors"]
            ],
        ]
    return delta