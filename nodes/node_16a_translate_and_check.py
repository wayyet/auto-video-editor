"""node_16a_translate_and_check — 步骤 16a:翻译 + layout 检测(Week 5 新增)。

对齐第 5 周计划 §2.1 / §4.2:
- 只做"翻译 + 写 marker + 写 subtitle_segments_en 到 state + 检测 layout +
  写 SRT(正常路径)”。**不**含 ``interrupt()`` —— 这是设计关键:翻译 API
  调用独立成节点后,LangGraph 中间件重放时由 ``_marker_exists`` 守住,自然幂等
  (附件"关键发现①/③")。
- ``layout_issues`` / ``layout_issues_detected`` 写到 state,后续
  ``node_checkpoint3_layout_review`` 据此决定是否触发关卡③。
- SRT 写入:正常路径下(无 layout 异常)在 16a 内一次性写完,与 Week 4 原
  ``node_16_translate_subtitles`` 行为对齐;关卡③ 触发路径下 SRT 由
  ``node_checkpoint3_layout_review`` resume 后重写。

依赖:共享纯函数来自 ``node_16_translate_subtitles``(Week 5 拆分后保留)。
"""

from __future__ import annotations

from pathlib import Path

from jy_common.translate_client import translate_segments
from nodes.node_16_translate_subtitles import (
    _marker_exists,
    _read_segments_from_draft,
    _save_marker,
    _write_marker,
    validate_layout,
    write_srt_from_segments,
)
from state import WorkflowState


def node_16a_translate_and_check(state: WorkflowState) -> dict:
    """LangGraph 节点(Week 5 新增):翻译 + 写 marker + layout 校验。

    Args:
        state: 需含 ``asr_segments_zh`` / ``draft_dir_en_branch``。

    Returns:
        dict,只含变更字段 — 避免 fan-in 时与其他分支并发写同一字段:
        - ``subtitle_segments_en``: 英文字幕段
        - ``layout_issues``: layout 异常列表(可能为空)
        - ``layout_issues_detected``: bool,关卡③ 触发条件
        - ``status_log``: 仅 delta(由 reducer ``_append_unique`` 合并)
    """
    segments_zh = list(state.get("asr_segments_zh") or [])
    if not segments_zh:
        return {
            "error_log": ["[node_16a] asr_segments_zh 缺失,跳过翻译"],
            "status_log": ["node_16a_translate_skipped"],
        }

    draft_dir = state.get("draft_dir_en_branch")
    if not draft_dir:
        return {
            "error_log": ["[node_16a] draft_dir_en_branch 缺失,跳过翻译"],
            "status_log": ["node_16a_translate_skipped"],
        }

    draft_dir_p = Path(draft_dir)

    # ----- 翻译(已有 marker 则跳过)-----
    if _marker_exists(draft_dir_p, expected_count=len(segments_zh)):
        # 已有 marker,跳过翻译 API 调用,直接读草稿(可能含人工修正)
        segments_en = _read_segments_from_draft(draft_dir_p)
    else:
        translated = translate_segments(segments_zh)
        # 同步 start_ms / end_ms(translate_client 可能不返回)
        for i, (src, dst) in enumerate(zip(segments_zh, translated)):
            dst.setdefault("start_ms", src.get("start_ms", 0))
            dst.setdefault("end_ms", src.get("end_ms", 0))
            dst.setdefault("index", src.get("index", i))
        # 写入 draft(materials.texts[i].text_en)与 marker
        _write_marker(draft_dir_p, translated)
        _save_marker(draft_dir_p, translated)
        segments_en = translated

    # ----- layout 校验(只写 state,不 interrupt)-----
    issues = validate_layout(
        segments=segments_en,
        font_path=None,
        font_size=48,
        max_width_px=1500,
    )

    # ----- 正常路径下写 SRT(关卡③ 触发时由 c3 resume 后重写)-----
    srt_path = write_srt_from_segments(draft_dir_p, segments_en)

    return {
        "subtitle_segments_en": list(segments_en),
        "subtitle_srt_path": srt_path,
        "layout_issues": issues,
        "layout_issues_detected": bool(issues),
        "status_log": ["node_16a_translate_done"],
    }
