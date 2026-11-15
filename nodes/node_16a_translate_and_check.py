"""node_16a_translate_and_check — 步骤 16a:英文字幕翻译(第六期改版)。

历史:Week 5 曾在本节点内做"布局检测"(subtitle_build → subtitle_render →
subtitle_qc qc 链 + ``validate_layout`` 兜底),产出 ``layout_issues`` /
``layout_issues_detected`` / 预览视频 / 证据帧,供关卡③
(``node_checkpoint3_layout_review``)人工复核;2026-11-15 关卡③整体删除,
布局自动检测随之下线(能力层 ``subtitle_build/render/qc`` 保留,另有消费方;
产品行为变化:英文字幕超宽/重叠等问题不再自动检测,见执行记录)。
本节点回归单一职责:**翻译 + 写 marker + 写英文字幕到英文分支草稿 + 写 SRT**。

- 翻译源 = node_08 产出的中文旁白字幕段(``subtitle_segments_zh``,时间取自
  当前草稿);旧线程 checkpoint 残留的 ``asr_segments_zh`` 按兼容语义兜底
  (B1 移除后新流程不再产出)。
- ``_marker_exists`` 守住翻译 API 的重放幂等:resume 重放时不再重复调用
  (附件"关键发现①/③")。
- 无源字幕是英文分支的**常态输入**(B1 ASR 移除后),正常 skip 而非错误。

依赖:共享纯函数来自 ``node_16_translate_subtitles``。
"""

from __future__ import annotations

from pathlib import Path

from jy_common.translate_client import translate_segments
from nodes.node_16_translate_subtitles import (
    _marker_exists,
    _read_segments_from_draft,
    _save_marker,
    _write_marker,
    write_srt_from_segments,
)
from state import WorkflowState


def node_16a_translate_and_check(state: WorkflowState) -> dict:
    """LangGraph 节点:翻译英文字幕 + 写 marker + 写英文分支草稿 + 写 SRT。

    Args:
        state: 需含 ``subtitle_segments_zh``(兼容 ``asr_segments_zh``)/
            ``draft_dir_en_branch``。

    Returns:
        dict,只含变更字段 — 避免 fan-in 时与其他分支并发写同一字段:
        - ``subtitle_segments_en``: 英文字幕段
        - ``subtitle_srt_path``: 英文 SRT 路径
        - ``status_log``: 仅 delta(由 reducer ``_append_unique`` 合并)
    """
    # 阶段 2(2026-11-09 效果缺失修复):翻译源 = node_08 产出的中文旁白字幕段
    # (subtitle_segments_zh,时间取自当前草稿,英文字幕/配音随其同步);旧线程
    # checkpoint 残留的 asr_segments_zh 按兼容语义兜底(B1 移除后新流程不再产出)。
    segments_zh = list(
        state.get("subtitle_segments_zh") or state.get("asr_segments_zh") or []
    )
    if not segments_zh:
        # B1 ASR 移除(2026-11-08)后"无源字幕"是英文分支的**常态输入**:
        # 正常跳过而非错误(不写 error_log,不污染排障日志)。
        return {
            "status_log": ["skipped_no_source_subtitles"],
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

    # ----- 写 SRT -----
    srt_path = write_srt_from_segments(draft_dir_p, segments_en)

    return {
        "subtitle_segments_en": list(segments_en),
        "subtitle_srt_path": srt_path,
        "status_log": ["node_16a_translate_done"],
    }
