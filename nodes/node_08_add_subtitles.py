"""节点 8:add_subtitles — 字幕注入(对应原文档 4.3 节 + Week 4 计划 §3.1)。

基于 ``snapshot2_path``(节点 7 产出快照②)或 ``draft_path`` 读取草稿,
调用 ASR 客户端得到带时间戳字幕段,写入 ``materials.texts`` **与**
``state["asr_segments_zh"]``(Week 4 新增,供节点 16 翻译使用)。

字幕样式字段借鉴 ``E:\\Documents\\kuaishou\\.claude\\skills\\jianying-add-subtitles``
已实测惯例(字号 5.0 / 白色加粗 / 黑色描边 width 40 / transform_y=-0.8)。

9 工具迁移 §6.4 step 1 / §7.1:``subtitle_scout`` 的结论**真的落到草稿**。
scout 报告没有顶层 ``style`` 键(§7.1 片段里的 ``result.data["style"]`` 写错了),
两条建议分别挂在 ``contrast_recommendation.style`` 与
``font_recommendation.style``,且用的是 libass 世界(绝对像素 + ASS 颜色 +
``readability`` 宏),剪映 ``style`` 是另一套键名和单位——两边不能直接 merge。
对照表见 ``STYLE_KEY_MAP``,换算逻辑见 ``_style_from_scout``。
scout 失败 / 异常 / 判定"当前设置已合适" → 一律回落到 ``_JIANYING_DEFAULT_STYLE``,
不拖垮主链(§6.4 step 1 的降级原则)。
"""

from __future__ import annotations

import copy
from pathlib import Path

import config
from assembly_capabilities.run_context import RunContext
from jy_common.asr_client import call_asr2s
from nodes._draft_io import apply_and_write, load_draft
from state import SubtitleSegment, WorkflowState
from video_edit_capabilities import subtitle_style as sty
from video_edit_capabilities.subtitle_scout import subtitle_scout


# Week 4 硬编码的 JianYing 字幕样式,借鉴 jianying-add-subtitles 已实测惯例。
# 该样式是 9 工具迁移 §6.4 step 1 的"降级兜底默认",scout 调用失败时原样保留。
_JIANYING_DEFAULT_STYLE: dict = {
    "size": 5.0,
    "bold": True,
    "color": [1.0, 1.0, 1.0],
    "align": 1,
    "border": {"color": [0, 0, 0], "width": 40.0},
    "transform_y": -0.8,
}

# ---------------------------------------------------------------------------
# scout(libass 世界) → 剪映草稿 style(剪映世界)的字段对照表
# ---------------------------------------------------------------------------
# 只列**确实有对应关系、且有实测基准**的两个键。其余 scout 字段一律不落到草稿,
# 理由写在这里而不是留给下一个人猜:
#   color       : scout 从不改 primary_colour(三个 preset 都是纯白 &H00FFFFFF),
#                 映射过来是恒等空操作,写了反而多一条假路径。
#   align       : ASS ``alignment=2`` 是"底部居中",剪映 ``align=1`` 是"居中",
#                 语义不同,照搬会让字幕整体上移一行。
#   transform_y : scout 把 ``caption_band`` 当**诊断信息**报出(告诉人字幕带在哪),
#                 不给推荐值;动它等于替人做一次排版决定,不该由这一步默认承担。
#   shadow      : node_08 调好的默认样式里没有这个键(= 不描阴影,这是
#                 jianying-add-subtitles 实测出来的约定),本链路从没校准过它,
#                 往里写 ``shadow: false`` 只会给一个没基准的旋钮凭空赋值。
STYLE_KEY_MAP: dict[str, str] = {
    "size": "font_size",         # 字号:scout 判 too_small / too_large 时给绝对像素
    "border.width": "outline",   # 描边粗细:对比度建议最终落在这里
}

# 剪映 ``size`` / ``border.width`` 的单位与 ASS 像素之间没有公开换算公式,编一个
# 系数就是拿真实字幕的观感赌一个猜出来的数。改用比值锚定:默认值 5.0 / 40.0 是
# jianying-add-subtitles 在**真实剪映**上调出来的,对应的就是 scout 当前 preset 在
# 本片分辨率下的 baseline(``sty.resolve_style(preset)``)。拿"建议值 / baseline 值"
# 这个无量纲比值去缩放默认值,不需要知道剪映的内部单位——scout 判的是"大了还是
# 小了",比值刚好就是差多少倍。顺带保证 baseline 无变化时草稿一个像素都不动。
# 上下限防止 scout 一句建议把字幕推到不可用的极端。
_STYLE_SCALE_LIMITS: dict[str, tuple[float, float]] = {
    "size": (0.6, 2.0),
    "border.width": (0.5, 3.0),
}

# ``readability`` 是 ASS 的四字段宏(border_style / outline / shadow / 底色带),
# 剪映这边本链路只校准过"描边粗细"这一个对比度旋钮(见 STYLE_KEY_MAP):
#   heavy_outline → 直接按 resolve_style 算出的描边比例缩放
#   box           → ASS 那边是**一整条半透明底色带**;本仓库已实测的剪映 style
#                   字段里没有背景字段,只能降级成"描边加到最厚"。描边 ≠ 底色带,
#                   这是**不等价的降级**,所以 status_log 会明说,不装作两者一回事。
_BOX_BORDER_RATIO = 2.5


def _scout_style_overrides(scout_report: dict) -> dict:
    """从 scout 报告里抽出可直接喂给 ``sty.resolve_style`` 的 override 字典。

    两处 ``*.style`` 都是 drop-in override(见 ``subtitle_style.OVERRIDABLE``),
    直接合到一起即可;非 dict / 缺键一律跳过。
    """
    overrides: dict = {}
    for section in ("contrast_recommendation", "font_recommendation"):
        block = scout_report.get(section)
        if isinstance(block, dict) and isinstance(block.get("style"), dict):
            overrides.update(block["style"])
    return overrides


def _scaled(default_value: float, ratio: float, key: str) -> float:
    """按"相对 preset baseline 的倍数"缩放剪映默认值,并夹在 ``_STYLE_SCALE_LIMITS`` 内。"""
    lo, hi = _STYLE_SCALE_LIMITS[key]
    return round(default_value * min(hi, max(lo, ratio)), 2)


def _safe_ratio(rec_px: float, base_px: float) -> float:
    """建议值 / baseline 值。baseline 缺失或为 0 时返回 1.0(即"不用改")。"""
    return float(rec_px) / float(base_px) if base_px else 1.0


def _style_from_scout(scout_report: dict | None) -> tuple[dict, list[str]]:
    """scout 报告 → 剪映 ``texts[].style``。

    返回 ``(style, notes)``。``notes`` 是人可读的变更说明,拼进 ``status_log``,
    这样"scout 到底改了什么"在跑批日志里就能看清,不必去翻 draft。

    **任何**异常都吞掉并回落到 ``_JIANYING_DEFAULT_STYLE``。scout 是旁路增强,
    它自己的坑(字体找不到 / 报告结构变了 / 未知 preset)不应该拖垮主链。
    """
    default = copy.deepcopy(_JIANYING_DEFAULT_STYLE)
    if not config.SUBTITLE_SCOUT_STYLE_ENABLED:
        return default, []
    if not scout_report:
        return default, []

    video = scout_report.get("video")
    if not isinstance(video, dict):
        return default, []
    try:
        width = int(video["width"])
        height = int(video["height"])
    except (KeyError, TypeError, ValueError):
        return default, []
    if width <= 0 or height <= 0:
        return default, []

    overrides = _scout_style_overrides(scout_report)
    if not overrides:
        # contrast level=low 且 font level=ok 时,两处 style 都是空 dict——
        # scout 的原话就是"当前设置就合适",草稿不该有任何变化。
        return default, []

    try:
        baseline = sty.resolve_style(
            scout_report.get("preset"), None, video_width=width, video_height=height
        )
        recommended = sty.resolve_style(
            scout_report.get("preset"), overrides, video_width=width, video_height=height
        )
    except Exception:
        # 未知 preset / 未知 override 键 / 机器上没有可用 CJK 字体,都会走到这里。
        return default, []

    style = copy.deepcopy(_JIANYING_DEFAULT_STYLE)
    notes: list[str] = []

    font_rec = scout_report.get("font_recommendation") or {}
    if "font_size" in overrides:
        before = style["size"]
        ratio = _safe_ratio(recommended["font_size"], baseline["font_size"])
        style["size"] = _scaled(before, ratio, "size")
        notes.append(
            f"字号 {before} → {style['size']}(scout 判定 {font_rec.get('level', '?')},"
            f"建议 {recommended['font_size']}px / baseline {baseline['font_size']}px)"
        )

    contrast_rec = scout_report.get("contrast_recommendation") or {}
    if "readability" in overrides or "outline" in overrides:
        mode = str(overrides.get("readability") or "")
        if mode in ("box", "opaque_box"):
            ratio = _BOX_BORDER_RATIO
            notes.append(
                f"对比度 {contrast_rec.get('level', '?')} → 描边加粗至 {ratio} 倍"
                f"(降级:剪映 style 无背景色带字段,描边 ≠ ASS 的 box)"
            )
        else:
            ratio = _safe_ratio(recommended["outline"], baseline["outline"])
        before = style["border"]["width"]
        style["border"]["width"] = _scaled(before, ratio, "border.width")
        notes.append(
            f"描边宽度 {before} → {style['border']['width']}"
            f"(scout 判定 {contrast_rec.get('level', '?')},treatment {mode or 'outline'})"
        )

    if not notes:
        # override 里有键,但没有一个落在 STYLE_KEY_MAP 覆盖的字段上。
        return default, []
    return style, notes


def _run_subtitle_scout(video_path: str) -> dict | None:
    """调 ``subtitle_scout`` 拿字幕排版建议;失败 / 异常 → 返回 ``None``。

    对照 9 工具迁移 §6.4 step 1:失败回退原样式,不抛异常(主链不阻塞)。
    """
    if not video_path:
        return None
    try:
        ctx = RunContext()
        result = subtitle_scout({"video_path": video_path}, ctx=ctx)
    except Exception:
        # 任何异常(import error / scenedetect 未装 / ffmpeg 缺失等)→ 兜底
        return None
    if result.text.startswith("[ERROR]"):
        return None
    # data 是 dict;若结构异常也兜底
    data = result.data if isinstance(result.data, dict) else None
    return data


def add_subtitles(state: WorkflowState) -> dict:
    """读取草稿 → 调 ASR → 注入字幕 → 原子写回 + 写 ``asr_segments_zh`` 到 state。"""
    draft_path = Path(state["draft_path"])
    # 优先读 snapshot2,缺失时退化到 draft_path
    snapshot2 = state.get("snapshot2_path")
    src = Path(snapshot2) if snapshot2 and Path(snapshot2).exists() else draft_path

    draft = load_draft(src)
    asr_segments = call_asr2s(state.get("video_input_path", ""))

    materials = draft.setdefault("materials", {})
    texts = materials.setdefault("texts", [])

    # 9 工具迁移 §6.4 step 1 / §7.1:scout 失败 → 沿用 _JIANYING_DEFAULT_STYLE
    scout_report = _run_subtitle_scout(state.get("video_input_path", ""))
    subtitle_style, style_notes = _style_from_scout(scout_report)

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
            # 每条字幕一份独立副本:换算逻辑若日后就地修改也不会互相污染。
            "style": copy.deepcopy(subtitle_style),
            "track_name": "Subtitles",
        })
        # Week 4 §3.1:同步把毫秒版写进 state,供节点 16 使用
        asr_zh.append({
            "index": i,
            "start_ms": int(start_s * 1000),
            "end_ms": int(end_s * 1000),
            "text_zh": text_content,
        })

    # Week 5:参数从 draft_path 提升为 draft_path.parent,safe_write_draft 双写
    log = list(state.get("status_log", []) or [])
    log.extend(
        apply_and_write(
            draft_path,
            draft,
            "node_08",
            done_tag="node_08_add_subtitles_done",
        )
    )
    # 9 工具迁移 §6.4 step 1:成功调 scout → 把侦察报告落到 state;失败 → 不写
    if scout_report is not None:
        log.append("[node_08] subtitle_scout 成功,推荐已落到 state.subtitle_scout_report")
    else:
        log.append("[node_08] subtitle_scout 失败,沿用默认样式")
    for note in style_notes:
        log.append(f"[node_08] scout 样式已生效:{note}")
    if scout_report is not None and not style_notes:
        log.append("[node_08] scout 未提出需要改样式的建议(当前设置已合适)")
    out: dict = {
        **state,
        "asr_segments_zh": asr_zh,
        "asr_segments_zh_written": True,
        "status_log": log,
    }
    if scout_report is not None:
        out["subtitle_scout_report"] = scout_report
    return out
