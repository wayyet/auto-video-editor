"""本地化 plan_timeline_pro(plan_v4 §5 阶段 5 / C 类)。

vendored ``PlanTimelineProNode`` 接收 ``groups / tts / bgm / targets`` +
``transition_plan`` + ``text_style_plan`` 等多个上游,产出 ``CanonicalTimeline``
JSON。本地化版本拆成 3 步:

1. ``TimeLine.distribute``:算每片时长(由 ``timeline_planner`` 简化版做)。
2. ``_attach_voiceover_meta``:把 voiceover 数组映射到每个 clip(用于剪映
   配音轨)。
3. ``_attach_bgm_meta``:把 bgm info 映射到 audio.bgm_ref。
4. ``_attach_transition_text_meta``:把转场 / 字幕样式按 group 应用。

设计纪律(plan §5 阶段 5):
- **不复用 vendored PlanTimelineProConfig**:简化版只用 ``target_duration_ms``
  和 ``min_clip_duration_ms`` 两个参数,其它 toml 配置在主项目不存在。
- **节拍对齐用 BGM beat_period_ms**,不用 vendored numpy 加速。
- **永远返回 dict**(plan §4.4),失败时只 append warning,不抛。

Pydantic 校验由下游 ``qa_gate`` 与 ``join_storyline`` 负责(已有逻辑)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from storyline_capabilities.timeline_planner import TimeLine

logger = logging.getLogger(__name__)


def plan_timeline_pro(
    *,
    groups: list[dict[str, Any]],
    voiceover: Optional[dict[str, Any]] = None,
    group_scripts: Optional[dict[str, Any]] = None,
    bgm_selection: Optional[dict[str, Any]] = None,
    transition_plan: Optional[dict[str, Any]] = None,
    text_style_plan: Optional[dict[str, Any]] = None,
    targets: Optional[dict[str, Any]] = None,
    source_media: Optional[list[dict[str, Any]]] = None,
    job_id: str = "unknown",
) -> dict[str, Any]:
    """组装 CanonicalTimeline dict。

    Args:
        groups: ``group_clips`` 输出的 ``groups`` 数组,每个 group 含 ``group_id``
            + ``clips`` 数组(每个 clip 含 ``clip_id`` / ``media_id`` /
            ``source_in_ms`` / ``source_out_ms``)。
        voiceover: ``generate_voiceover`` 输出 ``{"voiceover": [...]}`。
        bgm_selection: ``select_bgm`` 输出 ``storyline_bgm_selection`` dict。
        group_scripts: ``generate_script`` 输出 ``{"group_scripts": [...]}``;
            字幕**文本**来源(``narration``)。2026-10-19(P8 D9)新增:此前
            ``subtitles.zh`` 被错填成字幕**样式** dict,而契约要求 str。
        transition_plan: ``recommend_transition`` 输出;per-group 转场。
        text_style_plan: ``recommend_text`` 输出;per-group 字幕样式。
        targets: ``storyline_targets``(plan §3.2 字段);至少含
            ``target_duration_ms``。
        source_media: ``load_media`` 输出 ``media`` 数组(用于填
            ``source_media`` 字段);None 时合成最小一项占位。
        job_id: 用于 CanonicalTimeline.job_id 字段。

    Returns:
        形如 CanonicalTimeline 形状的 dict,字段:
        - ``schema_version``: "1.0"
        - ``job_id``, ``created_at_ms``
        - ``source_media``: list
        - ``clips``: 每片 ``{clip_id, source_media_id, source_in_ms,
            source_out_ms, timeline_in_ms, timeline_out_ms, voiceover_id?}``
        - ``audio``: ``{bgm_ref, voiceover}``
        - ``subtitles``: ``{zh, en}``
        - ``options``: ``{enable_ai_transition, enable_voiceover, max_duration_ms}``
        - ``method``: 时间线分配方法名
        - ``warnings``: list[str]
    """
    targets = targets or {}
    voiceover = voiceover or {}
    group_scripts = group_scripts or {}
    bgm_selection = bgm_selection or {}

    tts_res = list(voiceover.get("voiceover") or [])
    bgm = _bgm_to_internal(bgm_selection)
    transition_items = (
        list((transition_plan or {}).get("items") or [])
        if isinstance(transition_plan, dict)
        else []
    )
    text_items = (
        list((text_style_plan or {}).get("items") or [])
        if isinstance(text_style_plan, dict)
        else []
    )

    # 1. 分配时长
    timeline = TimeLine().distribute(
        groups=groups,
        tts_res=tts_res,
        bgm=bgm,
        targets=targets,
    )
    clips = timeline["clips"]
    voiceover_id_map = timeline.get("voiceover_id_map") or {}

    # 2. 给每片加 voiceover_id(便于剪映配音轨映射)
    if voiceover_id_map:
        group_to_clips: dict[str, list[dict]] = {}
        for i, g in enumerate(groups or []):
            group_to_clips[str(g.get("group_id") or f"group_{i + 1:04d}")] = list(
                g.get("clips") or []
            )
        # 反向:group_id → voiceover_id
        for clip_out, clip_in_meta in zip(clips, _iter_clip_with_group(groups)):
            gid = clip_in_meta["group_id"]
            cid = clip_in_meta["clip_id"]
            if gid in voiceover_id_map:
                clip_out["voiceover_id"] = voiceover_id_map[gid]

    # 3. 给每片加 transition / subtitle 样式(plan §2.2:转场 + 字幕按 group)
    transition_by_group = {it.get("group_id"): it for it in transition_items}
    text_by_group = {it.get("group_id"): it for it in text_items}
    # 2026-10-19(P8 D9):没有 per-group 样式时回落到 default 样式,
    # 免得 default 样式(此前被错塞进 subtitles.zh)整段丢失。
    default_text_style = (
        text_style_plan.get("default") if isinstance(text_style_plan, dict) else None
    )
    for clip_out, clip_in_meta in zip(clips, _iter_clip_with_group(groups)):
        gid = clip_in_meta["group_id"]
        if gid in transition_by_group:
            clip_out["transition_in"] = transition_by_group[gid].get("transition_in")
            clip_out["transition_out"] = transition_by_group[gid].get("transition_out")
        style = text_by_group.get(gid) or default_text_style
        if style:
            clip_out["subtitle_style"] = style

    # 4. 构造 source_media(若有 None 占位)
    if not source_media:
        source_media = [
            {
                "media_id": "media_0001",
                "file_uri": "file:///unknown",
                "duration_ms": max(1, timeline["total_ms"]),
                "media_type": "video",
            }
        ]
    # 2026-10-19(P8 D9):``load_media`` 产物是「运维形状」(path / metadata /
    # orig_path / orig_md5),契约要的是「抓取形状」(file_uri + 顶层 duration_ms)。
    # 直接透传 → extra="forbid" 报 4 个 extra_forbidden + 1 个 missing,
    # 草稿永远生成不出来。这里统一整形一次。
    source_media = normalize_source_media(source_media, clips=clips)

    # 5. 拼 CanonicalTimeline 形状(对齐 ``storyline.contract.CanonicalTimeline``)
    timeline_dict: dict[str, Any] = {
        "schema_version": "1.0",
        "job_id": str(job_id or "unknown"),
        "created_at_ms": 0,
        "source_media": list(source_media),
        "clips": clips,
        "audio": {
            "bgm_ref": bgm_selection.get("bgm_ref") if bgm_selection else None,
            # CanonicalTimeline.audio.voiceover 是 Optional[str];空时 None,有映射
            # 时存 JSON 字符串(group_id → voiceover_id),mapper 不读、联调可查。
            "voiceover": json.dumps(voiceover_id_map, ensure_ascii=False)
            if voiceover_id_map
            else None,
        },
        "subtitles": {
            # 2026-10-19(P8 D9):这里是**字幕文本**,不是样式 dict。
            # 文本来自 generate_script 的 group narration(按 group 顺序拼接)。
            "zh": _subtitle_text(groups, group_scripts),
            "en": None,
        },
        "options": {
            "enable_ai_transition": False,
            "enable_voiceover": bool(tts_res),
            "max_duration_ms": int(targets.get("target_duration_ms") or 0)
            or 35_000,
        },
        # 扩展字段:mapper 不读,联调时方便人查
        "method": timeline["method"],
        "warnings": timeline.get("warnings") or [],
    }

    return timeline_dict


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
_MEDIA_TYPES = {"video", "image", "audio", "unknown"}


def _path_to_file_uri(path: str) -> str:
    """本地绝对路径 → ``file:///...`` URI(Windows 走盘符形式)。"""
    p = str(path or "").replace("\\", "/")
    if not p:
        return "file:///unknown"
    if len(p) >= 2 and p[1] == ":":
        return f"file:///{p}"
    if p.startswith("/"):
        return f"file://{p}"
    return f"file:///{p}"


def normalize_source_media(
    source_media: list[dict[str, Any]],
    *,
    clips: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """把 ``load_media`` 产物整形成契约的 ``source_media`` 形状。

    ``load_media`` 输出(运维形状)::

        {"media_id", "path", "file_uri", "media_type",
         "metadata": {"duration_ms", "width", "height", "fps", ...},
         "orig_path", "orig_md5"}

    契约 ``SourceMedia``(抓取形状)只认::

        {"media_id", "file_uri", "duration_ms", "media_type",
         "sha256"?, "width"?, "height"?, "fps"?}

    两边字段集不同,直接透传会被 ``extra="forbid"`` 判死(2026-10-19 P8 D9)。
    这里做一次**单向整形**:字段取值只从原对象搬,不新增语义。
    """
    out: list[dict[str, Any]] = []
    for idx, raw in enumerate(source_media or []):
        if not isinstance(raw, dict):
            continue
        meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}

        media_id = str(raw.get("media_id") or f"media_{idx + 1:04d}")
        file_uri = str(raw.get("file_uri") or "")
        if not file_uri:
            file_uri = _path_to_file_uri(raw.get("path") or raw.get("orig_path") or "")

        media_type = str(raw.get("media_type") or "video")
        if media_type not in _MEDIA_TYPES:
            media_type = "unknown"

        item: dict[str, Any] = {
            "media_id": media_id,
            "file_uri": file_uri,
            "duration_ms": int(raw.get("duration_ms") or meta.get("duration_ms") or 0),
            "media_type": media_type,
        }
        for key in ("sha256", "width", "height", "fps"):
            value = raw.get(key)
            if value is None:
                value = meta.get(key)
            if value is not None:
                item[key] = value
        out.append(item)

    # 时长兜底:素材没报时长时,用引用它的 clip 的最大 source_out_ms 顶上。
    # 否则契约不变量 ``source_out_ms <= duration_ms`` 会因 duration=0 误判失败。
    max_out_by_media: dict[str, int] = {}
    for clip in clips or []:
        if not isinstance(clip, dict):
            continue
        mid = str(clip.get("source_media_id") or "")
        out_ms = int(clip.get("source_out_ms") or 0)
        if mid and out_ms > max_out_by_media.get(mid, 0):
            max_out_by_media[mid] = out_ms
    for item in out:
        if not item.get("duration_ms"):
            item["duration_ms"] = max_out_by_media.get(str(item["media_id"]), 0)
    return out


def _subtitle_text(
    groups: list[dict[str, Any]],
    group_scripts: dict[str, Any],
) -> Optional[str]:
    """拼中文字幕**文本**:按 group 顺序取 ``generate_script`` 的 narration。

    2026-10-19(P8 D9):此前这里填的是 ``recommend_text`` 的**样式** dict,
    与契约 ``TimelineSubtitles.zh: Optional[str]`` 不符 → 校验直接失败。
    样式改由 ``clips[*].subtitle_style`` 承载。
    """
    narration_by_group: dict[str, str] = {}
    for item in (group_scripts or {}).get("group_scripts") or []:
        if not isinstance(item, dict):
            continue
        gid = str(item.get("group_id") or "")
        narration = str(item.get("narration") or "").strip()
        if gid and narration:
            narration_by_group[gid] = narration

    lines: list[str] = []
    for idx, group in enumerate(groups or []):
        if not isinstance(group, dict):
            continue
        gid = str(group.get("group_id") or f"group_{idx + 1:04d}")
        narration = narration_by_group.get(gid)
        if narration:
            lines.append(narration)
    return "\n".join(lines) if lines else None


def _bgm_to_internal(bgm_selection: dict[str, Any]) -> Optional[dict[str, Any]]:
    """把 select_bgm 输出转成 timeline_planner 期望的形状。

    期望形状:``{"duration_ms", "beat_period_ms", "beats"}``
    """
    if not bgm_selection:
        return None
    if bgm_selection.get("bgm_ref") in (None, "", "bgm_none", "bgm_unknown"):
        return None
    duration_ms = int(bgm_selection.get("duration_ms") or 0)
    beats = list(bgm_selection.get("beats") or [])
    if not duration_ms or not beats:
        return None
    # beat_period_ms = 平均节拍间隔
    if len(beats) >= 2:
        diffs = [beats[i + 1] - beats[i] for i in range(len(beats) - 1)]
        beat_period_ms = int(round(sum(diffs) / len(diffs))) if diffs else 1000
    else:
        beat_period_ms = 1000
    return {
        "duration_ms": duration_ms,
        "beat_period_ms": max(beat_period_ms, 200),
        "beats": beats,
    }


def _iter_clip_with_group(groups: list[dict[str, Any]]):
    """惰性生成 ``(group_id, clip_id, clip)`` 元组,与 timeline.clips 顺序一致。"""
    for g in groups or []:
        gid = str(g.get("group_id") or "")
        for c in g.get("clips") or []:
            yield {
                "group_id": gid,
                "clip_id": str(c.get("clip_id") or ""),
                "clip": c,
            }


__all__ = ["plan_timeline_pro", "normalize_source_media"]