"""节点 8:add_subtitles — 字幕注入(对应原文档 4.3 节)。

基于 ``snapshot2_path``(节点 7 产出快照②)或 ``draft_path`` 读取草稿,
调用 ASR 客户端得到带时间戳字幕段,写入 ``materials.texts``。

字幕样式字段借鉴 ``E:\\Documents\\kuaishou\\.claude\\skills\\jianying-add-subtitles``
已实测惯例(字号 5.0 / 白色加粗 / 黑色描边 width 40 / transform_y=-0.8)。
"""

from __future__ import annotations

import json
from pathlib import Path

from draft_ops.atomic_writer import atomic_write_draft
from jy_common.asr_client import call_asr2s
from state import WorkflowState


def _load_draft(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def add_subtitles(state: WorkflowState) -> dict:
    """读取草稿 → 调 ASR → 注入字幕 → 原子写回。"""
    draft_path = Path(state["draft_path"])
    # 优先读 snapshot2,缺失时退化到 draft_path
    snapshot2 = state.get("snapshot2_path")
    src = Path(snapshot2) if snapshot2 and Path(snapshot2).exists() else draft_path

    draft = _load_draft(src)
    asr_segments = call_asr2s(state.get("video_input_path", ""))

    materials = draft.setdefault("materials", {})
    texts = materials.setdefault("texts", [])

    for i, seg in enumerate(asr_segments):
        start_s = float(seg.get("start_s", 0.0))
        end_s = float(seg.get("end_s", start_s + 1.0))
        texts.append({
            "id": f"text-{i + 1}",
            "content": seg.get("text", ""),
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

    atomic_write_draft(draft_path, draft)

    log = list(state.get("status_log", []) or []) + ["node_08_add_subtitles_done"]
    return {**state, "status_log": log}