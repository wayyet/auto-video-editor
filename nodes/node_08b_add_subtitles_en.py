"""节点 08b:add_subtitles_en — 英文字幕构建计划发射(与 node_08 同类)。

2026-11-22 SDK 链转正(六段 legacy 链退役):本节点不再改写英文分支草稿,
读 ``state["subtitle_segments_en"]``(16a 产出)构造 material 形态后经
``subtitle_specs_from_materials`` 转为 DraftBuildSpec.subtitles(node_11 SDK
编译统一落轨)。

2026-11-17 P1(英文字幕真实翻译,修订计划 §5.4):**翻译状态门禁** ——
``subtitle_translation_status != "real"``(stub/failed/缺失)时不发射英文字幕:
Mock 文本(``[EN] `` 前缀)一旦进草稿就是用户可见的假英文字幕。缺失记
error_log 降级放行,由交付 QA 的 ``subtitles_en`` 类别(required)阻断。

设计要点:
- **只返回变更字段(delta)**:本节点与中文主线节点同一超步并行,返回整份 state 会让
  LastValue 通道在同一超步被写两次(InvalidUpdateError)。
- **幂等**:spec 字幕段整体替换,重放得到同一结果。
- 折行宽度取 spec canvas(SDK 编译稿画布与草稿一致;legacy 的草稿画布读取
  随原生写入退役)。
"""

from __future__ import annotations

from pathlib import Path

from nodes._subtitle_texts import make_text_material, subtitle_specs_from_materials
from nodes.node_08_add_subtitles import _emit_subtitle_spec, _overlay_storyline_style, _style_from_scout
from nodes.node_16a_translate_and_check import STATUS_REAL
from state import WorkflowState


def add_subtitles_en(state: WorkflowState) -> dict:
    """英文字幕构建计划 → 英文分支草稿 spec(仅限真实翻译)。"""
    draft_dir_raw = state.get("draft_dir_en_branch")
    segments = list(state.get("subtitle_segments_en") or [])

    if not draft_dir_raw:
        return {"error_log": ["[node_08b] draft_dir_en_branch 缺失,跳过英文字幕发射"]}

    if not segments:
        # B1 ASR 移除(2026-11-08)后 16a 无源字幕 → 正常 skip 是**常态输入**:
        # 只记 skip 状态,不算错误,不写 error_log,后续节点(node_10b 等)照常放行。
        return {"status_log": ["node_08b_skipped_no_en_segments"]}

    translation_status = str(state.get("subtitle_translation_status") or "")
    if translation_status != STATUS_REAL:
        # 修订计划 §5.4:stub/failed/缺失一律不得冒充正式英文字幕 —— Mock 文本
        # 落草稿即用户可见假英文;降级放行由 QA subtitles_en(required)阻断。
        # 注意:空段 skip 先于本门禁 —— 无旁白任务不该被刷 error。
        return {
            "status_log": ["node_08b_skipped_translation_not_real"],
            "error_log": [
                f"[node_08b] 英文字幕翻译状态非 real({translation_status or 'missing'}),"
                "不发射正式英文字幕(stub 不冒充成功)"
            ],
        }

    draft_file = Path(draft_dir_raw) / "draft_content.json"
    if not draft_file.exists():
        return {"error_log": [f"[node_08b] 英文分支草稿不存在,跳过: {draft_file}"]}

    # 基础样式 = 默认。注:node_08 把 scout 报告写到 state["subtitle_scout_report"]
    # (该键已在 state.py 声明);英文分支当前**有意**不消费它(是否复用中文
    # scout 样式属行为评审,另案)。如需复用:把 None 换成
    # state.get("subtitle_scout_report") 即可。
    # P4(决定 2 选 A):storyline 推荐(storyline_text_style_plan.default)叠加在
    # 此基础样式之上。推荐 stub 默认(24/bottom/#FFFFFF)为恒等变换,
    # 无推荐/推荐等于默认时行为不变。
    style, base_notes = _style_from_scout(None)
    style, style_notes = _overlay_storyline_style(
        style, state.get("storyline_text_style_plan"), lang="en"
    )

    # 2026-11-18 P2-03:折行宽度优先用目标画布(SDK 链:spec canvas = 编译稿画布)
    from nodes._draft_build_spec import load_build_spec

    spec = load_build_spec(Path(draft_dir_raw))
    canvas_width = int(spec.canvas.width) if spec else None

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
            canvas_width=canvas_width,
        ))

    # 2026-11-22 SDK 链转正:英文字幕构建计划发射(唯一通道;失败 fail-closed)
    log: list[str] = []
    _emit_subtitle_spec(
        Path(draft_dir_raw), texts, language="en", log=log, node_tag="node_08b"
    )
    log.append("node_08b_add_subtitles_en_done")
    # P4:样式决策说明(scout 基础 + storyline 叠加)落 status_log,跑批日志可查
    for note in (*base_notes, *style_notes):
        log.append(f"[node_08b] 字幕样式:{note}")
    return {"status_log": log}
