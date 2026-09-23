"""``assembly_build_timeline`` 能力函数(plan §7.3 + §3.3)。

**这是 6 节点中唯一调用 LLM/VLM 的能力**。输入是前两步产物
``media.json`` / ``transcript.json`` / ``video_ingest.json``,让模型看截图
+ 读转写文字,按 ``video-edit-assembly`` 第 3-5 阶段规则(分组评分选段 →
定版式与结构 → 搭建 project 风格 timeline)做选段和排序判断,按
``schemas/timeline.schema.json`` 关键契约写出 ``timeline.json``。

**与 LLM 网关的契约**:
- 复用 ``storyline_capabilities.vlm_client`` 的 ``chat_json`` / Protocol 抽象,
  不绑死厂商。默认用 ``StubLLMClient``,阶段五起可注入真实 SK client
  (本仓库 LLM 网关的注册入口是 ``vlm_client.register_default_clients``,
  与本模块保持解耦)。
- LLM 返回结构化的"选段理由"JSON(``selected_segments[]`` /
  ``editorial_structure`` / ``task_assumption`` / ``selected_strategy``),
  本函数负责把它组装成合法的 timeline 树,并保证通过
  ``validate_timeline_data`` 的关键校验。

**失败兜底**:
- 若 LLM 返回空 / 解析失败 / 选段为空,自动回退到确定性算法
  (按 candidate_segments 顺序取前 N 段),保证下游 validate/render/qc
  链路总有合法 timeline 可跑,与 ADR-3 的"软降级不阻断"原则一致。
- 若上游连 ``candidate_segments`` 都没有,产出空 timeline + 警告,
  下游 validate 会自然 escalated,留给 ``route_after_assembly_qc``
  路由到 repair_loop。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from storyline_capabilities.vlm_client import (
    LLMClient,
    chat_json,
    get_default_client,
)
from storyline_capabilities.prompts import render_prompt


# ---------------------------------------------------------------------------
# 常量(plan §7.3 关键约束)
# ---------------------------------------------------------------------------
# 软上限:超过此数量的 candidate_segments 强制截断到前 N 段参与 LLM 选段,
# 避免 prompt 过长超出模型 token 上限。N 与下游 validate_timeline 一致。
MAX_LLM_CANDIDATES: int = 24
# 占位 fallback 单条最小时长(秒);candidate_segments 缺 start/end 时按此构造。
FALLBACK_MIN_DURATION: float = 1.0
# 默认 FPS / canvas,LLM 不指定时使用(plan §7.3:LLM 决定 9:16 / 16:9 / fps)。
DEFAULT_FPS: float = 30.0
DEFAULT_CANVAS_WIDTH: int = 1080
DEFAULT_CANVAS_HEIGHT: int = 1920


# ---------------------------------------------------------------------------
# 选段模型契约
# ---------------------------------------------------------------------------
# LLM 必须输出以下 JSON shape(在 prompt 里写明):
#
#   {
#     "selected_segments": [
#       {"index": <int>, "reason": <str>, "beat": <str>, "order": <int>},
#       ...
#     ],
#     "editorial_structure": <str>,
#     "task_assumption": <str>,
#     "selected_strategy": <str>,
#     "canvas": {"width": <int>, "height": <int>, "fps": <float>,
#                "platform": <str>, "aspect_ratio": <str>}
#   }
#
# 其中 ``beat`` 是结构性标签(hook / core / vibe / end / contrast /
# setup / climax / resolution 等),用于 report.md 渲染。
SELECTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "selected_segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "reason": {"type": "string"},
                    "beat": {"type": "string"},
                    "order": {"type": "integer"},
                },
            },
        },
        "editorial_structure": {"type": "string"},
        "task_assumption": {"type": "string"},
        "selected_strategy": {"type": "string"},
        "canvas": {
            "type": "object",
            "properties": {
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "fps": {"type": "number"},
                "platform": {"type": "string"},
                "aspect_ratio": {"type": "string"},
            },
        },
    },
}


# ---------------------------------------------------------------------------
# 输入解析 helpers
# ---------------------------------------------------------------------------
def _collect_candidate_segments(media_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 ``media.json`` 收集所有 candidate_segments,统一加 source + media_index。

    输入形态(每个 media_report):
      {"source": "...", "analysis": {"candidate_segments": [...]}, ...}

    输出每个 seg:
      {"index": <media 内 index>, "start": <float>, "end": <float>,
       "duration": <float>, "source": <seg.source | "scene_boundary">,
       "media_index": <int>, "media_source": <源文件路径>}

    兼容:无 candidate_segments 时,根据 ``analysis.duration_seconds`` + ``probe.duration_seconds``
    构造单段整段。
    """
    out: list[dict[str, Any]] = []
    for media_idx, rep in enumerate(media_reports or []):
        if not isinstance(rep, dict):
            continue
        source = str(rep.get("source") or "")
        analysis = rep.get("analysis") or {}
        if not isinstance(analysis, dict):
            analysis = {}
        candidates = list(analysis.get("candidate_segments") or [])
        # 兜底:无 candidate_segments → 整段占位
        if not candidates:
            try:
                dur = float(
                    (analysis.get("duration_seconds") or 0.0)
                    or ((rep.get("probe") or {}).get("duration_seconds") or 0.0)
                )
            except (TypeError, ValueError):
                dur = 0.0
            if dur > 0:
                candidates = [{
                    "index": 0,
                    "start": 0.0,
                    "end": float(dur),
                    "duration": float(dur),
                    "source": "placeholder_full",
                }]
        for seg in candidates:
            if not isinstance(seg, dict):
                continue
            try:
                start = float(seg.get("start", 0.0) or 0.0)
                end_raw = seg.get("end")
                duration_raw = seg.get("duration")
                if end_raw is not None:
                    end = float(end_raw)
                elif duration_raw is not None:
                    end = start + float(duration_raw)
                else:
                    end = start + FALLBACK_MIN_DURATION
                if end <= start:
                    end = start + FALLBACK_MIN_DURATION
            except (TypeError, ValueError):
                continue
            out.append({
                "index": int(seg.get("index", len(out))),
                "start": start,
                "end": end,
                "duration": end - start,
                "source": str(seg.get("source") or "scene_boundary"),
                "media_index": media_idx,
                "media_source": source,
            })
    return out


def _format_transcript_for_prompt(transcript_data: Any) -> str:
    """从 transcript.json 抽取全文文本供 LLM 读。无 transcript 时返回占位。"""
    if not isinstance(transcript_data, dict):
        return "(no transcript available)"
    # 兼容多种 transcript 结构
    segs = transcript_data.get("segments")
    if isinstance(segs, list) and segs:
        lines: list[str] = []
        for s in segs:
            if not isinstance(s, dict):
                continue
            text = str(s.get("text") or s.get("transcript") or "").strip()
            if not text:
                continue
            lines.append(text)
        return "\n".join(lines) if lines else "(transcript has no text)"
    text = str(transcript_data.get("text") or transcript_data.get("transcript_text") or "").strip()
    return text or "(no transcript text)"


def _format_sheet_paths(ingest_data: Any) -> list[str]:
    """从 video_ingest.json 拿 contact sheet 路径列表,供 LLM 看图。"""
    if not isinstance(ingest_data, dict):
        return []
    out: list[str] = []
    sheet_paths = ingest_data.get("sheet_paths") or []
    if isinstance(sheet_paths, list):
        out.extend(str(p) for p in sheet_paths if p)
    frame_paths = ingest_data.get("frame_paths") or []
    if isinstance(frame_paths, list):
        # 兜底:有些实现不写 sheet_paths 但写 frame_paths
        out.extend(str(p) for p in frame_paths if p)
    return out


# ---------------------------------------------------------------------------
# LLM 选段调用
# ---------------------------------------------------------------------------
def _build_user_prompt(
    *,
    candidates: list[dict[str, Any]],
    transcript_text: str,
    sheet_paths: list[str],
    lang: str,
) -> str:
    """组装 user prompt 文本(候选片段 + 转写 + 截图清单)。

    plan §7.3 + plan §第七节 7.3:模型看截图 + 读转写文字做选段判断。
    prompt 中明确写出"按 video-edit-assembly 第 3-5 阶段规则"与 JSON 输出契约。
    """
    candidate_lines = "\n".join(
        f"- [#{c['index']}] media={c.get('media_source', '?')} "
        f"start={c['start']:.3f}s end={c['end']:.3f}s dur={c['duration']:.3f}s "
        f"hint={c.get('source', 'scene_boundary')}"
        for c in candidates
    )
    sheet_lines = "\n".join(f"- {p}" for p in sheet_paths) or "(no contact sheet images)"
    return render_prompt(
        "assembly_build_timeline",
        "user",
        lang=lang,
        candidate_count=len(candidates),
        candidate_lines=candidate_lines,
        transcript_text=transcript_text,
        sheet_paths=sheet_lines,
    )


def _call_llm_for_selection(
    *,
    candidates: list[dict[str, Any]],
    transcript_text: str,
    sheet_paths: list[str],
    lang: str,
    client: Optional[LLMClient],
) -> dict[str, Any]:
    """调 LLM 拿选段 JSON。失败返回 ``{}``,调用方走 fallback。"""
    sys_p = render_prompt("assembly_build_timeline", "system", lang=lang)
    user_p = _build_user_prompt(
        candidates=candidates,
        transcript_text=transcript_text,
        sheet_paths=sheet_paths,
        lang=lang,
    )
    # media 参数:把 contact sheet 路径喂给 VLM。
    media_payload: Optional[list[dict[str, Any]]] = (
        [{"path": p} for p in sheet_paths[:8]] if sheet_paths else None
    )
    try:
        result = chat_json(
            system_prompt=sys_p,
            user_prompt=user_p,
            media=media_payload,
            schema=SELECTION_JSON_SCHEMA,
            client=client,
        )
    except Exception:  # noqa: BLE001
        return {}
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# 选段还原 + 组装 timeline
# ---------------------------------------------------------------------------
def _normalize_selection(
    raw: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """把 LLM 输出还原成"已选 + 已排序"的候选片段。

    Returns:
        (chosen, extras, llm_succeeded):
        - chosen: 还原后的候选片段 list(已按 order 排序)
        - extras: editorial_structure / canvas / selected_strategy 等元数据
        - llm_succeeded: True 表示 LLM 给出过合法 selected_segments,
          False 表示 LLM 输出空/不合法,主函数应走 fallback。

    关键纪律:
    1. ``selected_segments[].index`` 必须能在 ``candidates`` 里找到,
       找不到的忽略,避免 LLM 幻觉导致 ``validate_timeline`` 报 missing source。
    2. ``order`` 缺省按列表顺序。
    3. ``editorial_structure`` / ``task_assumption`` / ``selected_strategy`` / ``canvas``
       缺失则用占位默认值。
    """
    if not candidates:
        return [], {}, False
    raw_segments = raw.get("selected_segments") or []
    by_index: dict[int, dict[str, Any]] = {int(c["index"]): c for c in candidates}
    chosen: list[dict[str, Any]] = []
    used_indices: set[int] = set()
    for raw_seg in raw_segments:
        if not isinstance(raw_seg, dict):
            continue
        try:
            idx = int(raw_seg.get("index"))
        except (TypeError, ValueError):
            continue
        if idx not in by_index or idx in used_indices:
            continue
        base = dict(by_index[idx])
        base["reason"] = (
            str(raw_seg.get("reason") or "").strip()
            or f"LLM-selected segment #{idx}"
        )
        try:
            base["order"] = int(raw_seg.get("order", len(chosen)))
        except (TypeError, ValueError):
            base["order"] = len(chosen)
        beat = str(raw_seg.get("beat") or "").strip()
        if beat:
            base["beat"] = beat
        chosen.append(base)
        used_indices.add(idx)
    # 按 order 排序,缺 order 的保持原顺序
    chosen.sort(key=lambda x: int(x.get("order", 0)))
    extras = {
        "editorial_structure": str(raw.get("editorial_structure") or ""),
        "task_assumption": str(raw.get("task_assumption") or ""),
        "selected_strategy": str(raw.get("selected_strategy") or ""),
        "canvas": raw.get("canvas") if isinstance(raw.get("canvas"), dict) else {},
    }
    # llm_succeeded=True 仅当 LLM 返回了至少一条 selected_segments
    # (允许 extras 部分缺失,但 selected_segments 必须非空)
    return chosen, extras, bool(chosen)


def _fallback_chosen(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LLM 失败时的占位选段算法:按场景边界顺序取前 N 段(阶段二原行为)。"""
    out: list[dict[str, Any]] = []
    for c in candidates:
        if c.get("source") == "placeholder_full":
            # 整段占位优先保留
            out.append(dict(c, reason=f"placeholder full-segment fallback (start={c['start']:.3f}s)", order=len(out)))
        elif len(out) < 6:
            out.append(dict(c, reason=f"placeholder scene-boundary fallback (start={c['start']:.3f}s, dur={c['duration']:.3f}s)", order=len(out)))
    if not out and candidates:
        # 实在没场景边界就硬取第一个
        c = candidates[0]
        out.append(dict(c, reason="placeholder forced first-segment fallback", order=0))
    return out


def _build_clips(
    chosen: list[dict[str, Any]],
    *,
    media_index_to_asset_id: dict[int, str],
) -> list[dict[str, Any]]:
    """把 chosen 转成 timeline.json 的 video clip 列表。

    ``candidate_index`` 字段保留原始 candidate_segment 的 index,
    便于 report.md / 选段评测脚本做"选段 vs 理想答案"的精准对比。
    """
    clips: list[dict[str, Any]] = []
    cursor = 0.0
    for i, seg in enumerate(chosen):
        start = float(seg["start"])
        end = float(seg["end"])
        duration = end - start
        asset_id = media_index_to_asset_id.get(
            int(seg.get("media_index", 0)), "a1"
        )
        clip = {
            "track_type": "video",
            "id": f"clip_{i:04d}",
            "asset_id": asset_id,
            "source": str(seg.get("media_source") or ""),
            "start": start,
            "end": end,
            "duration": duration,
            "timeline_start": cursor,
            "timeline_end": cursor + duration,
            "speed": 1.0,
            "volume": 1.0,
            "opacity": 1.0,
            "enabled": True,
            "reason": str(seg.get("reason") or f"segment #{i}"),
            "candidate_index": int(seg.get("index", -1)),
        }
        beat = str(seg.get("beat") or "").strip()
        if beat:
            clip["beat"] = beat
        clips.append(clip)
        cursor += duration
    return clips


def _build_assets(
    media_reports: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """生成 timeline.assets[] + media_index → asset_id 映射。"""
    assets: list[dict[str, Any]] = []
    index_to_id: dict[int, str] = {}
    for i, rep in enumerate(media_reports or []):
        if not isinstance(rep, dict):
            continue
        path = str(rep.get("source") or "")
        analysis = rep.get("analysis") or {}
        duration = 0.0
        if isinstance(analysis, dict):
            try:
                duration = float(analysis.get("duration_seconds") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
        asset_id = f"a{i + 1}"
        index_to_id[i] = asset_id
        assets.append({
            "id": asset_id,
            "path": path,
            "source": path,
            "type": "video",
            "duration": duration,
            "width": 16,
            "height": 16,
            "fps": DEFAULT_FPS,
        })
    if not assets:
        # 兜底:media_reports 为空时构造一个 placeholder asset
        assets.append({
            "id": "a1",
            "path": "",
            "source": "",
            "type": "video",
            "duration": 0.0,
            "width": DEFAULT_CANVAS_WIDTH,
            "height": DEFAULT_CANVAS_HEIGHT,
            "fps": DEFAULT_FPS,
        })
        index_to_id[0] = "a1"
    return assets, index_to_id


def _resolve_canvas(extras: dict[str, Any]) -> dict[str, Any]:
    """从 LLM 输出或默认值挑 canvas。"""
    raw = extras.get("canvas") or {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        width = int(raw.get("width") or DEFAULT_CANVAS_WIDTH)
    except (TypeError, ValueError):
        width = DEFAULT_CANVAS_WIDTH
    try:
        height = int(raw.get("height") or DEFAULT_CANVAS_HEIGHT)
    except (TypeError, ValueError):
        height = DEFAULT_CANVAS_HEIGHT
    try:
        fps = float(raw.get("fps") or DEFAULT_FPS)
    except (TypeError, ValueError):
        fps = DEFAULT_FPS
    canvas = {
        "width": max(width, 2),
        "height": max(height, 2),
        "fps": fps,
        "platform": str(raw.get("platform") or ""),
        "aspect_ratio": str(raw.get("aspect_ratio") or ""),
    }
    return canvas


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def build_timeline_from_media(
    *,
    media_reports: list[dict[str, Any]],
    transcript_data: Any = None,
    ingest_data: Any = None,
    lang: str = "zh",
    client: Optional[LLMClient] = None,
    fallback_to_placeholder: bool = True,
) -> dict[str, Any]:
    """阶段三主入口:从 media + transcript + ingest 选段 + 组装 timeline。

    Args:
        media_reports: ``assembly_discover_and_probe`` 写出的 ``media.json``。
        transcript_data: ``assembly_asr_and_visual_observe`` 写出的 ``transcript.json``
            (允许 None,表示无转写)。
        ingest_data: ``assembly_asr_and_visual_observe`` 写出的 ``video_ingest.json``
            (允许 None,表示无截图)。
        lang: prompt 语言(目前只接 ``"zh"``)。
        client: LLM client。None 时用 ``vlm_client.get_default_client()``(默认 Stub)。
        fallback_to_placeholder: LLM 选段为空时是否回退到确定性占位算法。

    Returns:
        ``timeline.json`` 的完整 dict,符合 ``schemas/timeline.schema.json`` 关键契约。
        关键字段:``project`` / ``assets[]`` / ``sequence`` / ``tracks[].clips[]``,
        每个 clip 必有 ``source`` / ``start`` / ``end`` / ``reason``。
    """
    started = time.perf_counter()
    if client is None:
        client = get_default_client()

    candidates = _collect_candidate_segments(media_reports)
    # 截断到 LLM 上限,避免 prompt 过长
    truncated = False
    if len(candidates) > MAX_LLM_CANDIDATES:
        candidates = candidates[:MAX_LLM_CANDIDATES]
        truncated = True
    transcript_text = _format_transcript_for_prompt(transcript_data)
    sheet_paths = _format_sheet_paths(ingest_data)

    llm_used = False
    fallback_used = False
    raw_selection: dict[str, Any] = {}
    chosen: list[dict[str, Any]] = []
    extras: dict[str, Any] = {}
    if candidates:
        raw_selection = _call_llm_for_selection(
            candidates=candidates,
            transcript_text=transcript_text,
            sheet_paths=sheet_paths,
            lang=lang,
            client=client,
        )
        # llm_used = 调用 LLM 成功且拿到非空 raw_selection(有 selected_segments 字段)
        llm_used = bool(raw_selection) and isinstance(raw_selection.get("selected_segments"), list)
        chosen, extras, llm_succeeded = _normalize_selection(raw_selection, candidates)
        if not chosen and fallback_to_placeholder:
            chosen = _fallback_chosen(candidates)
            fallback_used = True
            # 若 LLM 主动说"选段为空"(返回了空数组),仍记 llm_used=True(它给了选段判断)
            # 但 chosen 是 fallback 产物,extras 用原始 LLM 输出(ex:它可能写了 selected_strategy)
            if llm_succeeded and isinstance(raw_selection, dict):
                extras = {
                    "editorial_structure": str(raw_selection.get("editorial_structure") or ""),
                    "task_assumption": str(raw_selection.get("task_assumption") or ""),
                    "selected_strategy": str(raw_selection.get("selected_strategy") or "") or "(LLM returned no segments; used fallback)",
                    "canvas": raw_selection.get("canvas") if isinstance(raw_selection.get("canvas"), dict) else {},
                }

    assets, index_to_id = _build_assets(media_reports)
    clips = _build_clips(chosen, media_index_to_asset_id=index_to_id)
    canvas = _resolve_canvas(extras)
    total_duration = sum((c["end"] - c["start"]) for c in clips)

    project_meta: dict[str, Any] = {
        "name": "assembly",
        "task_assumption": extras.get("task_assumption") or "",
        "selected_strategy": extras.get("selected_strategy") or "",
        "editorial_structure": extras.get("editorial_structure") or "",
        "selected_segments_count": len(chosen),
        "candidate_segments_count": len(candidates),
        "truncated_for_llm": truncated,
        "llm_used": llm_used,
        "fallback_used": fallback_used,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }

    timeline: dict[str, Any] = {
        "version": "assembly-build-timeline-1",
        "task": "multi-material assembly (auto-video-editor integration)",
        "project": project_meta,
        "sequence": {
            "fps": canvas["fps"],
            "duration": total_duration,
            "canvas": canvas,
            "output_canvas": canvas,
        },
        "assets": assets,
        "tracks": [
            {
                "id": "t_video_main",
                "name": "video_main",
                "type": "video",
                "track_type": "video",
                "enabled": True,
                "visible": True,
                "order": 0,
                "clips": clips,
            }
        ],
        "markers": [
            {
                "time": float(clip.get("timeline_start", 0.0)),
                "label": str(clip.get("beat") or clip.get("reason") or f"clip_{i}"),
                "name": str(clip.get("beat") or f"clip_{i}"),
            }
            for i, clip in enumerate(clips)
            if str(clip.get("beat") or "").strip()
        ],
        "metadata": {
            "assembly_build_timeline": True,
            "candidate_segments_count": len(candidates),
            "selected_segments_count": len(chosen),
            "fallback_used": fallback_used,
            "transcript_chars": len(transcript_text),
            "sheet_count": len(sheet_paths),
            "llm_used": llm_used,
        },
    }

    # 兜底空 timeline(无 candidates) → 标记空,让 validate_timeline 自然 escalated
    if not candidates:
        timeline["project"]["empty"] = True
        timeline["metadata"]["empty"] = True

    return timeline


def build_timeline_from_paths(
    *,
    media_artifact: str | Path,
    transcript_artifact: str | Path | None = None,
    ingest_artifact: str | Path | None = None,
    lang: str = "zh",
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """从产物文件路径直接调 ``build_timeline_from_media``(节点层用)。

    容错:任一文件缺失或解析失败,传 None 给主函数,自然走 fallback 路径。
    """
    def _safe_read(p: str | Path | None) -> Any:
        if not p:
            return None
        path = Path(str(p))
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    media_data = _safe_read(media_artifact)
    if not isinstance(media_data, list):
        media_data = []
    return build_timeline_from_media(
        media_reports=media_data,
        transcript_data=_safe_read(transcript_artifact),
        ingest_data=_safe_read(ingest_artifact),
        lang=lang,
        client=client,
    )


__all__ = [
    "build_timeline_from_media",
    "build_timeline_from_paths",
    "MAX_LLM_CANDIDATES",
    "SELECTION_JSON_SCHEMA",
]