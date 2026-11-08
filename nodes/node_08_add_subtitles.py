"""节点 8:add_subtitles — 中文字幕原生落草稿的唯一写入口(2026-11-09 效果缺失修复)。

历史上本节点调用 ASR 客户端把语音转成逐句字幕写进草稿;B1 ASR 移除
(``docs/第三期开发/B1_ASR_移除设计与分阶段执行计划.md`` 阶段 3)后不再
识别语音。2026-11-09《剪映草稿效果缺失根因分析与修复设计执行计划》阶段 1
起,本节点承担**中文字幕原生落草稿**(此前的断点:mapper 私有形状 text 轨被
``storyline_to_native`` 整轨丢弃、node_08 只算样式不落草稿):

- 字幕文本 = ``storyline_script_artifact`` 的逐组旁白(``group_scripts[].narration``);
- 组 → 时间区间 = 当前草稿视频段反推(**源范围匹配**,抗关卡①人工调序;
  ``node_07`` 压缩后的时间变化天然被吸收 —— 这是写在 node_05 的初始草稿做不到的);
- 组内逐句:旁白按标点切句,按字符比例分摊组区间;
- 原生形状(materials.texts 条目 + text 轨道段)复用 ``nodes/_subtitle_texts.py``
  (与 node_08b 英文侧同一套已验证构造器);样式 = scout 换算 + storyline 推荐
  叠加(P4 代码自此真正上岗);
- 同步语义是**定向替换**:只替换/清理本节点自己的 id 前缀(``text-zh-``)与轨
  (``track-text-1``),人工在剪映加的其他文本素材/轨道原样保留(原则:自动节点
  不覆盖人工编辑);
- 无旁白 / 无 storyline_plan / 组映射失败 → 不写草稿,仅记 status_log(空输入
  跳过是常态路径,不是错误)。

保留的职责:
- 保持原图节点位置与中英两条分支的共同入口(拓扑不变);
- ``subtitle_scout`` 样式侦察与 ``_style_from_scout`` 换算(非 ASR 样式能力,
  ``_style_from_scout`` 仍被 ``node_08b_add_subtitles_en`` 复用);
- **不写** ``asr_segments_zh`` / ``asr_segments_zh_written``(计划 §3.3"置空或缺省"
  取缺省):新任务 state 天然无此键,节点 16a 走"无源字幕"正常跳过;旧线程
  checkpoint 里已有的段落经 ``{**state}`` 原样透传,按兼容语义继续可被 16a 消费。

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
import json
import re
from pathlib import Path
from typing import Any

import config
from assembly_capabilities.run_context import RunContext
from nodes._draft_io import apply_and_write, load_draft
from nodes._subtitle_texts import build_text_track, make_text_material
from state import WorkflowState
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

# ---------------------------------------------------------------------------
# P4(2026-11-08,相似功能节点计划 决定 2 选 A):storyline 推荐 → 剪映样式的换算
# ---------------------------------------------------------------------------
# storyline_recommend_text 的 size 是**设计基准像素**(缺省 24),与剪映 size 无
# 公开换算公式 —— 与 scout 同用"比值锚定法":建议值 / 基准 24 的无量纲比值缩放
# 剪映默认 5.0(复用 _scaled 夹取),基准不变时草稿一个数都不动。
_STORYLINE_SIZE_BASELINE_PX = 24.0

# storyline 的 position 是枚举字符串;剪映侧是 transform_y(当前默认 bottom=-0.8)。
# center/middle 归并为 0.0,top 取对称 +0.8。未知值一律保留原值。
_STORYLINE_POSITION_TO_Y: dict[str, float] = {
    "bottom": -0.8,
    "center": 0.0,
    "middle": 0.0,
    "top": 0.8,
}


# ---------------------------------------------------------------------------
# 阶段 1(2026-11-09,效果缺失修复计划):中文旁白字幕原生落草稿
# ---------------------------------------------------------------------------
#: 本节点拥有的 text material id 前缀(_sync_zh_text_materials 只清理它)
_ZH_ID_PREFIX = "text-zh"
#: 本节点拥有的 text 轨 id(_subtitle_texts.build_text_track 的固定 id)
_ZH_TRACK_ID = "track-text-1"
#: 本节点写入的私有轨名(仅日志/人读用;剪映原生轨 name 恒为 "")
_ZH_TRACK_NAME = "Subtitles_zh"

#: 句子边界:在 。!?;!?;\n 之后切(lookbehind,标点留在前句)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？；!?;\n])")


def _split_sentences(narration: str) -> list[str]:
    """组旁白 → 逐句(标点切分;切不出任何边界时整段一条)。"""
    text = str(narration or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_BOUNDARY_RE.split(text) if p and p.strip()]
    return parts or [text]


def _proportional_ranges(
    start_us: int, end_us: int, sentences: list[str]
) -> list[tuple[int, int]]:
    """把 [start_us, end_us] 按句子字符数比例切分;末句吃整数余量避免累计漂移。"""
    span = max(0, int(end_us) - int(start_us))
    weights = [max(1, len(s)) for s in sentences]
    total = sum(weights)
    out: list[tuple[int, int]] = []
    acc = int(start_us)
    for i, w in enumerate(weights):
        if i == len(weights) - 1:
            dur = int(end_us) - acc
        else:
            dur = int(span * w / total)
        out.append((acc, max(0, dur)))
        acc += dur
    return out


def _narration_by_group(state: WorkflowState) -> dict[str, str]:
    """``storyline_script_artifact``(script.json) → {group_id: narration}。"""
    raw_path = state.get("storyline_script_artifact")
    if not raw_path:
        return {}
    try:
        doc = json.loads(Path(str(raw_path)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for g in (doc or {}).get("group_scripts") or []:
        if isinstance(g, dict) and g.get("group_id"):
            out[str(g["group_id"])] = str(g.get("narration") or "")
    return out


def _voiceover_to_group(state: WorkflowState, plan: dict) -> dict[str, str]:
    """voiceover_id → group_id 桥。

    优先解析 plan ``audio.voiceover`` 的 JSON 映射串({group_id: voiceover_id},
    plan_timeline_pro 写入的联调字段);缺失时回落 voiceover.json 产物的逐段
    ``voiceover_id``/``group_id``。
    """
    bridge: dict[str, str] = {}
    audio = plan.get("audio") if isinstance(plan.get("audio"), dict) else {}
    raw = (audio or {}).get("voiceover")
    if isinstance(raw, str) and raw.strip():
        try:
            for gid, vid in json.loads(raw).items():
                bridge[str(vid)] = str(gid)
        except (ValueError, AttributeError):
            pass
    if bridge:
        return bridge
    raw_artifact = state.get("storyline_voiceover_artifact")
    if not raw_artifact:
        return {}
    try:
        doc = json.loads(Path(str(raw_artifact)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for entry in (doc or {}).get("voiceover") or []:
        if isinstance(entry, dict) and entry.get("voiceover_id") and entry.get("group_id"):
            bridge[str(entry["voiceover_id"])] = str(entry["group_id"])
    return bridge


def _norm_path_key(path: str) -> str:
    """路径归一化键:统一分隔符 + 小写(剪映保存可能改写 \\ 为 /)。"""
    return str(path or "").replace("\\", "/").strip().lower()


def _group_spans_from_draft(
    draft: dict, state: WorkflowState
) -> dict[str, tuple[int, int]] | None:
    """当前草稿视频段 → {voiceover_id: (start_us, end_us)} 组时间区间。

    匹配键 = (素材路径归一化, source 起点 µs, source 跨度 µs):关卡① 人工调序
    只改 target 位置不改源范围,所以按源范围匹配是抗调序的;plan 缺失/键对不上
    时退化为**顺序配对**(mapper 保序产出,段数一致即可靠);再失败返回 None
    (调用方跳过写字幕,不猜)。
    """
    from storyline.mapper import file_uri_to_path

    plan = state.get("storyline_plan")
    if not isinstance(plan, dict):
        return None
    clips = [c for c in (plan.get("clips") or []) if isinstance(c, dict)]
    if not clips:
        return None

    media = {
        str(m.get("media_id")): m
        for m in (plan.get("source_media") or [])
        if isinstance(m, dict) and m.get("media_id")
    }
    key_to_clip: dict[tuple[str, int, int], dict] = {}
    for c in clips:
        m = media.get(str(c.get("source_media_id") or ""))
        if not m:
            continue
        try:
            path = file_uri_to_path(str(m.get("file_uri") or ""))
        except ValueError:
            continue
        in_us = int(c.get("source_in_ms", 0) or 0) * 1000
        dur_us = (int(c.get("source_out_ms", 0) or 0) - int(c.get("source_in_ms", 0) or 0)) * 1000
        key_to_clip.setdefault((_norm_path_key(path), in_us, dur_us), c)

    mat_path_by_id: dict[str, str] = {}
    for v in (draft.get("materials") or {}).get("videos") or []:
        if isinstance(v, dict) and v.get("id") and isinstance(v.get("path"), str):
            mat_path_by_id[str(v["id"])] = v["path"]

    draft_segs: list[dict] = []
    for t in draft.get("tracks") or []:
        if not isinstance(t, dict) or t.get("type") != "video":
            continue
        for s in t.get("segments") or []:
            if not isinstance(s, dict):
                continue
            src = s.get("source_timerange") or {}
            tgt = s.get("target_timerange") or {}
            if not src or not tgt:
                continue
            draft_segs.append(
                {
                    "path": mat_path_by_id.get(str(s.get("material_id") or ""), ""),
                    "src_start": int(src.get("start") or 0),
                    "src_dur": int(src.get("duration") or 0),
                    "tgt_start": int(tgt.get("start") or 0),
                    "tgt_dur": int(tgt.get("duration") or 0),
                }
            )
    if not draft_segs:
        return None

    # 段 → clip:先源范围精确匹配,miss 的段再顺序配对(仅当段数与 clips 一致)
    matched: list[dict | None] = []
    misses: list[int] = []
    for i, seg in enumerate(draft_segs):
        clip = key_to_clip.get(
            (_norm_path_key(seg["path"]), seg["src_start"], seg["src_dur"])
        )
        matched.append(clip)
        if clip is None:
            misses.append(i)
    if misses and len(draft_segs) == len(clips):
        ordered = sorted(clips, key=lambda c: int(c.get("timeline_in_ms", 0) or 0))
        for i in misses:
            matched[i] = ordered[i]

    vo_group = _voiceover_to_group(state, plan)
    spans: dict[str, tuple[int, int]] = {}
    for seg, clip in zip(draft_segs, matched):
        if not clip:
            continue
        vid = str(clip.get("voiceover_id") or "")
        if not vid:
            continue
        gid = vo_group.get(vid, vid)
        start = seg["tgt_start"]
        end = start + seg["tgt_dur"]
        lo, hi = spans.get(gid, (start, end))
        spans[gid] = (min(lo, start), max(hi, end))
    return spans or None


def _build_zh_subtitles(
    draft: dict, state: WorkflowState, style: dict
) -> tuple[list[dict], list[dict], list[str]]:
    """构造中文字幕 materials + state 段落元数据;返回 (materials, segments, notes)。

    空输入(无旁白/无 plan/组映射失败)→ ([], [], notes),调用方不写草稿。
    """
    notes: list[str] = []
    spans = _group_spans_from_draft(draft, state)
    if spans is None:
        notes.append(
            "中文字幕跳过:无法把当前草稿视频段映射到分组(缺 storyline_plan 或段数不符)"
        )
        return [], [], notes
    narration = _narration_by_group(state)
    if not narration:
        notes.append("中文字幕跳过:script 无旁白(空 narration 是上游断裂的信号,见根因分析)")
        return [], [], notes

    materials: list[dict] = []
    segments: list[dict] = []
    index = 0
    for gid, (start_us, end_us) in sorted(spans.items(), key=lambda kv: kv[1][0]):
        text = (narration.get(gid) or "").strip()
        if not text:
            continue
        sentences = _split_sentences(text)
        for (s_us, d_us), sentence in zip(_proportional_ranges(start_us, end_us, sentences), sentences):
            if d_us <= 0 or not sentence:
                continue
            materials.append(
                make_text_material(
                    index=index,
                    content=sentence,
                    start_us=s_us,
                    duration_us=d_us,
                    style=style,
                    track_name=_ZH_TRACK_NAME,
                    id_prefix=_ZH_ID_PREFIX,
                    extra={"text_zh": sentence},
                )
            )
            segments.append(
                {
                    "index": index,
                    "start_ms": s_us // 1000,
                    "end_ms": (s_us + d_us) // 1000,
                    "text_zh": sentence,
                }
            )
            index += 1
    if not materials:
        notes.append("中文字幕跳过:全部分组旁白为空")
    return materials, segments, notes


def _sync_zh_text_materials(draft: dict, materials: list[dict]) -> None:
    """定向替换 materials.texts:只清理/替换本节点的 ``text-zh-`` 前缀条目,
    人工在剪映加的其他文本素材原样保留(自动节点不覆盖人工编辑)。"""
    mats = draft.setdefault("materials", {})
    existing = mats.get("texts") or []
    foreign = [
        t
        for t in existing
        if isinstance(t, dict) and not str(t.get("id") or "").startswith(_ZH_ID_PREFIX)
    ]
    mats["texts"] = foreign + list(materials)


def _sync_zh_text_track(draft: dict, materials: list[dict]) -> None:
    """定向替换本节点的 text 轨(id=track-text-1);其余 text 轨(人工)保留。"""
    tracks = draft.setdefault("tracks", []) or []
    kept = [
        t
        for t in tracks
        if not (
            isinstance(t, dict)
            and t.get("type") == "text"
            and str(t.get("id") or "") == _ZH_TRACK_ID
        )
    ]
    kept.append(build_text_track(materials))
    draft["tracks"] = kept


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


def _hex_to_rgb_floats(value: Any) -> list[float] | None:
    """'#RRGGBB' / '#RRGGBBAA' → [r, g, b] 0-1 浮点(round 4);非法返回 None。

    剪映 style 的 color 是 0-1 浮点三元组(``_JIANYING_DEFAULT_STYLE["color"]``),
    storyline 推荐的是 ``#RRGGBB`` 字符串,这里是两者唯一的换算点。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("#"):
        return None
    hex_part = text[1:]
    if len(hex_part) not in (6, 8):
        return None
    try:
        r = int(hex_part[0:2], 16)
        g = int(hex_part[2:4], 16)
        b = int(hex_part[4:6], 16)
    except ValueError:
        return None
    return [round(r / 255.0, 4), round(g / 255.0, 4), round(b / 255.0, 4)]


def _font_file_available(name: Any) -> bool:
    """推荐字体文件名(如 SourceHanSansCN-Bold.otf)在 ``config.ASSETS_FONTS_DIR`` 是否存在。

    recommend_text 的字体候选约定来自本地资源库(``assets/fonts``);该目录当前
    只有 README,所以"查不到"是本机的常态路径 —— 调用方据此回落并记日志。
    """
    if not isinstance(name, str) or not name.strip():
        return False
    try:
        return (Path(config.ASSETS_FONTS_DIR) / name.strip()).is_file()
    except (OSError, ValueError):
        return False


def _overlay_storyline_style(
    style: dict, text_style_plan: Any, *, lang: str = "zh"
) -> tuple[dict, list[str]]:
    """P4(决定 2 选 A):storyline 推荐(``storyline_text_style_plan``)叠加到基础样式。

    优先级(计划 §5.5):storyline 推荐 > 调用方已算好的基础样式(scout 换算结果 /
    默认)。「用户明确要求」层当前无数据源(``style_request`` 死键:无写者、
    recommend_text 也不读、console 无样式输入),不实现 —— 见执行记录偏差。

    只叠加语义明确、且有剪映渲染路径的字段(plan 的 ``default``,node_08/08b
    无 group 上下文,per-group items 无法映射):
    - size:    推荐值 / 基准 24px 的比值缩放剪映 size(复用 ``_scaled`` 夹取 0.6~2.0);
    - color:   ``#RRGGBB(AA)`` → [r,g,b] 浮点;非法/缺省保留原值;
    - position: bottom/center/middle/top → ``transform_y``;未知值保留原值;
    - font:    ``font_zh``(zh)/``font_en``(en)仅检查 ``config.ASSETS_FONTS_DIR``
      存在性并记 note —— 原生 content JSON(``build_text_content_json``)暂无字体
      渲染路径,不落草稿;查不到同样只记 note,样式回落不变。

    plan 缺失/非 dict/无 default → 原样返回([],无 note)。推荐 stub 默认
    (24 / bottom / #FFFFFF)与剪映默认逐项等值 → 恒等变换,「无推荐时行为不变」
    由构造成立。永不抛异常:非法输入一律原样返回。
    """
    notes: list[str] = []
    plan = text_style_plan if isinstance(text_style_plan, dict) else None
    default_rec = plan.get("default") if plan else None
    if not isinstance(default_rec, dict):
        return style, notes

    # --- size(比值锚定,复用 scout 的夹取)---
    try:
        rec_size = float(default_rec.get("size"))
    except (TypeError, ValueError):
        rec_size = 0.0
    if rec_size > 0 and isinstance(style.get("size"), (int, float)):
        ratio = rec_size / _STORYLINE_SIZE_BASELINE_PX
        before = style["size"]
        new_size = _scaled(float(before), ratio, "size")
        if new_size != before:
            style["size"] = new_size
            notes.append(
                f"字号 {before} → {new_size}(storyline 推荐 {rec_size:g}px / 基准 "
                f"{_STORYLINE_SIZE_BASELINE_PX:g}px)"
            )

    # --- color(hex → 0-1 浮点三元组)---
    rgb = _hex_to_rgb_floats(default_rec.get("color"))
    if rgb is not None and isinstance(style.get("color"), (list, tuple)):
        before_color = [round(float(c), 4) for c in style["color"]]
        if before_color != rgb:
            style["color"] = rgb
            notes.append(
                f"字幕颜色 {before_color} → {rgb}(storyline 推荐 {default_rec.get('color')})"
            )

    # --- position(枚举 → transform_y)---
    position = str(default_rec.get("position") or "").strip().lower()
    target_y = _STORYLINE_POSITION_TO_Y.get(position)
    if target_y is not None and isinstance(style.get("transform_y"), (int, float)):
        before_y = style["transform_y"]
        if float(before_y) != target_y:
            style["transform_y"] = target_y
            notes.append(
                f"字幕位置 {position}(transform_y {before_y} → {target_y})"
            )

    # --- font(仅存在性检查 + 日志;无渲染路径,不落草稿)---
    font_key = "font_en" if lang == "en" else "font_zh"
    font_name = default_rec.get(font_key)
    if isinstance(font_name, str) and font_name.strip():
        if _font_file_available(font_name):
            notes.append(
                f"推荐字体 {font_name} 本机资源库存在(仅记录:原生字幕暂无字体渲染路径,"
                "样式不落草稿)"
            )
        else:
            notes.append(
                f"推荐字体 {font_name} 本机资源库不存在,回落当前默认(仅记录,"
                "原生字幕暂无字体渲染路径)"
            )
    return style, notes


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
    """读取草稿(不动字幕)→ scout 样式侦察 → 原子写回。

    B1 ASR 移除后本节点不做语音转写、不注入字幕、**不写** ``asr_segments_zh`` /
    ``asr_segments_zh_written``:新任务 state 无此键 → 节点 16a 走"无源字幕"
    正常跳过;旧线程残留值经 ``{**state}`` 透传,按兼容语义继续可用。
    """
    raw_draft_path = state.get("draft_path")
    if not raw_draft_path:
        # node_05 早退时草稿不存在(D7,2026-10-20 修):降级跳过,别抛 TypeError
        state.setdefault("error_log", []).append(
            "[node_08] state.draft_path 缺失,跳过字幕注入"
        )
        status_log = list(state.get("status_log", []) or [])
        status_log.append("node_08_add_subtitles_skipped")
        return {**state, "status_log": status_log}
    draft_path = Path(raw_draft_path)
    # 优先读 snapshot2,缺失时退化到 draft_path
    snapshot2 = state.get("snapshot2_path")
    src = Path(snapshot2) if snapshot2 and Path(snapshot2).exists() else draft_path

    draft = load_draft(src)

    # 9 工具迁移 §6.4 step 1 / §7.1:scout 失败 → 沿用 _JIANYING_DEFAULT_STYLE。
    # 换算结果暂无草稿落点(ASR 字幕素材通道已移除),但 scout 报告与换算说明
    # 仍按原契约落到 state / status_log;``_style_from_scout`` 本身仍被 08b 复用。
    scout_report = _run_subtitle_scout(state.get("video_input_path", ""))
    _style, scout_notes = _style_from_scout(scout_report)
    # P4(决定 2 选 A):storyline 推荐(storyline_text_style_plan)叠加在 scout 换算
    # 结果之上 —— 字幕基础样式的单一决策点。B1 后 zh 侧换算结果暂无草稿落点,
    # 叠加说明按契约落 status_log;草稿仍不修改(回归锁 test_add_subtitles_
    # preserves_existing_script_subtitles)。zh 侧样式真实落草稿的通道待后续阶段。
    _style, plan_notes = _overlay_storyline_style(
        _style, state.get("storyline_text_style_plan"), lang="zh"
    )

    # 阶段 1(2026-11-09 效果缺失修复):中文旁白字幕原生落草稿 —— 本节点是
    # 中文字幕唯一写入口。时机在 node_07 时长适配之后(读 snapshot2):组→时间
    # 区间从当前草稿视频段按源范围反推,关卡①人工调序与压缩后的时间变化天然被
    # 吸收。空输入(无旁白/无 plan)→ 不写草稿,仅记 notes(常态跳过路径)。
    zh_materials, zh_segments, zh_notes = _build_zh_subtitles(draft, state, _style)
    if zh_materials:
        _sync_zh_text_materials(draft, zh_materials)
        _sync_zh_text_track(draft, zh_materials)

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
    if zh_materials:
        log.append(
            f"[node_08] 中文字幕已落草稿:{len(zh_materials)} 条(text 轨 track-text-1,"
            f"样式=scout+storyline 叠加)"
        )
    # 9 工具迁移 §6.4 step 1:成功调 scout → 把侦察报告落到 state;失败 → 不写
    if scout_report is not None:
        log.append("[node_08] subtitle_scout 成功,推荐已落到 state.subtitle_scout_report")
    else:
        log.append("[node_08] subtitle_scout 失败,沿用默认样式")
    for note in scout_notes:
        log.append(f"[node_08] scout 样式已生效:{note}")
    if scout_report is not None and not scout_notes:
        log.append("[node_08] scout 未提出需要改样式的建议(当前设置已合适)")
    for note in plan_notes:
        log.append(f"[node_08] storyline 推荐样式:{note}")
    for note in zh_notes:
        log.append(f"[node_08] {note}")
    out: dict = {
        **state,
        "status_log": log,
    }
    if scout_report is not None:
        out["subtitle_scout_report"] = scout_report
    if zh_segments:
        # 阶段 1:中文字幕段落(state 形状复用 SubtitleSegment),node_16a 英文
        # 翻译以此为源(替代已废弃的 asr_segments_zh,见阶段 2)。
        out["subtitle_segments_zh"] = zh_segments
    return out
