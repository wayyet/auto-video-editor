"""Phase 4/5 C 类本地化节点:storyline_plan_timeline_pro(plan_v4 §5 阶段 5)。

阶段 0 时是 ``_mcp_passthrough`` 壳子;阶段 5 起改为调本地化的
``storyline_capabilities.plan_timeline_pro.plan_timeline_pro`` — 无 vendored
依赖,纯 Python 简化版 TimeLine。

输入:
- ``storyline_groups_artifact`` (JSON 路径,含 ``groups`` 数组)
- ``storyline_script_artifact`` (JSON 路径,含 ``group_scripts`` 数组)
- ``storyline_voiceover_artifact`` (JSON 路径,含 ``voiceover`` 数组)
- ``storyline_bgm_selection`` (内联 dict)
- ``storyline_transition_plan`` (内联 dict)
- ``storyline_text_style_plan`` (内联 dict)
- ``storyline_targets`` (目标时长 / 比例)
- ``storyline_media_artifact`` (JSON 路径,含 ``media`` 数组;用于填
  ``source_media`` 字段)

输出:
- ``storyline_timeline_plan`` (CanonicalTimeline JSON 路径)
- ``status_log`` append ``storyline_plan_timeline_pro_done``
"""
from __future__ import annotations

import json
import logging

from state import WorkflowState
from storyline.contract import StorylineErrorCode
from storyline_capabilities.plan_timeline_pro import (
    normalize_source_media,
    plan_timeline_pro,
)
from storyline_capabilities.timeline_transcript import fill_clip_transcripts
from nodes.storyline._common import (
    _read_groups,
    _read_json_artifact,
    _read_media,
    append_status_tag,
    _resolve_outputs_root,
)

logger = logging.getLogger(__name__)


def storyline_plan_timeline_pro_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    groups = _read_groups(state)
    source_media = _read_media(state)

    voiceover_doc = _read_json_artifact(state, "storyline_voiceover_artifact")
    script_doc = _read_json_artifact(state, "storyline_script_artifact")
    bgm_selection = state.get("storyline_bgm_selection") or {}
    transition_plan = state.get("storyline_transition_plan") or {}
    text_style_plan = state.get("storyline_text_style_plan") or {}
    targets = state.get("storyline_targets") or {}

    job_id = str(state.get("session_id") or "unknown")

    if not groups:
        # 没有 group → 写最小空 timeline 让 join_storyline 兜底;不阻断
        out_path = out_dir / "timeline_plan.json"
        out_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "job_id": job_id,
                    "created_at_ms": 0,
                    "source_media": normalize_source_media(
                        source_media
                        or [
                            {
                                "media_id": "media_0001",
                                "file_uri": "file:///unknown",
                                "duration_ms": 35000,
                                "media_type": "video",
                            }
                        ]
                    ),
                    "clips": [],
                    "audio": {"bgm_ref": None, "voiceover": None},
                    "subtitles": {"zh": None, "en": None},
                    "options": {
                        "enable_ai_transition": False,
                        "enable_voiceover": False,
                        "max_duration_ms": int(targets.get("target_duration_ms") or 35000),
                    },
                    "method": "empty",
                    "warnings": [],
                },
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
        return {
            "storyline_timeline_plan": str(out_path),
            "status_log": append_status_tag(
                state, "storyline_plan_timeline_pro_done_empty"
            ),
        }

    try:
        timeline = plan_timeline_pro(
            groups=groups,
            voiceover=voiceover_doc if isinstance(voiceover_doc, dict) else None,
            group_scripts=script_doc if isinstance(script_doc, dict) else None,
            bgm_selection=bgm_selection if isinstance(bgm_selection, dict) else None,
            transition_plan=transition_plan if isinstance(transition_plan, dict) else None,
            text_style_plan=text_style_plan if isinstance(text_style_plan, dict) else None,
            targets=targets if isinstance(targets, dict) else {},
            source_media=source_media,
            job_id=job_id,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:plan_timeline_pro] {StorylineErrorCode.TOOL_EXECUTION_FAILED}: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_plan_timeline_pro_failed"
            ),
        }

    # 2026-10:ASR / 口播粗剪产物回填 Clip.transcript(只回填文本,不改镜头与时长)。
    # 失败只记 warning,绝不影响时间线本身。
    # ⚠️ 这里故意**不**往 status_log 写"回填了 N 条"这类会变的标签:qa 重试路由
    #    (qa_gate.route_after_storyline_qa)靠 status_log 的**最后一条**判断,而重试
    #    回路里同名标签会被去重 —— 循环内多出一个新标签就会让重试提前结束。
    try:
        rough_doc = _read_json_artifact(state, "storyline_rough_cut_artifact")
        asr_doc = _read_json_artifact(state, "storyline_asr_artifact")
        if rough_doc or asr_doc:
            filled = fill_clip_transcripts(
                timeline, rough_cut=rough_doc, asr=asr_doc
            )
            if filled:
                logger.info("plan_timeline_pro: transcript 回填 %d 个 clip", filled)
    except Exception as e:  # noqa: BLE001
        logger.warning("plan_timeline_pro: transcript 回填失败(忽略): %r", e)

    out_path = out_dir / "timeline_plan.json"
    out_path.write_text(
        json.dumps(timeline, ensure_ascii=False, default=str), encoding="utf-8"
    )

    delta_log: list[str] = [
        "storyline_plan_timeline_pro_done",
        f"storyline_timeline_method={timeline.get('method', 'unknown')}",
    ]
    delta: dict = {
        "storyline_timeline_plan": str(out_path),
        "status_log": append_status_tag(state, *delta_log),
    }

    # warnings 写 status_log 便于联调识别;不阻断
    for w in timeline.get("warnings") or []:
        delta["status_log"] = append_status_tag(
            state, f"storyline_timeline_warning: {w}"
        )
    return delta