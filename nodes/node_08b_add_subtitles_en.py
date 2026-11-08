"""节点 08b:add_subtitles_en — 把英文字幕写入英文分支草稿(与 node_08 同类)。

背景(2026-10 在 main@4c5b042 实测):英文分支草稿 fork 自快照②,而快照②早于
node_08,所以草稿里 ``materials.texts`` 为空;16a 的 ``_write_marker`` 只按 index 往
**已存在**的 texts 里补 ``text_en``,空表时什么都不写。结果英文草稿里没有任何字幕
文本素材,关卡③ resume 后 ``_read_segments_from_draft`` 读到 0 段、SRT 被重写成 0 字节。

本节点补上这一步:读 ``state["subtitle_segments_en"]``(16a 产出),把每段英文写成
一条 ``materials.texts`` 素材。

设计要点:
- **只返回变更字段(delta)**:本节点与中文主线节点同一超步并行,返回整份 state 会让
  LastValue 通道在同一超步被写两次(InvalidUpdateError)。
- **幂等**:整表替换 ``materials.texts``(不是 append),重放得到同一结果。
- ``content`` 与 ``text_en`` 都写英文:``content`` 供剪映显示与
  ``_read_segments_from_draft`` 还原;``text_en`` 与 16a 的 marker 约定一致。
"""

from __future__ import annotations

from pathlib import Path

from nodes._draft_io import apply_and_write, load_draft
from nodes._subtitle_texts import make_text_material, sync_text_track
from nodes.node_08_add_subtitles import _overlay_storyline_style, _style_from_scout
from state import WorkflowState


def add_subtitles_en(state: WorkflowState) -> dict:
    """英文字幕 → 英文分支草稿 ``materials.texts``。"""
    draft_dir_raw = state.get("draft_dir_en_branch")
    segments = list(state.get("subtitle_segments_en") or [])

    if not draft_dir_raw:
        return {"error_log": ["[node_08b] draft_dir_en_branch 缺失,跳过英文字幕写入"]}
    if not segments:
        # B1 ASR 移除(2026-11-08)后 16a 无源字幕 → 正常 skip 是**常态输入**:
        # 只记 skip 状态,不算错误,不写 error_log,后续节点(node_10b 等)照常放行。
        return {"status_log": ["node_08b_skipped_no_en_segments"]}

    draft_file = Path(draft_dir_raw) / "draft_content.json"
    if not draft_file.exists():
        return {"error_log": [f"[node_08b] 英文分支草稿不存在,跳过: {draft_file}"]}

    draft = load_draft(draft_file)
    # 基础样式 = 默认。注:node_08 把 scout 报告写到 state["subtitle_scout_report"],
    # 但该键没有在 state.py 里声明,图运行时会被丢弃,这里拿不到(已知限制)。
    # 如需与中文字幕同一份 scout 样式:先在 state.py 声明该键,再把 None 换成 state.get(...)。
    # P4(决定 2 选 A):storyline 推荐(storyline_text_style_plan.default)叠加在此
    # 基础样式之上 —— 这是推荐样式唯一真实落草稿的通道:字号/颜色/位置有剪映
    # 渲染路径会生效;字体仅检查 ASSETS_FONTS_DIR 存在性并记日志(原生 content
    # JSON 暂无字体渲染路径)。推荐 stub 默认(24/bottom/#FFFFFF)为恒等变换,
    # 无推荐/推荐等于默认时行为不变。
    style, base_notes = _style_from_scout(None)
    style, style_notes = _overlay_storyline_style(
        style, state.get("storyline_text_style_plan"), lang="en"
    )

    texts = []
    for i, seg in enumerate(segments):
        start_ms = int(seg.get("start_ms", 0))
        end_ms = int(seg.get("end_ms", start_ms + 1_000))
        text_en = str(seg.get("text_en") or "")
        texts.append(make_text_material(
            index=i,
            content=text_en,
            start_us=start_ms * 1_000,
            duration_us=(end_ms - start_ms) * 1_000,
            style=style,
            track_name="Subtitles_en",
            id_prefix="text-en",
            extra={"text_en": text_en},
        ))

    # 整表替换 = 幂等(重放不会叠出重复字幕)
    draft.setdefault("materials", {})["texts"] = texts
    # 2026-11-07 文本链原生升级:字幕铺上原生 text 轨道(整轨替换,幂等),
    # 关卡③ 人工才能在剪映时间轴里看到并修正英文字幕布局
    sync_text_track(draft, texts)

    log = apply_and_write(
        draft_file,
        draft,
        "node_08b",
        done_tag="node_08b_add_subtitles_en_done",
    )
    # P4:样式决策说明(scout 基础 + storyline 叠加)落 status_log,跑批日志可查
    for note in (*base_notes, *style_notes):
        log.append(f"[node_08b] 字幕样式:{note}")
    return {"status_log": log}
