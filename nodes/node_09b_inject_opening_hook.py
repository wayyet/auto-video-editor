"""节点 9b:inject_opening_hook — 开场钩子(开头 0–3 秒免费开场特效 + 大字标题)。

第四期阶段 3(G2 / 原计划 WP7),方案 A(D4):开头 0–3 秒加免费开场特效 +
大字标题(带入场动画),**不切分镜头**;第 1 个转场仍在镜头 1→2 切点(node_09)。
中文草稿做特效 + 标题 + 入场动画,英文草稿只做特效(不做英文标题,见原计划 §7.12)。

设计要点(第四期计划 §3.2 对位表):

1. **写法与 node_09 同模式**:video_effect 素材经 ``extra_material_refs`` 绑到
   视频轨 ``segments[0]``,**不建 effect 轨**(原生转换器实证的引用通道)。
2. **标题轨自拼**(``track-text-hook``):不用 ``sync_text_track``/``build_text_track``
   —— 它们写死 ``track-text-1`` 且 node_08 的同步是定向替换(只动 ``text-zh-``
   前缀与 ``track-text-1``),不会误删本轨;但本节点排在 node_08 之后运行,
   标题必须在字幕之后写(图顺序天然满足:node_09 → 09b → node_11)。
3. **标题资源内置免费档**(``jy_common/hook_title.py``,来源 vendor metadata,
   is_vip=False):特效默认「CCD闪光」备选「闪白」;入场动画默认「冲屏位移」
   备选「放大」「渐显」。
4. **幂等**:我方产物用确定性 id(素材 ``hook-effect-`` / ``hook-title-`` /
   ``anim-hook-title-``,轨道 ``track-text-hook``);重放先清自己的旧产物再写。
   node_10 只处理 ``text-zh-``/``text-en-`` 前缀(不动标题),node_11 跳过
   ``hook-title-`` 前缀文字(标题不被贴贴纸),node_09 的幂等清理只认
   ``transition-``/``vfx-`` 模式 —— 三者与本节点的产物互不干扰。
5. **开关**:``config.OPENING_HOOK_ENABLED``(默认开,env
   ``AUTO_VIDEO_EDITOR_OPENING_HOOK_ENABLED``);关闭时只记
   ``…_noop:disabled``,不写草稿。
"""

from __future__ import annotations

from typing import Any

import config
from jy_common.hook_title import (
    HOOK_ANIMATION_CANDIDATES,
    HOOK_EFFECT_CANDIDATES,
    pick_hook_title,
)
from jy_common.native_fx import (
    build_native_text_animation_material,
    build_native_video_effect_material,
)
from jy_common.native_text import text_display_text
from nodes._draft_io import (
    apply_and_write,
    find_video_track,
    load_draft,
    upsert_by_id,
)
from nodes._dual_draft import ZH, dual_draft_targets, missing_en_note
from nodes._subtitle_texts import make_text_material, make_text_segment
from state import WorkflowState

#: 我方开场钩子产物的确定性 id / 轨道约定(幂等清理只动这些;人工素材 id 是
#: 剪映 uuid,不落这些前缀,不受影响)。
HOOK_TRACK_ID = "track-text-hook"
HOOK_TITLE_TRACK_NAME = "opening_title"
HOOK_EFFECT_PREFIX = "hook-effect-"
HOOK_TITLE_PREFIX = "hook-title-"
HOOK_ANIM_PREFIX = "anim-hook-title-"
_HOOK_REFS_PREFIXES = (HOOK_EFFECT_PREFIX, HOOK_TITLE_PREFIX, HOOK_ANIM_PREFIX)

#: 开场窗口上限(方案 A:前 3 秒,µs);标题/特效时长 = min(窗口, 视频总长)。
HOOK_WINDOW_US = 3_000_000

#: 开场大字标题样式(原生口径:字号 12.0 = 标题级,字幕默认 5.0;
#: transform_y 0.35 = 上半屏,字幕默认 −0.8 下沉)。
_HOOK_TITLE_STYLE = {"size": 12.0, "bold": True, "transform_y": 0.35}

#: node_08 我方字幕素材 id 前缀(取第 1 条字幕文本用)
_ZH_SUBTITLE_PREFIX = "text-zh-"


def _seg_duration_us(seg: dict) -> int:
    tr = seg.get("target_timerange") or {}
    return int(tr.get("duration", 0) or 0)


def _sorted_video_segments(draft: dict) -> list[dict]:
    """按 target_timerange.start 升序返回视频段(无计时段保持原顺序)。"""
    video_track = find_video_track(draft) or {}
    segments = list(video_track.get("segments") or [])
    segments.sort(key=lambda s: int((s.get("target_timerange") or {}).get("start", 0) or 0))
    return segments


def _first_subtitle_text(draft: dict) -> str | None:
    """中文草稿里第 1 条我方字幕(text-zh- 前缀)的显示文本;没有则 None。"""
    texts = [
        t
        for t in (draft.get("materials", {}).get("texts") or [])
        if isinstance(t, dict) and str(t.get("id") or "").startswith(_ZH_SUBTITLE_PREFIX)
    ]
    if not texts:
        return None
    texts.sort(key=lambda t: int((t.get("target_timerange") or {}).get("start", 0) or 0))
    return text_display_text(texts[0])


def _cleanup_stale_hook_products(draft: dict) -> int:
    """清掉本节点上一轮的全部产物(轨道/素材/refs),返回清理条数。

    先清后写(而不是保留续用):本节点每份草稿至多 1 特效 + 1 标题 + 1 动画,
    重放语义 = 按当前输入重建;title 变为 None 时上一轮的标题也会被清掉。
    """
    removed = 0
    materials = draft.setdefault("materials", {})

    tracks = draft.setdefault("tracks", []) or []
    kept_tracks: list[Any] = []
    for track in tracks:
        if isinstance(track, dict) and track.get("id") == HOOK_TRACK_ID:
            removed += len(track.get("segments") or [])
            continue
        kept_tracks.append(track)
    draft["tracks"] = kept_tracks

    for bucket_key, prefix in (
        ("texts", HOOK_TITLE_PREFIX),
        ("video_effects", HOOK_EFFECT_PREFIX),
        ("material_animations", HOOK_ANIM_PREFIX),
    ):
        bucket = materials.setdefault(bucket_key, [])
        kept: list[dict] = []
        for item in bucket:
            item_id = str(item.get("id") or "")
            if item_id.startswith(prefix):
                removed += 1
                continue
            kept.append(item)
        bucket[:] = kept

    # refs 剥离:特效 ref 在视频段,动画 ref 在标题段;遍历所有轨道统一剥
    for track in draft["tracks"]:
        if not isinstance(track, dict):
            continue
        for seg in track.get("segments", []) or []:
            refs = seg.get("extra_material_refs")
            if not isinstance(refs, list):
                continue
            kept_refs = [r for r in refs if not str(r).startswith(_HOOK_REFS_PREFIXES)]
            if len(kept_refs) != len(refs):
                removed += len(refs) - len(kept_refs)
                seg["extra_material_refs"] = kept_refs
    return removed


def _bind_ref(seg: dict, material_id: str) -> None:
    refs = seg.setdefault("extra_material_refs", [])
    if material_id not in refs:
        refs.append(material_id)


def _inject_hook_into(draft: dict, title: str | None, tag: str) -> tuple[list[str], dict]:
    """把开场特效(+ 标题/动画,仅中文)注入**一份**草稿(就地改 ``draft``)。

    Returns:
        (告警列表, 摘要 dict)。摘要只做日志/单测口径,不进 state(与
        fx_summary 移除后的"草稿原生条目 + status_log 为准"口径一致)。
    """
    warnings: list[str] = []
    summary: dict[str, Any] = {
        "effect_written": 0,
        "title_written": 0,
        "animation_written": 0,
        "no_video": False,
        "cleaned_stale": 0,
    }

    summary["cleaned_stale"] = _cleanup_stale_hook_products(draft)

    segments = _sorted_video_segments(draft)
    if not segments or _seg_duration_us(segments[0]) <= 0:
        summary["no_video"] = True
        warnings.append(f"[{tag}] 无可用视频段,开场钩子不写入(noop)")
        return warnings, summary

    materials = draft.setdefault("materials", {})

    # ---- 开场特效:hook-effect-1 绑视频轨 segments[0](不建 effect 轨)----
    # 特效跟随片段本身(原生 video_effect 无独立时长),3 秒窗口只约束标题时长
    effect_material = build_native_video_effect_material(
        material_id="hook-effect-1",
        meta=HOOK_EFFECT_CANDIDATES[0],
        value=1.0,
    )
    upsert_by_id(materials.setdefault("video_effects", []), effect_material)
    _bind_ref(segments[0], "hook-effect-1")
    summary["effect_written"] = 1

    # ---- 大字标题 + 入场动画(仅中文;title=None 时特效仍保留)----
    if title:
        title_dur_us = min(HOOK_WINDOW_US, _seg_duration_us(segments[0]))
        title_material = make_text_material(
            index=0,
            content=title,
            start_us=0,
            duration_us=title_dur_us,
            style=_HOOK_TITLE_STYLE,
            track_name=HOOK_TITLE_TRACK_NAME,
            id_prefix="hook-title",
        )
        upsert_by_id(materials.setdefault("texts", []), title_material)

        title_seg = make_text_segment(
            index=0,
            material_id="hook-title-1",
            start_us=0,
            duration_us=title_dur_us,
            style=_HOOK_TITLE_STYLE,
            id_prefix="hook-title-seg",
        )
        animation = build_native_text_animation_material(
            material_id="anim-hook-title-1",
            anim_meta=HOOK_ANIMATION_CANDIDATES[0],
            kind="in",
        )
        upsert_by_id(materials.setdefault("material_animations", []), animation)
        _bind_ref(title_seg, "anim-hook-title-1")

        # 自拼标题轨追加在 tracks 末尾(不用 sync_text_track/build_text_track,
        # 见模块 docstring 第 2 点)
        draft.setdefault("tracks", []).append(
            {
                "attribute": 0,
                "flag": 0,
                "id": HOOK_TRACK_ID,
                "is_default_name": True,
                "name": HOOK_TITLE_TRACK_NAME,
                "segments": [title_seg],
                "type": "text",
            }
        )
        summary["title_written"] = 1
        summary["animation_written"] = 1

    return warnings, summary


def inject_opening_hook(state: WorkflowState) -> dict:
    """读取草稿 → 清旧产物 → 注入开场特效/标题/动画 → 原子写回。

    K2=B:对**中文主草稿 + 英文分支草稿**各处理一次(见 ``nodes/_dual_draft.py``);
    中文做特效 + 标题 + 动画,英文只做特效(不做英文标题)。
    """
    status_log = list(state.get("status_log", []) or [])
    error_log = list(state.get("error_log", []) or [])

    if not config.OPENING_HOOK_ENABLED:
        status_log.append("node_09b_inject_opening_hook_noop:disabled")
        return {**state, "status_log": status_log, "error_log": error_log}

    targets = dual_draft_targets(state)
    if not targets:
        # node_05 早退时草稿不存在:降级跳过(与 node_09 同语义),别抛
        # FileNotFoundError 把整个 worker 带走。
        error_log.append("[node_09b] state.draft_path 缺失,跳过开场钩子(降级通过)")
        status_log.append("node_09b_inject_opening_hook_skipped")
        return {**state, "status_log": status_log, "error_log": error_log}

    for tag, draft_file in targets:
        draft = load_draft(draft_file)
        title: str | None = None
        if tag == ZH:
            title = pick_hook_title(state, _first_subtitle_text(draft))
            if title is None:
                status_log.append("node_09b_inject_opening_hook_noop:no_title")
        warnings, _summary = _inject_hook_into(draft, title, f"node_09b_{tag}")
        error_log.extend(warnings)
        # Week 5:参数从 draft_path 提升为 draft_path.parent,safe_write_draft 双写
        status_log.extend(
            apply_and_write(
                draft_file,
                draft,
                "node_09b",
                done_tag=(
                    "node_09b_inject_opening_hook_done"
                    if tag == ZH
                    else "node_09b_inject_opening_hook_en_done"
                ),
            )
        )

    if len(targets) == 1 and state.get("draft_dir_en_branch"):
        error_log.append(missing_en_note(state))

    return {**state, "status_log": status_log, "error_log": error_log}
