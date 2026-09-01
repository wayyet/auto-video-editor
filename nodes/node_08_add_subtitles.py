"""节点 8:add_subtitles — 字幕注入(对应原文档 4.3 节 + Week 4 计划 §3.1)。

基于 ``snapshot2_path``(节点 7 产出快照②)或 ``draft_path`` 读取草稿,
调用 ASR 客户端得到带时间戳字幕段,写入 ``materials.texts`` **与**
``state["asr_segments_zh"]``(Week 4 新增,供节点 16 翻译使用)。

字幕样式字段借鉴 ``E:\\Documents\\kuaishou\\.claude\\skills\\jianying-add-subtitles``
已实测惯例(字号 5.0 / 白色加粗 / 黑色描边 width 40 / transform_y=-0.8)。
"""

from __future__ import annotations

import json
from pathlib import Path

from draft_ops.atomic_writer import atomic_write_draft
from jy_common.asr_client import call_asr2s
from state import SubtitleSegment, WorkflowState


def _load_draft(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def add_subtitles(state: WorkflowState) -> dict:
    """读取草稿 → 调 ASR → 注入字幕 → 原子写回 + 写 ``asr_segments_zh`` 到 state。"""
    draft_path = Path(state["draft_path"])
    # 优先读 snapshot2,缺失时退化到 draft_path
    snapshot2 = state.get("snapshot2_path")
    src = Path(snapshot2) if snapshot2 and Path(snapshot2).exists() else draft_path

    draft = _load_draft(src)
    asr_segments = call_asr2s(state.get("video_input_path", ""))

    materials = draft.setdefault("materials", {})
    texts = materials.setdefault("texts", [])

    asr_zh: list[SubtitleSegment] = []

    for i, seg in enumerate(asr_segments):
        start_s = float(seg.get("start_s", 0.0))
        end_s = float(seg.get("end_s", start_s + 1.0))
        text_content = seg.get("text", "")
        texts.append({
            "id": f"text-{i + 1}",
            "content": text_content,
            "target_timerange": {
                "start": int(start_s * 1_000_000),
                "duration": int((end_s - start_s) * 1_000_000),
            },
            "style": {
                "size": 5.0,
                "bold": True,
                "color": [1.0, 1.0, 1.0],
                "align": 1,
                "border": {"color": [0, 0, 0], "width": 40.0},
                "transform_y": -0.8,
            },
            "track_name": "Subtitles",
        })
        # Week 4 §3.1:同步把毫秒版写进 state,供节点 16 使用
        asr_zh.append({
            "index": i,
            "start_ms": int(start_s * 1000),
            "end_ms": int(end_s * 1000),
            "text_zh": text_content,
        })

    atomic_write_draft(draft_path, draft)

    log = list(state.get("status_log", []) or []) + ["node_08_add_subtitles_done"]
    return {
        **state,
        "asr_segments_zh": asr_zh,
        "asr_segments_zh_written": True,
        "status_log": log,
    }