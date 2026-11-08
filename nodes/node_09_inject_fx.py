"""节点 9:inject_fx — 转场 + 视频特效**原生绑定**注入(对应原文档 4.4 节)。

2026-11-09 阶段 3(效果缺失修复)重写:转场/特效不再只是往
``materials.transitions`` / ``materials.video_effects`` 里塞数组记录,而是构造
原生 material(``jy_common/native_fx.py``)并挂到目标 video segment 的
``extra_material_refs`` —— 这是仓库内已实证的 segment 引用素材通道
(贴纸 node_11 先例 / native 转换器伴随素材同源)。

与旧实现的三处契约变化:

1. **消费转场推荐计划**(旧实现只按 ``shot_plan`` 风格索引注入,canonical 路径
   下 shot_plan 缺失 → 全落 default):语义来源优先级
   ``state.storyline_plan`` clips 的 ``transition_out``/``transition_in`` →
   ``state.storyline_transition_plan`` items → 无计划时回落 style_tag 现行逻辑。
   语义 → 资源名映射在 ``templates/fx_template.json`` 的
   ``transition_semantic_map``(fade→叠化、cut→硬切不加素材)。
2. **边界只认草稿**:``max(len(video_segments) - 1, 0)``,删除对 shot_plan 的
   隐式依赖;边界 i→i+1 的转场挂在**后一段**(segments[i+1],转场在其开头播放,
   与 pyJianYingDraft 语义一致)。只有 1 个 segment → ``not_applicable``,
   不再伪装成功。
3. **状态拆分**(计划任务 5):``ok``(写出并绑定)/ ``cut``(语义=硬切)/
   ``degraded``(资源未命中或占位 ID,不写)/ ``not_applicable``(无边界)/
   模板库缺失 → failed(error_log)。汇总写入新 state 字段 ``fx_summary``。

幂等与守卫(对齐 node_08 配音轨删除守卫):我方素材 id 用确定性模式
``transition-{k}`` / ``vfx-{k}``(k 从 1 起);重放先剥离我方旧 refs、清理
未被本次复用的我方旧 material,再写入新条目。人工在剪映里加的转场/特效
(id 是 uuid)不落这些前缀,一律保留。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jy_common.native_fx import (
    OUR_TRANSITION_PATTERN,
    OUR_VIDEO_EFFECT_PATTERN,
    build_native_transition_material,
    build_native_video_effect_material,
    is_19_digit_resource_id,
)
from jy_common.template_library import TemplateLibrary, load_resource_libraries
from nodes._draft_io import apply_and_write, find_video_track, load_draft, upsert_by_id
from nodes._dual_draft import ZH, dual_draft_targets, missing_en_note
from state import WorkflowState


_DEFAULT_STYLE_TAG = "default"
_DEFAULT_TRANSITION_DURATION_US = 500_000


def _seg_duration_us(seg: dict) -> int:
    tr = seg.get("target_timerange") or {}
    return int(tr.get("duration", 0) or 0)


def _sorted_video_segments(video_track: dict) -> list[dict]:
    """按 target_timerange.start 升序返回视频段(无计时段保持原顺序)。"""
    segments = list(video_track.get("segments") or [])
    segments.sort(key=lambda s: int((s.get("target_timerange") or {}).get("start", 0) or 0))
    return segments


def _boundary_semantics_plan(state: WorkflowState) -> list[str | None]:
    """提取逐边界的转场语义(建议值;调用方按边界数截断/回落)。

    边界 i(clip i 与 i+1 之间)取 clip i 的 ``transition_out``,缺省再取
    clip i+1 的 ``transition_in`` —— 两端都有值时以 out 为准,避免一个边界
    被两端重复创建(计划任务 2)。返回列表按边界对齐:无语义的边界占位
    ``None``,调用方对 None 回落 style_tag 现行逻辑。
    """
    plan = state.get("storyline_plan")
    source: list[Any] | None = None
    if isinstance(plan, dict) and plan.get("clips"):
        source = plan["clips"]
    if source is None:
        tp = state.get("storyline_transition_plan")
        if isinstance(tp, dict) and tp.get("items"):
            source = tp["items"]
    if not source:
        return []
    out: list[str | None] = []
    # 边界只有 len(source)-1 个;末条 clip 的 out 不对应任何边界,不产出
    for i in range(len(source) - 1):
        cur = source[i] if isinstance(source[i], dict) else {}
        nxt = source[i + 1] if isinstance(source[i + 1], dict) else {}
        sem = cur.get("transition_out") or nxt.get("transition_in")
        out.append(sem.strip() if isinstance(sem, str) and sem.strip() else None)
    return out


def _resolve_transition_meta(
    template_lib: TemplateLibrary, semantic: str | None
) -> tuple[str, dict | None]:
    """语义/风格 → (status, 资源元数据)。status ∈ {"ok","cut","degraded"}。

    - 语义命中映射表:映射名资源解析成功 → ok;映射为 None → cut(硬切);
      映射名解析失败 → degraded(语义被尊重,不静默替换成别的转场)。
    - 语义缺失或未收录:回落 style_tag=default 现行逻辑(闪屏故障等 VIP 索引)。
    """
    sem_map = template_lib.transition_semantic_map()
    if semantic is not None and semantic in sem_map:
        mapped = sem_map[semantic]
        if mapped is None:
            return "cut", None
        meta = template_lib.pick_transition_by_name(mapped)
        if meta is not None and is_19_digit_resource_id(meta.get("resource_id")):
            return "ok", meta
        return "degraded", None

    placeholder = template_lib.pick_transition(_DEFAULT_STYLE_TAG) or {}
    name = placeholder.get("name")
    meta = template_lib.pick_transition_by_name(name) if name else None
    if meta is not None and is_19_digit_resource_id(meta.get("resource_id")):
        return "ok", meta
    return "degraded", None


def _resolve_video_effect_meta(
    template_lib: TemplateLibrary,
) -> tuple[dict | None, float]:
    """style_tag=default → (特效元数据, 强度 value)。未命中返回 (None, 1.0)。"""
    placeholder = template_lib.pick_video_effect(_DEFAULT_STYLE_TAG) or {}
    name = placeholder.get("name")
    meta = template_lib.pick_video_effect_by_name(name) if name else None
    if meta is None or not is_19_digit_resource_id(meta.get("resource_id")):
        return None, 1.0
    intensity = placeholder.get("intensity")
    try:
        value = float(intensity) if intensity is not None else 1.0
    except (TypeError, ValueError):
        value = 1.0
    return meta, value


def _strip_stale_owned_refs(
    segments: list[dict], patterns: tuple[re.Pattern[str], ...], kept_ids: set[str]
) -> set[str]:
    """剥掉 segment 上"我方模式 id 但本次未续用"的 refs,返回被剥的 id 集合。"""
    removed: set[str] = set()
    for seg in segments:
        refs = seg.get("extra_material_refs")
        if not isinstance(refs, list):
            continue
        kept: list[Any] = []
        for ref in refs:
            ref_str = str(ref)
            if any(p.match(ref_str) for p in patterns) and ref_str not in kept_ids:
                removed.add(ref_str)
                continue
            kept.append(ref)
        seg["extra_material_refs"] = kept
    return removed


def _remove_owned_materials(
    bucket: list[dict], patterns: tuple[re.Pattern[str], ...], kept_ids: set[str]
) -> int:
    """就地删除"我方模式 id 但本次未续用"的 material,返回删除数。"""
    kept: list[dict] = []
    removed = 0
    for item in bucket:
        item_id = str(item.get("id") or "")
        if any(p.match(item_id) for p in patterns) and item_id not in kept_ids:
            removed += 1
            continue
        kept.append(item)
    bucket[:] = kept
    return removed


def _bind_ref(seg: dict, material_id: str) -> None:
    refs = seg.setdefault("extra_material_refs", [])
    if material_id not in refs:
        refs.append(material_id)


def _inject_fx_into(
    draft: dict,
    template_lib: TemplateLibrary,
    semantics: list[str],
    tag: str,
) -> tuple[list[str], dict]:
    """把转场 + 视频特效**原生绑定**进一份草稿(就地改 ``draft``)。

    中英两份草稿共用这一个实现(``tag`` 只用于日志前缀)。两份草稿 fork 自
    同一份快照②,视频时间轴一致,所以两边得到的条数与绑定一致。

    Returns:
        (告警列表, fx_summary 条目 dict)。
    """
    warnings: list[str] = []

    materials = draft.setdefault("materials", {})
    transitions_bucket = materials.setdefault("transitions", [])
    video_effects_bucket = materials.setdefault("video_effects", [])

    video_track = find_video_track(draft)
    segments = _sorted_video_segments(video_track)
    n_boundaries = max(len(segments) - 1, 0)

    summary: dict[str, Any] = {
        "video_segments": len(segments),
        "boundaries": n_boundaries,
        "transitions_written": 0,
        "transitions_cut": 0,
        "transitions_degraded": 0,
        "video_effects_written": 0,
        "video_effects_degraded": 0,
        "not_applicable": n_boundaries == 0,
        "cleaned_stale": 0,
    }
    if n_boundaries == 0:
        warnings.append(f"[{tag}] 只有 {len(segments)} 个视频段,无剪辑边界,转场 not_applicable")

    kept_transition_ids: set[str] = set()
    for i in range(n_boundaries):
        semantic = semantics[i] if i < len(semantics) else None
        status, meta = _resolve_transition_meta(template_lib, semantic)
        if status == "cut":
            summary["transitions_cut"] += 1
            continue
        if status != "ok" or meta is None:
            summary["transitions_degraded"] += 1
            warnings.append(
                f"[{tag}] boundary {i + 1} 转场资源未解析"
                f"(semantic={semantic!r}),degraded 不写入"
            )
            continue

        # 时长钳制:不超过相邻两段的较短者(不足时退资源默认时长)
        neighbor_min = min(_seg_duration_us(segments[i]), _seg_duration_us(segments[i + 1]))
        default_us = int(round(float(meta.get("default_duration_s") or 0.0) * 1_000_000))
        duration_us = (
            min(default_us, neighbor_min)
            if neighbor_min > 0 and default_us > 0
            else (default_us or _DEFAULT_TRANSITION_DURATION_US)
        )
        material_id = f"transition-{i + 1}"
        material = build_native_transition_material(
            material_id=material_id, meta=meta, duration_us=duration_us
        )
        upsert_by_id(transitions_bucket, material)
        kept_transition_ids.add(material_id)
        _bind_ref(segments[i + 1], material_id)
        summary["transitions_written"] += 1

    # 视频特效:每段一个特效素材(原生绑定到该 segment)
    effect_meta, effect_value = _resolve_video_effect_meta(template_lib)
    kept_vfx_ids: set[str] = set()
    for k, seg in enumerate(segments, start=1):
        if effect_meta is None:
            if k == 1:
                summary["video_effects_degraded"] += max(len(segments), 1)
                warnings.append(f"[{tag}] 视频特效资源未解析,degraded 不写入")
            continue
        material_id = f"vfx-{k}"
        material = build_native_video_effect_material(
            material_id=material_id, meta=effect_meta, value=effect_value
        )
        upsert_by_id(video_effects_bucket, material)
        kept_vfx_ids.add(material_id)
        _bind_ref(seg, material_id)
        summary["video_effects_written"] += 1

    # 清理我方旧产物(重放/边界数收缩后的孤儿);人工素材 id 不落我方模式,不受影响
    removed_refs = _strip_stale_owned_refs(
        segments, (OUR_TRANSITION_PATTERN, OUR_VIDEO_EFFECT_PATTERN),
        kept_transition_ids | kept_vfx_ids,
    )
    removed_mats = _remove_owned_materials(
        transitions_bucket, (OUR_TRANSITION_PATTERN,), kept_transition_ids
    )
    removed_mats += _remove_owned_materials(
        video_effects_bucket, (OUR_VIDEO_EFFECT_PATTERN,), kept_vfx_ids
    )
    summary["cleaned_stale"] = len(removed_refs) + removed_mats

    return warnings, summary


def inject_fx(state: WorkflowState) -> dict:
    """读取草稿 → 消费转场计划 → 在剪辑边界原生绑定转场/特效 → 原子写回。

    K2=B(2026-10):对**中文主草稿 + 英文分支草稿**各注入一次(见 ``nodes/_dual_draft.py``)。
    两份草稿在同一节点内顺序处理,不跨超步,返回值契约不变(整份 state)。
    """
    status_log = list(state.get("status_log", []) or [])
    error_log = list(state.get("error_log", []) or [])

    template_lib = load_resource_libraries(
        fx_template="templates/fx_template.json",
        fx_resource="templates/fx_resource_library.json",
        text_resource="templates/text_resource_library.json",
    )

    if template_lib.is_empty:
        status_log.append("node_09_inject_fx_no_template")
        error_log.append("[node_09] 模板库缺失,降级通过(Week 3 可接受)")
        return {**state, "status_log": status_log, "error_log": error_log}

    targets = dual_draft_targets(state)
    if not targets:
        # node_05 早退时草稿不存在(D7,2026-10-20 修):降级跳过,别抛 FileNotFoundError
        # 把整个 worker 带走。
        error_log.append("[node_09] state.draft_path 缺失,跳过转场注入(降级通过)")
        status_log.append("node_09_inject_fx_skipped")
        return {**state, "status_log": status_log, "error_log": error_log}

    semantics = _boundary_semantics_plan(state)
    en_file = Path(state["draft_dir_en_branch"]) / "draft_content.json" if state.get("draft_dir_en_branch") else None

    fx_summary: dict[str, Any] = {}
    for tag, draft_file in targets:
        draft = load_draft(draft_file)
        warnings, summary = _inject_fx_into(draft, template_lib, semantics, f"node_09_{tag}")
        error_log.extend(warnings)
        fx_summary[tag] = summary
        if summary.get("not_applicable"):
            status_log.append(f"node_09_inject_fx_{tag}_not_applicable")
        # Week 5:参数从 draft_path 提升为 draft_path.parent,safe_write_draft 双写
        status_log.extend(
            apply_and_write(
                draft_file,
                draft,
                "node_09",
                done_tag=(
                    "node_09_inject_fx_done" if tag == ZH else "node_09_inject_fx_en_done"
                ),
            )
        )

    if len(targets) == 1 and en_file is not None:
        error_log.append(missing_en_note(state))

    return {**state, "status_log": status_log, "error_log": error_log, "fx_summary": fx_summary}
