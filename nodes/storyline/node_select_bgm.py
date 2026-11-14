"""Phase 4 C 类本地化节点:storyline_select_bgm(plan_v4 §5 阶段 4)。

阶段 0 时是 ``_mcp_passthrough`` 壳子;阶段 4 起改为调本地化的
``storyline_capabilities.select_bgm.select_bgm`` — 无 MCP、无 vendored venv。

输入:
- ``storyline_groups_artifact`` (JSON 路径,含 ``groups`` 数组)
- ``storyline_script_artifact`` (JSON 路径,含 ``group_scripts`` 数组)
- ``storyline_targets.bgm_request`` (用户对 BGM 的要求,可空)
- ``storyline_voiceover_artifact`` (2026-11-13 边审计阶段1 任务B:聚合各段
  ``duration`` 为总时长传给 ``select_bgm``,作为配乐时长覆盖偏好的真实依据;
  artifact 缺失/解析失败按 0 容缺省,BGM 不因配音失败陪葬)

输出:
- ``storyline_bgm_selection`` (内联 dict,plan §3.2 字段约定)
- ``status_log`` append ``storyline_select_bgm_done``
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.select_bgm import select_bgm
from nodes.storyline._common import (
    _read_groups,
    append_status_tag,
    _resolve_outputs_root,
    extract_user_request,
)

logger = logging.getLogger(__name__)


# ``_read_groups`` 已收敛到 ``nodes/storyline/_common.py``(与其他 storyline 节点共用一份);
# 本文件只保留 ``_read_script`` —— 它读的是另一个 key / 另一个数组名
# (``storyline_script_artifact`` → ``group_scripts``),_common 里的两个 helper 不覆盖它。
def _read_script(state: WorkflowState) -> list[dict]:
    p = state.get("storyline_script_artifact")
    if not p:
        return []
    try:
        doc = json.loads(Path(str(p)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(doc, dict):
        return list(doc.get("group_scripts") or [])
    return []


def _read_voiceover_total_ms(state: WorkflowState) -> int:
    """聚合配音 artifact 的可用总时长(毫秒),作为 BGM 时长覆盖偏好的输入。

    2026-11-13 边审计阶段1 任务B:``[GS, GV]→SB`` 双源屏障的 GV 源此前只是
    "docstring 提及、代码未消费"的预留等待;本函数把它变成真实数据依赖 ——
    各段 ``duration`` 求和(跳过带 ``error`` 的失败段)传给 ``select_bgm``。
    容缺省纪律(与 2026-11-10 配音/字幕解耦同款):TTS 整体失败时
    ``storyline_generate_voiceover`` 不写 artifact、单段失败时该段
    ``duration=0``,本函数在任何缺失/异常路径都返回 0,BGM 行为退化为
    与任务B 之前完全一致。
    """
    p = state.get("storyline_voiceover_artifact")
    if not p:
        return 0
    try:
        doc = json.loads(Path(str(p)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(doc, dict):
        return 0
    total_ms = 0
    for item in doc.get("voiceover") or []:
        if not isinstance(item, dict) or item.get("error"):
            continue
        try:
            total_ms += max(0, int(item.get("duration") or 0))
        except (TypeError, ValueError):
            continue
    return total_ms


def storyline_select_bgm_node(state: WorkflowState) -> dict:
    groups = _read_groups(state)
    scripts = _read_script(state)
    user_request = extract_user_request(state, "bgm_request", "music_request", "user_request")

    narration_text = "\n".join(
        (g.get("narration") or "").strip() for g in scripts if g.get("narration")
    )
    voiceover_total_ms = _read_voiceover_total_ms(state)

    try:
        result = select_bgm(
            user_request=user_request,
            groups=groups,
            narration_text=narration_text,
            voiceover_total_ms=voiceover_total_ms,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"select_bgm capability exception: {e!r}")
        result = {
            "bgm_ref": "bgm_fallback",
            "bgm_path": "",
            "beats": [],
            "duration_ms": 35000,
            "tempo": "medium",
            "default_volume": 0.0,
            "reason": f"select_bgm exception: {e!r}",
            "method": "fallback",
            "stub": True,
        }

    # 20261114 阶段2:日志可读性 —— 云音乐命中时带曲名/music_id/选择来源;
    # stub(=素材不可用)才记 BGM_FALLBACK,默认策略选中有效素材走普通 status_log。
    music_desc = ""
    if result.get("music_id"):
        music_desc = (
            f":music={result.get('music_name') or result.get('music_id')}"
            f"({result.get('music_id')}):source={result.get('selection_source') or result.get('method', '?')}"
        )
    delta: dict = {
        "storyline_bgm_selection": result,
        "status_log": append_status_tag(
            state,
            f"storyline_select_bgm_done:bgm_ref={result.get('bgm_ref', '?')}{music_desc}",
        ),
    }

    # stub(素材不可用/空库)兜底时告知调试;不阻断下游
    if result.get("stub"):
        delta["error_log"] = [
            *list(state.get("error_log", []) or []),
            f"[storyline:select_bgm] BGM_FALLBACK: bgm_ref={result.get('bgm_ref', '?')} reason={result.get('reason', '')}",
        ]
    return delta