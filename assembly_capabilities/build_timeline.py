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
    StubLLMClient,
    chat_json_with_trace,
    get_default_client,
)
from storyline_capabilities.prompts import render_prompt

from .draft_patch import add_operation


# 选段模板根(2026-11-15 P1-3 根治:模板在本模块 prompts/ 下,而非全局
# prompts/tasks —— 此前 render_prompt("assembly_build_timeline") 一直解析到
# 空串,选段 prompt 静默为空,模型只能靠 media 图瞎选)
_PROMPTS_ROOT = Path(__file__).resolve().parent / "prompts"


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
def manifest_to_media_reports(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """把 P1-2 素材 manifest(media.json,dict)还原成旧 media_reports list 形状。

    P1-2 起 ``assembly_media_artifact`` 指向 manifest dict
    (``{schema_version, base_plan_sha256, sources[], ...}``);旧消费方
    (:func:`build_timeline_from_media` / report 摘要)仍吃 list 形状 ——
    本 adapter 是唯一的形状归一点,候选明细 ``candidate_segments`` 原样透传。

    兼容:已经是 list 的输入原样返回;非 dict/list 返回 []。
    """
    if isinstance(manifest, list):
        return manifest
    if not isinstance(manifest, dict):
        return []
    reports: list[dict[str, Any]] = []
    for src in manifest.get("sources") or []:
        if not isinstance(src, dict):
            continue
        probe_summary = src.get("probe_summary") if isinstance(src.get("probe_summary"), dict) else {}
        analysis_summary = src.get("analysis_summary") if isinstance(src.get("analysis_summary"), dict) else {}
        analysis = dict(analysis_summary)
        # 候选明细等正式契约字段从 source 顶层透传(manifest 是唯一事实源)
        for key in ("candidate_segments", "scene_change_times", "black_ranges", "silence_ranges"):
            analysis[key] = src.get(key) if src.get(key) is not None else analysis.get(key) or []
        reports.append(
            {
                "source": str(src.get("source_path") or ""),
                "asset_id": src.get("asset_id"),
                "plan_media_id": src.get("plan_media_id"),
                "plan_clip_ids": list(src.get("plan_clip_ids") or []),
                "probe_status": src.get("probe_status"),
                "analysis_status": src.get("analysis_status"),
                "probe": {
                    "duration_seconds": src.get("duration_seconds"),
                    "summary": probe_summary,
                    "technical_risks": src.get("technical_risks") or [],
                },
                "analysis": analysis,
                "probe_text": str(src.get("probe_text") or ""),
                "analysis_text": str(src.get("analysis_text") or ""),
            }
        )
    return reports


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
        asset_id = rep.get("asset_id")
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
                index = int(seg.get("index", len(out)))
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
            entry = {
                "index": index,
                # P1-4:LLM 选段索引用全候选空间唯一序号 —— 旧实现 media 局部
                # index 跨素材塌缩(dict 后者覆盖前者),模型说"选 #0"会命中
                # 末位素材的同号候选(存量缺陷,多素材选段从不确定)。
                "selection_index": len(out),
                "start": start,
                "end": end,
                "duration": end - start,
                "source": str(seg.get("source") or "scene_boundary"),
                "media_index": media_idx,
                "media_source": source,
            }
            # P1-3:与 visual_observation.json 的关联键(manifest 有 asset_id 时)
            if asset_id:
                entry["asset_id"] = str(asset_id)
                entry["candidate_id"] = f"{asset_id}_c{max(index, 0):03d}"
            out.append(entry)
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


def attach_visual_evidence(
    candidates: list[dict[str, Any]],
    observation_data: Any,
) -> list[dict[str, Any]]:
    """把候选级视觉观察(P1-3 ``visual_observation.json``)挂到候选条目上。

    关联键 = ``candidate_id``(manifest 有 asset_id 时由
    ``_collect_candidate_segments`` 生成,与观察产物的 ``candidate_id`` 同一
    生成规则);观察缺失 / 候选无观察条目 → ``evidence=None``(选段侧据此
    标注"确定性兜底,无视觉证据")。

    黑帧旗标(``black_suspect``)随 evidence 透传,供确定性 fallback 降权排序。
    """
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(observation_data, dict):
        for entry in observation_data.get("candidates") or []:
            if isinstance(entry, dict) and entry.get("candidate_id"):
                by_id[str(entry["candidate_id"])] = entry
    if not by_id:
        return candidates
    for cand in candidates:
        cid = str(cand.get("candidate_id") or "")
        obs = by_id.get(cid)
        if obs is None:
            continue
        cand["evidence"] = {
            "candidate_id": cid,
            "status": str(obs.get("status") or "unknown"),
            "observed_flags": list(obs.get("observed_flags") or []),
            "sheet_paths": list(obs.get("sheet_paths") or []),
            "frame_paths": list(obs.get("frame_paths") or []),
        }
    return candidates


def _has_black_flag(candidate: dict[str, Any]) -> bool:
    evidence = candidate.get("evidence") or {}
    return any(
        isinstance(f, dict) and f.get("kind") == "black_suspect"
        for f in evidence.get("observed_flags") or []
    )


def _candidate_sheet_for_prompt(candidate: dict[str, Any]) -> str:
    """候选的视觉证据行(拼贴图 + 首帧),无证据时给明确占位。"""
    evidence = candidate.get("evidence") or {}
    if not evidence:
        return "evidence=none(no candidate-level observation; deterministic fallback only)"
    if evidence.get("status") != "ok":
        return f"evidence=degraded({evidence.get('status')}; detail in visual_observation.json)"
    sheets = evidence.get("sheet_paths") or []
    frames = evidence.get("frame_paths") or []
    parts = [f"sheet={sheets[0]}"] if sheets else ["sheet=none"]
    if frames:
        parts.append(f"first_frame={frames[0]}")
    flags = [f.get("kind") for f in evidence.get("observed_flags") or [] if isinstance(f, dict)]
    if flags:
        parts.append(f"flags={','.join(flags)}")
    return " ".join(parts)


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
        f"- [#{c['selection_index']}] id={c.get('candidate_id', '?')} "
        f"media={c.get('media_source', '?')} "
        f"start={c['start']:.3f}s end={c['end']:.3f}s dur={c['duration']:.3f}s "
        f"hint={c.get('source', 'scene_boundary')} "
        f"{_candidate_sheet_for_prompt(c)}"
        for c in candidates
    )
    sheet_lines = "\n".join(f"- {p}" for p in sheet_paths) or "(no contact sheet images)"
    return render_prompt(
        "assembly_build_timeline",
        "user",
        lang=lang,
        prompts_root=_PROMPTS_ROOT,
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """调 LLM 拿选段 JSON(P2-1 带留痕)。

    Returns:
        ``(raw_selection, call_trace)``。``raw_selection`` 失败时为 ``{}``
        (调用方走 fallback);``call_trace`` = ``chat_json_with_trace`` 的
        ``{ok, error, raw_head, parsed}``,供 ``llm_diagnostics`` 分类。
    """
    sys_p = render_prompt("assembly_build_timeline", "system", lang=lang, prompts_root=_PROMPTS_ROOT)
    user_p = _build_user_prompt(
        candidates=candidates,
        transcript_text=transcript_text,
        sheet_paths=sheet_paths,
        lang=lang,
    )
    # media 参数:把候选级 contact sheet 路径喂给 VLM(P1-3:每候选一张,
    # 与 candidate_lines 的 id 对应;不再是无映射的全片前 8 张)。
    candidate_sheets: list[str] = []
    for c in candidates:
        evidence = c.get("evidence") or {}
        for sheet in evidence.get("sheet_paths") or []:
            if sheet and sheet not in candidate_sheets:
                candidate_sheets.append(str(sheet))
                break
    media_payload: Optional[list[dict[str, Any]]] = (
        [{"path": p} for p in candidate_sheets[:8]] if candidate_sheets else None
    )
    try:
        # P2-1:带留痕调用 —— 区分「调用异常 / 解析失败 / 空响应 / schema 不匹配」,
        # 诊断信息随 llm_diagnostics 透出,不再让 llm_used 单字段掩盖失败形态。
        result, call_trace = chat_json_with_trace(
            system_prompt=sys_p,
            user_prompt=user_p,
            media=media_payload,
            schema=SELECTION_JSON_SCHEMA,
            client=client,
        )
    except Exception as exc:  # noqa: BLE001 — with_trace 自身异常(防御)
        return {}, {"ok": False, "error": repr(exc), "raw_head": "", "parsed": False}
    return (result if isinstance(result, dict) else {}), call_trace


# ---------------------------------------------------------------------------
# 选段还原 + 组装 timeline
# ---------------------------------------------------------------------------
def _normalize_selection(
    raw: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], bool, dict[str, int]]:
    """把 LLM 输出还原成"已选 + 已排序"的候选片段。

    Returns:
        (chosen, extras, llm_succeeded, selection_stats):
        - chosen: 还原后的候选片段 list(已按 order 排序)
        - extras: editorial_structure / canvas / selected_strategy 等元数据
        - llm_succeeded: True 表示 LLM 给出过合法 selected_segments,
          False 表示 LLM 输出空/不合法,主函数应走 fallback。
        - selection_stats: P2-1 候选索引统计
          ``{returned, invalid, duplicate, adopted}``(llm_diagnostics 消费)。

    关键纪律:
    1. ``selected_segments[].index`` 必须能在 ``candidates`` 里找到,
       找不到的忽略,避免 LLM 幻觉导致 ``validate_timeline`` 报 missing source。
    2. ``order`` 缺省按列表顺序。
    3. ``editorial_structure`` / ``task_assumption`` / ``selected_strategy`` / ``canvas``
       缺失则用占位默认值。
    """
    if not candidates:
        return [], {}, False, {"returned": 0, "invalid": 0, "duplicate": 0, "adopted": 0}
    raw_segments = raw.get("selected_segments") or []
    stats: dict[str, int] = {"returned": 0, "invalid": 0, "duplicate": 0, "adopted": 0}
    # P1-4:按全候选空间唯一序号 selection_index 还原(旧实现用 media 局部
    # index,多素材时 dict 塌缩导致选段命中错素材)
    by_index: dict[int, dict[str, Any]] = {int(c["selection_index"]): c for c in candidates}
    chosen: list[dict[str, Any]] = []
    used_indices: set[int] = set()
    for raw_seg in raw_segments:
        if not isinstance(raw_seg, dict):
            continue
        stats["returned"] += 1
        try:
            idx = int(raw_seg.get("index"))
        except (TypeError, ValueError):
            stats["invalid"] += 1
            continue
        if idx not in by_index or idx in used_indices:
            if idx in used_indices:
                stats["duplicate"] += 1
            else:
                stats["invalid"] += 1
            continue
        base = dict(by_index[idx])
        base["reason"] = (
            str(raw_seg.get("reason") or "").strip()
            or f"LLM-selected segment #{idx}"
        )
        # P1-3:选段依据与证据引用(证据随 evidence 一起从 attach 步透传)
        base["selection_basis"] = "llm_selection"
        evidence = base.get("evidence") or {}
        base["evidence_refs"] = [str(base.get("candidate_id") or f"index:{idx}")] + [
            str(p) for p in (evidence.get("frame_paths") or [])[:2]
        ]
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
    stats["adopted"] = len(chosen)
    return chosen, extras, bool(chosen), stats


# ---------------------------------------------------------------------------
# P2-1:LLM 调用诊断(请求状态与选段采纳状态分离,stub 不冒充真实成功)
# ---------------------------------------------------------------------------
#: request_status 全集(llm_diagnostics.request_status)
LLM_REQUEST_STATUSES = (
    "not_attempted",      # 无候选,未发起请求
    "ok",                 # 调用成功 + 解析成功 + selected_segments 合法非空
    "empty_response",     # 调用成功但模型返回空选段(合法的"无候选可用"判断)
    "parse_error",        # 调用成功但返回内容解析不出 dict
    "schema_mismatch",    # 解析成功但 selected_segments 缺失/非 list
    "exception",          # client.chat 抛异常
    "timeout",            # exception 的子类:错误信息含 timeout
)


def _llm_client_type(client: Optional[LLMClient]) -> str:
    """客户端类型三分类:stub(默认假客户端)/ real(真实后端)/ none(未注入)。"""
    if client is None:
        return "none"
    if isinstance(client, StubLLMClient):
        return "stub"
    return "real"


def build_llm_diagnostics(
    *,
    client: Optional[LLMClient],
    call_trace: dict[str, Any],
    raw_selection: dict[str, Any],
    selection_stats: dict[str, int],
    chosen_count: int,
    fallback_used: bool,
    attempted: bool,
    elapsed_seconds: float = 0.0,
) -> dict[str, Any]:
    """组装 P2-1 LLM 诊断 dict(纯函数;llm_used 不再是"是否真请求"的唯一信号)。

    字段语义(计划文档 P2-1):
    - ``llm_attempted``:是否真的发起过模型请求(candidates 非空才尝试);
    - ``llm_client_type``:stub / real / none —— stub 永远显式标注,不冒充真实成功;
    - ``request_status``:ok / empty_response / parse_error / schema_mismatch /
      exception / timeout / not_attempted(全集见 :data:`LLM_REQUEST_STATUSES`);
    - ``selection_stats``:模型返回的候选索引统计(returned/invalid/duplicate/adopted)。
    """
    ok = bool(call_trace.get("ok"))
    parsed = bool(call_trace.get("parsed"))
    error = str(call_trace.get("error") or "")
    segs = (
        raw_selection.get("selected_segments")
        if isinstance(raw_selection, dict)
        else None
    )
    if not attempted:
        status = "not_attempted"
    elif not ok:
        lowered = error.lower()
        status = (
            "timeout"
            if ("timeout" in lowered or "timed out" in lowered)
            else "exception"
        )
    elif not parsed:
        status = "parse_error"
    elif not isinstance(segs, list):
        status = "schema_mismatch"
    elif not segs:
        status = "empty_response"
    else:
        status = "ok"

    if status in ("exception", "timeout"):
        failure_reason = error
    elif status == "parse_error":
        failure_reason = str(call_trace.get("raw_head") or "")[:400] or "模型返回无法解析为 JSON"
    elif status == "schema_mismatch":
        failure_reason = "解析成功但 selected_segments 缺失或非 list"
    elif status == "empty_response":
        failure_reason = "模型返回空选段(走确定性兜底/不打补丁)"
    elif status == "not_attempted":
        failure_reason = "无候选片段,未发起请求"
    else:
        failure_reason = ""

    zeros = {"returned": 0, "invalid": 0, "duplicate": 0, "adopted": 0}
    merged_stats = dict(zeros)
    merged_stats.update({k: int(v) for k, v in (selection_stats or {}).items() if k in zeros})
    return {
        "llm_attempted": bool(attempted),
        "llm_client_type": _llm_client_type(client),
        "model_name": type(client).__name__ if client is not None else "",
        "request_status": status,
        "response_valid": status == "ok",
        "selection_applied": chosen_count > 0,
        "fallback_used": bool(fallback_used),
        "failure_reason": failure_reason,
        "elapsed_seconds": round(float(elapsed_seconds or 0.0), 4),
        "selection_stats": merged_stats,
    }


def _fallback_chosen(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LLM 失败时的占位选段算法(P1-3:视觉证据可解释排序)。

    排序规则:无 black_suspect 旗标的候选优先,旗标命中者降权到尾部;
    同旗标内保持原始候选顺序。整段占位(placeholder_full)仍优先保留
    (阶段二兼容行为)。每个入选候选带 ``selection_basis`` 标注,选段依据
    可追溯到视觉证据或"无证据的确定性兜底"。
    """
    ranked = sorted(
        enumerate(candidates),
        key=lambda pair: (0 if pair[1].get("source") == "placeholder_full" else 1,
                          1 if _has_black_flag(pair[1]) else 0,
                          pair[0]),
    )
    out: list[dict[str, Any]] = []
    for original_pos, c in ranked:
        evidence = c.get("evidence") or {}
        evidence_refs = [str(c.get("candidate_id") or f"index:{c.get('index', original_pos)}")] + [
            str(p) for p in (evidence.get("frame_paths") or [])[:2]
        ]
        if c.get("source") == "placeholder_full":
            reason = f"placeholder full-segment fallback (start={c['start']:.3f}s)"
            out.append(dict(c, reason=reason, order=len(out), selection_basis="deterministic_fallback",
                            evidence_refs=evidence_refs))
        elif len(out) < 6:
            flag_note = ", black-flag demoted in ranking" if _has_black_flag(c) else ""
            reason = (
                f"placeholder scene-boundary fallback (start={c['start']:.3f}s, "
                f"dur={c['duration']:.3f}s{flag_note})"
            )
            out.append(dict(c, reason=reason, order=len(out), selection_basis="deterministic_fallback",
                            evidence_refs=evidence_refs))
        else:
            break
    if not out and candidates:
        c = candidates[0]
        evidence = c.get("evidence") or {}
        out.append(dict(
            c,
            reason="placeholder forced first-segment fallback",
            order=0,
            selection_basis="deterministic_fallback",
            evidence_refs=[str(c.get("candidate_id") or "index:0")] + [
                str(p) for p in (evidence.get("frame_paths") or [])[:2]
            ],
        ))
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
        # P1-3:选段依据 + 证据引用透传到 clip(report/QC 可追溯)
        if seg.get("candidate_id"):
            clip["candidate_id"] = str(seg["candidate_id"])
        if seg.get("selection_basis"):
            clip["selection_basis"] = str(seg["selection_basis"])
        if seg.get("evidence_refs"):
            clip["evidence_refs"] = list(seg["evidence_refs"])
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
    observation_data: Any = None,
    lang: str = "zh",
    client: Optional[LLMClient] = None,
    fallback_to_placeholder: bool = True,
) -> dict[str, Any]:
    """阶段三主入口:从 media + transcript + ingest 选段 + 组装 timeline。

    Args:
        media_reports: ``assembly_discover_and_probe`` 写出的 ``media.json``
            (P1-2 起 manifest dict,经 ``manifest_to_media_reports`` 归一)。
        transcript_data: 可选的转写文本上下文(B1 ASR 移除后主图不再提供;
            保留通用可选接口给评估脚本等非主图调用方,允许 None)。
        ingest_data: 旧整段观察产物(允许 None;P1-3 起主图改传
            ``observation_data``,本参数保留给非主图调用方)。
        observation_data: P1-3 候选级视觉观察(``visual_observation.json``,
            允许 None = 无候选级证据,选段标注确定性兜底)。
        lang: prompt 语言(目前只接 ``"zh"``)。
        client: LLM client。None 时用 ``vlm_client.get_default_client()``(默认 Stub)。
        fallback_to_placeholder: LLM 选段为空时是否回退到确定性占位算法。

    Returns:
        ``timeline.json`` 的完整 dict,符合 ``schemas/timeline.schema.json`` 关键契约。
        关键字段:``project`` / ``assets[]`` / ``sequence`` / ``tracks[].clips[]``,
        每个 clip 必有 ``source`` / ``start`` / ``end`` / ``reason``;
        P1-3 起 clip 另带 ``selection_basis`` / ``evidence_refs``。
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
    # P1-3:候选级视觉证据关联(F-04 — 观察数据必须进入选段决策)
    candidates = attach_visual_evidence(candidates, observation_data)
    # prompt 的 sheet 清单 = 候选自己的拼贴图(不再是全片无映射清单)
    candidate_sheets: list[str] = []
    for c in candidates:
        for sheet in (c.get("evidence") or {}).get("sheet_paths") or []:
            if sheet and sheet not in candidate_sheets:
                candidate_sheets.append(str(sheet))
                break
    sheet_paths = candidate_sheets or _format_sheet_paths(ingest_data)
    transcript_text = _format_transcript_for_prompt(transcript_data)

    llm_used = False
    fallback_used = False
    raw_selection: dict[str, Any] = {}
    chosen: list[dict[str, Any]] = []
    extras: dict[str, Any] = {}
    llm_diagnostics: dict[str, Any] = build_llm_diagnostics(
        client=client,
        call_trace={},
        raw_selection={},
        selection_stats={},
        chosen_count=0,
        fallback_used=False,
        attempted=False,
    )
    if candidates:
        llm_started = time.perf_counter()
        raw_selection, call_trace = _call_llm_for_selection(
            candidates=candidates,
            transcript_text=transcript_text,
            sheet_paths=sheet_paths,
            lang=lang,
            client=client,
        )
        llm_elapsed = time.perf_counter() - llm_started
        # llm_used = 调用 LLM 成功且拿到非空 raw_selection(有 selected_segments 字段)
        llm_used = bool(raw_selection) and isinstance(raw_selection.get("selected_segments"), list)
        chosen, extras, llm_succeeded, sel_stats = _normalize_selection(raw_selection, candidates)
        llm_diagnostics = build_llm_diagnostics(
            client=client,
            call_trace=call_trace,
            raw_selection=raw_selection,
            selection_stats=sel_stats,
            chosen_count=len(chosen),
            fallback_used=False,
            attempted=True,
            elapsed_seconds=llm_elapsed,
        )
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
        # P2-1:fallback 判定在选段还原之后,诊断里的 fallback_used 回写终值
        llm_diagnostics["fallback_used"] = bool(fallback_used)

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
        "llm_diagnostics": llm_diagnostics,
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
            # B1 ASR 移除后主图不再提供 transcript;仅在调用方显式传入时计入
            "transcript_chars": len(transcript_text) if transcript_data is not None else 0,
            "sheet_count": len(sheet_paths),
            "llm_used": llm_used,
            # P1-3:视觉证据消费统计(F-04 死产物检查的数据面)
            "candidates_with_evidence": sum(
                1 for c in chosen if c.get("evidence") and c["evidence"].get("status") == "ok"
            ),
            "candidates_without_evidence": sum(1 for c in chosen if not c.get("evidence")),
            "black_flagged_selected": sum(1 for c in chosen if _has_black_flag(c)),
            "visual_evidence_used": bool(
                observation_data and isinstance(observation_data, dict)
                and observation_data.get("candidate_count")
            ),
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
    observation_artifact: str | Path | None = None,
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
    # P1-2:media.json 升级为 manifest dict —— 统一经 manifest_to_media_reports
    # 归一成旧 list 形状;list 输入原样透传(兼容旧产物与非主图调用方)。
    media_reports = manifest_to_media_reports(media_data)
    return build_timeline_from_media(
        media_reports=media_reports,
        transcript_data=_safe_read(transcript_artifact),
        ingest_data=_safe_read(ingest_artifact),
        observation_data=_safe_read(observation_artifact),
        lang=lang,
        client=client,
    )


# ---------------------------------------------------------------------------
# P1-4(第六期):基于原始 plan 的候选编辑计划(DraftPatch)
# ---------------------------------------------------------------------------
def _black_ranges_by_source(media_reports: list[dict[str, Any]]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for rep in media_reports or []:
        if not isinstance(rep, dict):
            continue
        source = str(rep.get("source") or "")
        analysis = rep.get("analysis") or {}
        if isinstance(analysis, dict):
            out[source] = [r for r in analysis.get("black_ranges") or [] if isinstance(r, dict)]
    return out


def _largest_clean_subrange(
    in_s: float, out_s: float, black_ranges: list[dict], *, min_len: float = 0.5
) -> tuple[float, float] | None:
    """[in_s, out_s] 剔除黑帧重叠后的最大连续干净子区间(秒);过短返回 None。"""
    blacks = []
    for r in black_ranges:
        try:
            bs = max(in_s, float(r.get("start") or 0.0))
            be = min(out_s, float(r.get("end") or 0.0))
        except (TypeError, ValueError):
            continue
        if be > bs:
            blacks.append((bs, be))
    blacks.sort()
    clean: list[tuple[float, float]] = []
    cursor = in_s
    for bs, be in blacks:
        if be <= cursor:
            continue
        if bs > cursor:
            clean.append((cursor, bs))
        cursor = max(cursor, be)
    if cursor < out_s:
        clean.append((cursor, out_s))
    if not clean:
        return None
    best = max(clean, key=lambda seg: seg[1] - seg[0])
    return best if (best[1] - best[0]) >= min_len else None


def _plan_media_path_map(base_plan: dict[str, Any]) -> dict[str, str]:
    """plan source_media media_id → 本机路径。"""
    from storyline.mapper import file_uri_to_path

    out: dict[str, str] = {}
    for m in base_plan.get("source_media") or []:
        if not isinstance(m, dict):
            continue
        try:
            out[str(m.get("media_id"))] = file_uri_to_path(str(m.get("file_uri") or ""))
        except ValueError:
            continue
    return out


def _overlap_seconds(in_ms: int, out_ms: int, black_ranges: list[dict]) -> float:
    overlap = 0.0
    in_s, out_s = in_ms / 1000.0, out_ms / 1000.0
    for r in black_ranges:
        try:
            bs, be = float(r.get("start") or 0.0), float(r.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        lo, hi = max(in_s, bs), min(out_s, be)
        if hi > lo:
            overlap += hi - lo
    return overlap


def _add_deterministic_evidence_ops(
    base_plan: dict[str, Any],
    patch: dict[str, Any],
    media_reports: list[dict[str, Any]],
) -> int:
    """逐 clip 对照 manifest 黑帧区间产出 trim/remove ops(确定性,证据驱动)。

    策略(P1-4 保守档):
    - 黑帧覆盖率 >= 80% 或无 >=0.5s 干净子区间 → remove op(证据 = 命中的黑帧区间);
    - 覆盖 >= 0.3s 且存在干净子区间 → trim op(裁到最大干净子区间,速度恒 1.0)。
    返回追加的 op 数。
    """
    blacks = _black_ranges_by_source(media_reports)
    path_by_media = _plan_media_path_map(base_plan)
    added = 0
    for clip in base_plan.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        ranges = blacks.get(path_by_media.get(str(clip.get("source_media_id"))) or "") or []
        if not ranges:
            continue
        in_ms, out_ms = int(clip["source_in_ms"]), int(clip["source_out_ms"])
        span_s = (out_ms - in_ms) / 1000.0
        if span_s <= 0:
            continue
        overlap_s = _overlap_seconds(in_ms, out_ms, ranges)
        if overlap_s <= 0:
            continue
        evidence = [f"black:{r.get('start')}-{r.get('end')}" for r in ranges]
        clean = _largest_clean_subrange(in_ms / 1000.0, out_ms / 1000.0, ranges)
        coverage = overlap_s / span_s
        if clean is None or coverage >= 0.8:
            add_operation(
                patch, operation="remove", target_clip_id=str(clip.get("clip_id")),
                reason=(
                    f"黑帧覆盖率 {coverage:.0%}(>=80%)或无可用干净子区间,移除坏片段"
                ),
                evidence_refs=evidence,
                confidence=0.95,
            )
            added += 1
        elif overlap_s >= 0.3:
            new_in = int(round(clean[0] * 1000))
            new_out = int(round(clean[1] * 1000))
            if (new_out - new_in) < (out_ms - in_ms):
                add_operation(
                    patch, operation="trim", target_clip_id=str(clip.get("clip_id")),
                    source_in_ms=new_in, source_out_ms=new_out,
                    reason=f"剔除源区间内黑帧(原 {in_ms}-{out_ms}ms,黑帧 {overlap_s:.2f}s)",
                    evidence_refs=evidence,
                    confidence=0.9,
                )
                added += 1
    return added


def _add_llm_select_ops(
    base_plan: dict[str, Any],
    patch: dict[str, Any],
    chosen: list[dict[str, Any]],
    media_reports: list[dict[str, Any]],
) -> int:
    """LLM 选中的候选 → select ops(候选来自 plan 已有 source_media,不虚构素材)。

    映射:候选 asset_id → manifest 的 plan_media_id;与既有 clip 源区间重叠
    >= 30% 的候选视为"该 clip 的画面证据已入编"(不打 op);否则追加新 clip
    (按 LLM 给出的 order 顺序)。返回追加的 op 数。
    """
    media_id_by_source: dict[str, str] = {}
    for rep in media_reports or []:
        if isinstance(rep, dict) and rep.get("plan_media_id"):
            media_id_by_source[str(rep.get("source") or "")] = str(rep["plan_media_id"])

    clips_by_media: dict[str, list[dict]] = {}
    for clip in base_plan.get("clips") or []:
        if isinstance(clip, dict):
            clips_by_media.setdefault(str(clip.get("source_media_id")), []).append(clip)

    added = 0
    for seg in chosen:
        media_id = media_id_by_source.get(str(seg.get("media_source") or ""))
        if not media_id:
            continue
        start_ms = int(round(float(seg["start"]) * 1000))
        end_ms = int(round(float(seg["end"]) * 1000))
        span = max(1, end_ms - start_ms)
        matched = False
        for clip in clips_by_media.get(media_id, []):
            c_in, c_out = int(clip["source_in_ms"]), int(clip["source_out_ms"])
            lo, hi = max(start_ms, c_in), min(end_ms, c_out)
            if hi > lo and (hi - lo) / span >= 0.3:
                matched = True
                break
        if matched:
            continue
        evidence = [str(seg.get("candidate_id") or f"index:{seg.get('index')}")] + [
            str(p) for p in (seg.get("evidence") or {}).get("frame_paths", [])[:2]
        ] if seg.get("evidence") else [str(seg.get("candidate_id") or f"index:{seg.get('index')}")]
        add_operation(
            patch, operation="select", target_clip_id=f"clip_sel_{added + 1:04d}",
            source_media_id=media_id, source_in_ms=start_ms, source_out_ms=end_ms,
            reason=str(seg.get("reason") or "LLM 选中且未入编的候选片段"),
            evidence_refs=evidence,
            extra={"beat": seg.get("beat"), "editorial_order": seg.get("order")},
        )
        added += 1
    return added


def build_candidate_plan_from_media(
    *,
    base_plan: dict[str, Any],
    base_plan_sha256: str,
    media_reports: list[dict[str, Any]],
    observation_data: Any = None,
    lang: str = "zh",
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """P1-4 主入口:在原始 storyline_plan 上生成候选编辑计划(DraftPatch)。

    设计纪律(设计文档 §3.3):
    - 基线不可变(apply_patch 只动深拷贝);候选补丁有证据可追溯;
    - LLM 失败 / 空选段 → 只保留确定性证据 ops;连确定性 ops 都没有时
      候选 = 基线 + ``no_safe_candidate`` 标记(**不再整段首尾相接**);
    - ``CanonicalTimeline.model_validate`` 为硬门;候选非法且基线也非法时
      ``validation=failed``,由节点层显式落 error(不静默放行)。

    Returns:
        dict: ``candidate_plan``(已剥内部标记)/ ``patch`` / ``patch_report`` /
        ``diffs`` / ``validation`` / 统计字段。
    """
    from storyline.contract import CanonicalTimeline

    from .draft_patch import (
        add_operation,
        apply_patch,
        diff_plans,
        new_patch,
        plan_sha256,
        strip_internal_marks,
    )

    started = time.perf_counter()
    if client is None:
        client = get_default_client()

    candidates = _collect_candidate_segments(media_reports)
    if len(candidates) > MAX_LLM_CANDIDATES:
        candidates = candidates[:MAX_LLM_CANDIDATES]
    candidates = attach_visual_evidence(candidates, observation_data)
    transcript_text = _format_transcript_for_prompt(None)

    llm_used = False
    chosen: list[dict[str, Any]] = []
    llm_diagnostics: dict[str, Any] = build_llm_diagnostics(
        client=client,
        call_trace={},
        raw_selection={},
        selection_stats={},
        chosen_count=0,
        fallback_used=False,
        attempted=False,
    )
    if candidates:
        llm_started = time.perf_counter()
        raw_selection, call_trace = _call_llm_for_selection(
            candidates=candidates,
            transcript_text=transcript_text,
            sheet_paths=[],  # P1-3:拼贴图经 candidate evidence 注入 media 与 prompt
            lang=lang,
            client=client,
        )
        llm_elapsed = time.perf_counter() - llm_started
        llm_used = bool(raw_selection) and isinstance(raw_selection.get("selected_segments"), list)
        chosen, _extras, _ok, sel_stats = _normalize_selection(raw_selection, candidates)
        llm_diagnostics = build_llm_diagnostics(
            client=client,
            call_trace=call_trace,
            raw_selection=raw_selection,
            selection_stats=sel_stats,
            chosen_count=len(chosen),
            fallback_used=False,
            attempted=True,
            elapsed_seconds=llm_elapsed,
        )

    patch = new_patch(base_plan_sha256, created_by="assembly_build_timeline")
    det_ops = _add_deterministic_evidence_ops(base_plan, patch, media_reports)
    llm_ops = _add_llm_select_ops(base_plan, patch, chosen, media_reports)

    candidate, patch_report = apply_patch(base_plan, patch, expect_base_sha256=base_plan_sha256)
    diffs = diff_plans(base_plan, candidate)
    cleaned = strip_internal_marks(candidate)

    validation_failed: str | None = None
    try:
        CanonicalTimeline.model_validate(cleaned)
    except Exception as exc:  # noqa: BLE001 — ValidationError / ValueError
        validation_failed = repr(exc)

    no_safe_candidate = not patch_report["applied_operations"] and not chosen
    return {
        "candidate_plan": cleaned,
        "candidate_plan_sha256": plan_sha256(cleaned),
        "patch": patch,
        "patch_report": patch_report,
        "diffs": diffs,
        "validation": {"pass": validation_failed is None, "error": validation_failed},
        "no_safe_candidate": no_safe_candidate,
        "llm_used": llm_used,
        "llm_diagnostics": llm_diagnostics,
        "deterministic_ops": det_ops,
        "llm_ops": llm_ops,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }


def candidate_projection_timeline(
    candidate_plan: dict[str, Any],
    media_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """候选 CanonicalTimeline → assembly timeline 投影(validate/render/QC 工具链沿用)。

    投影只承载视频轨(source/timeline 秒),供 ``validate_timeline`` /
    ``render_preview`` / ``qc_preview`` 继续工作;真正要生成剪映草稿时
    P1-5 会用与正式链相同的 mapper 映射(QC 临时草稿),两者互不替代。
    """
    reports_by_source: dict[str, dict[str, Any]] = {}
    for rep in media_reports or []:
        if isinstance(rep, dict) and rep.get("source"):
            reports_by_source[str(rep["source"])] = rep

    assets: list[dict[str, Any]] = []
    media_asset: dict[str, str] = {}
    path_by_media = _plan_media_path_map(candidate_plan)
    for i, m in enumerate(candidate_plan.get("source_media") or [], start=1):
        if not isinstance(m, dict):
            continue
        path = path_by_media.get(str(m.get("media_id"))) or str(m.get("file_uri") or "")
        rep = reports_by_source.get(path) or {}
        asset_id = str(rep.get("asset_id") or f"a{i}")
        media_asset[str(m.get("media_id"))] = asset_id
        duration = (rep.get("probe") or {}).get("duration_seconds")
        if duration is None:
            duration = (m.get("duration_ms") or 0) / 1000.0
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

    clips: list[dict[str, Any]] = []
    for i, clip in enumerate(candidate_plan.get("clips") or [], start=1):
        if not isinstance(clip, dict):
            continue
        start_s = int(clip["source_in_ms"]) / 1000.0
        end_s = int(clip["source_out_ms"]) / 1000.0
        t_in = int(clip["timeline_in_ms"]) / 1000.0
        t_out = int(clip["timeline_out_ms"]) / 1000.0
        clips.append({
            "track_type": "video",
            "id": str(clip.get("clip_id") or f"clip_{i:04d}"),
            "asset_id": media_asset.get(str(clip.get("source_media_id")), "a1"),
            "source": path_by_media.get(str(clip.get("source_media_id")), ""),
            "start": start_s,
            "end": end_s,
            "duration": end_s - start_s,
            "timeline_start": t_in,
            "timeline_end": t_out,
            "speed": 1.0,
            "volume": 1.0,
            "opacity": 1.0,
            "enabled": True,
            "reason": str(clip.get("_patch_reason") or f"baseline clip {clip.get('clip_id')}"),
        })

    total = clips[-1]["timeline_end"] if clips else 0.0
    return {
        "version": "assembly-candidate-projection-1",
        "task": "candidate plan projection (auto-video-editor P1-4)",
        "project": {
            "name": "assembly-candidate",
            "selected_segments_count": len(clips),
            "elapsed_seconds": 0.0,
        },
        "sequence": {
            "fps": DEFAULT_FPS,
            "duration": total,
            "canvas": {"width": DEFAULT_CANVAS_WIDTH, "height": DEFAULT_CANVAS_HEIGHT, "fps": DEFAULT_FPS},
            "output_canvas": {"width": DEFAULT_CANVAS_WIDTH, "height": DEFAULT_CANVAS_HEIGHT, "fps": DEFAULT_FPS},
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
        "markers": [],
        "metadata": {
            "candidate_projection": True,
            "clip_count": len(clips),
        },
    }


__all__ = [
    "build_timeline_from_media",
    "build_timeline_from_paths",
    "build_candidate_plan_from_media",
    "candidate_projection_timeline",
    "manifest_to_media_reports",
    "MAX_LLM_CANDIDATES",
    "SELECTION_JSON_SCHEMA",
]