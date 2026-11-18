"""节点 9:inject_fx — 转场 + 视频特效**原生绑定**注入(对应原文档 4.4 节)。

2026-11-09 阶段 3 重写:转场/特效构造原生 material(``jy_common/native_fx.py``)
并挂到目标 video segment 的 ``extra_material_refs``。

2026-11-17 池化重写(核验报告 §6.4-§6.7,转场/特效直接使用剪映资源库):

1. **转场风格池轮换**:语义 → ``fx_template.transition_semantic_map`` → 池 key
   → ``transition_pools`` 池内确定性轮换。池成员全部是
   ``fx_resource_library.json`` transitions 段的真实剪映资源(含 VIP,会员已
   确认可用)。轮换 seed = sha256(``job_id|boundary_index|pool_key``) ——
   **不含 zh/en tag、不用 python hash()**(str 盐化随机),中英两份草稿
   逐边界选出相同转场,跨进程重放稳定。未收录语义统一回退
   ``pool_gentle``(不再落强视觉默认值)。
2. **视频特效克制点缀**(2026-11-11"抖动模糊"全片挂载事故后重新启用):
   白名单只收氛围光效类(fx_template.video_effects),挂载位固定公式
   ``{0, n-1} ∪ {i∈[1,n-2] 且 i%3==0}``(0-based:首段、尾段、第4/7/10…段),
   特效按**挂载序号**在白名单内轮换;intensity 钳制 [0,1];空白名单
   fail-closed 不写入(记 status 说明,不刷 error_log)。
3. **all-cut 保底**(核验报告 §6.7):``required_features.transitions=True``
   且多镜头且全部边界语义=硬切时,把边界 0 回退 ``fade`` 池 —— 保证至少
   一个真实转场,交付 QA 不因"预期全硬切"误判注入失败后无限 repair。
4. **边界只认草稿**:``max(len(video_segments) - 1, 0)``;边界 i→i+1 的转场
   挂在**后一段**(segments[i+1])。只有 1 个 segment → ``not_applicable``。
5. **状态拆分**:``ok``(写出并绑定)/ ``cut``(语义=硬切)/ ``degraded``
   (池缺失/资源未命中,不写)/ ``not_applicable``(无边界);模板库缺失 →
   failed(error_log)。执行结果以草稿原生条目 + status_log 为准。
6. **开场转场加强**(第八期阶段3,设计文档 §6.3):用户【开场】标记正向要求
   时,边界 0 语义强制 ``whip`` → pool_whip(强动势/甩镜风格,非"全库最强"
   承诺);按**真实草稿边界数**生效(语义计划空/短先补 None 占位),单镜头
   not_applicable,资源解析失败不记 applied;覆盖先于 all-cut 判定。

幂等与守卫(对齐 node_08 配音轨删除守卫):我方素材 id 用确定性模式
``transition-{k}`` / ``vfx-{k}``(k 从 1 起);重放先剥离我方旧 refs、清理
未被本次复用的我方旧 material,再写入新条目。人工在剪映里加的转场/特效
(id 是 uuid)不落这些前缀,一律保留。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional

from draft_ops.draft_integrity import resolve_required_features
from jy_common.job_identity import resolve_job_id
from jy_common.native_fx import (
    OUR_TRANSITION_PATTERN,
    OUR_VIDEO_EFFECT_PATTERN,
    build_native_transition_material,
    build_native_video_effect_material,
    is_19_digit_resource_id,
)
from jy_common.template_library import TemplateLibrary, load_resource_libraries
from jy_common.user_directives import opening_boost_requested, parse_user_directives
from langchain_core.runnables import RunnableConfig
from nodes._draft_io import apply_and_write, find_video_track, load_draft, upsert_by_id
from nodes._dual_draft import ZH, dual_draft_targets, missing_en_note
from nodes.storyline._common import (
    PLAN_SOURCE_FINAL_INVALID,
    effective_canonical_plan,
    extract_user_request,
)
from state import WorkflowState


_DEFAULT_FALLBACK_POOL = "pool_gentle"
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
    ``None``,调用方对 None 回落 pool_gentle。
    """
    # P1-7:优先消费 assembly 最终批准计划(视频轨被 patch 改写后按旧计划
    # 提转场边界会错位);最终计划不可用时回落原始 storyline_plan。
    plan, plan_source, _plan_notes = effective_canonical_plan(state)
    if plan_source == PLAN_SOURCE_FINAL_INVALID:
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


def _stable_pool_seed(job_id: str, boundary_index: int, pool_key: str) -> int:
    """确定性池轮换 seed(sha256 前 8 位;跨进程稳定)。

    输入**不含** zh/en 日志 tag(双语草稿逐边界同选,核验报告 §4.9),
    也**不用** ``hash()``(str 盐化随机,破坏重放幂等)。
    """
    seed_input = f"{job_id}|{boundary_index}|{pool_key}"
    return int(hashlib.sha256(seed_input.encode("utf-8")).hexdigest()[:8], 16)


def _resolve_transition_meta(
    template_lib: TemplateLibrary,
    semantic: str | None,
    boundary_index: int,
    job_id: str,
) -> tuple[str, dict | None, str | None]:
    """语义 → (status, 资源元数据, 池 key)。status ∈ {"ok","cut","degraded"}。

    - 语义命中映射表:映射为池 key → 池内确定性轮换取名并按 name 解析资源
      → ok;映射为 None → cut(硬切);池缺失/为空/成员解析失败 → degraded
      (语义被尊重,不静默换池)。
    - 语义缺失或未收录:回退 ``pool_gentle`` 同样轮换(核验报告 §6.5)。
    """
    sem_map = template_lib.transition_semantic_map()
    pool_key: str | None
    if semantic is not None and semantic in sem_map:
        pool_key = sem_map[semantic]
        if pool_key is None:
            return "cut", None, None
    else:
        pool_key = _DEFAULT_FALLBACK_POOL
    members = template_lib.transition_pool(pool_key)
    if not members:
        return "degraded", None, pool_key
    seed = _stable_pool_seed(job_id, boundary_index, pool_key)
    name = members[(boundary_index + seed) % len(members)]
    meta = template_lib.pick_transition_by_name(name)
    if meta is not None and is_19_digit_resource_id(meta.get("resource_id")):
        return "ok", meta, pool_key
    return "degraded", None, pool_key


def _resolve_video_effect_candidates(
    template_lib: TemplateLibrary,
) -> tuple[list[tuple[dict, float]], list[str]]:
    """特效白名单 → [(资源元数据, 钳制后强度)] 保序解析;解析失败名单返回。

    - 每行必须经 ``pick_video_effect_by_name`` 在资源库 **video_effects 段**
      精确命中 + 19 位 ID 校验(核验报告 §4.3:同名转场不会跨段误中);
    - intensity 钳制 [0,1](核验报告 §6.6)。
    """
    resolved: list[tuple[dict, float]] = []
    failed: list[str] = []
    for row in template_lib.video_effect_candidates():
        name = str(row.get("name") or "")
        meta = template_lib.pick_video_effect_by_name(name)
        if meta is None or not is_19_digit_resource_id(meta.get("resource_id")):
            failed.append(name)
            continue
        try:
            value = float(row.get("intensity"))
        except (TypeError, ValueError):
            value = 1.0
        resolved.append((meta, min(max(value, 0.0), 1.0)))
    return resolved, failed


def _effect_segment_positions(n_segments: int) -> set[int]:
    """克制点缀挂载位(0-based;核验报告 §4.10 固定公式)。

    首段、末段,以及中间每第 3 段(0-based i∈[1,n-2] 且 i%3==0 →
    第 4/7/10…段)。n=1 → {0};n=2 → {0,1};n=3 → {0,2};n=7 → {0,3,6}。
    """
    if n_segments <= 0:
        return set()
    selected = {0, n_segments - 1}
    selected.update(i for i in range(1, n_segments - 1) if i % 3 == 0)
    return selected


def _boundary_is_cut(semantic: str | None, sem_map: dict[str, str | None]) -> bool:
    """边界语义是否为显式硬切(在映射表中且映射为 None)。"""
    return semantic is not None and semantic in sem_map and sem_map[semantic] is None


def _all_boundaries_cut(
    semantics: list[str | None],
    sem_map: dict[str, str | None],
    n_boundaries: int,
) -> bool:
    """多镜头草稿是否全部边界都被推荐为硬切(核验报告 §6.7)。"""
    if n_boundaries <= 0:
        return False
    return all(
        _boundary_is_cut(semantics[i] if i < len(semantics) else None, sem_map)
        for i in range(n_boundaries)
    )


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
    semantics: list[str | None],
    tag: str,
    job_id: str,
    required_transitions: bool,
    opening_boost: bool = False,
) -> tuple[list[str], dict]:
    """把转场 + 视频特效**原生绑定**进一份草稿(就地改 ``draft``)。

    中英两份草稿共用这一个实现(``tag`` 只用于日志前缀,**不进**轮换 seed)。
    两份草稿 fork 自同一份快照②,视频时间轴一致,所以两边得到的条数、绑定
    与转场名一致。

    ``opening_boost``(第八期阶段3,设计文档 §6.3):【开场】标记正向要求时,
    边界 0 语义强制 ``whip`` → pool_whip(强动势/甩镜风格池;P1-01/P1-02:
    这是第一处镜头切点的风格覆盖,不承诺 0-3s,不代表全库最强)。按**真实
    草稿边界数**生效(P1-03):语义计划为空/偏短时先补 ``None`` 占位(缺失
    语义走既有 pool_gentle 回退),单镜头无边界 → not_applicable;覆盖发生在
    all-cut 判定**之前**,边界 0=whip 后不再判「全硬切」。zh/en 共享同一
    ``opening_boost`` 与 ``job_id``,双稿边界 0 转场名一致。

    Returns:
        (告警列表, 汇总 dict)。
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
        "all_cut_fallback": False,
        "video_effects_written": 0,
        "video_effects_failed": 0,
        "video_effects_empty_whitelist": False,
        "not_applicable": n_boundaries == 0,
        "cleaned_stale": 0,
        # 第八期阶段3:开场加强诊断(资源名/池只进 summary/产物,不进监控事件)
        "opening_boost_requested": bool(opening_boost),
        "opening_boost_applied": False,
        "opening_boost_not_applicable": False,
        "opening_transition_pool": None,
        "opening_transition_name": None,
    }
    if n_boundaries == 0:
        warnings.append(f"[{tag}] 只有 {len(segments)} 个视频段,无剪辑边界,转场 not_applicable")

    working_semantics = list(semantics)
    # 第八期阶段3:【开场】正向要求 → 边界 0 强制 whip。按真实边界数生效
    # (P1-03):语义列表为空/偏短先补 None 占位,再覆盖边界 0 —— 不得因
    # 语义计划为空而跳过(否则合法草稿静默漏执行用户要求)。
    opening_boost_effective = bool(opening_boost) and n_boundaries > 0
    if opening_boost and n_boundaries == 0:
        summary["opening_boost_not_applicable"] = True
    if opening_boost_effective:
        if len(working_semantics) < n_boundaries:
            working_semantics.extend([None] * (n_boundaries - len(working_semantics)))
        working_semantics[0] = "whip"

    # all-cut 保底(核验报告 §6.7):全部边界硬切 + 交付要求转场 → 边界 0 回退 fade。
    # 开场覆盖成 whip 后边界 0 不再是硬切,保底通常不应触发。
    if (
        required_transitions
        and _all_boundaries_cut(working_semantics, template_lib.transition_semantic_map(), n_boundaries)
    ):
        working_semantics = ["fade" if i == 0 else s for i, s in enumerate(working_semantics)]
        summary["all_cut_fallback"] = True

    kept_transition_ids: set[str] = set()
    for i in range(n_boundaries):
        semantic = working_semantics[i] if i < len(working_semantics) else None
        status, meta, pool_key = _resolve_transition_meta(
            template_lib, semantic, i, job_id
        )
        if i == 0 and opening_boost_effective:
            summary["opening_transition_pool"] = pool_key
            summary["opening_transition_name"] = (
                str(meta.get("name"))
                if isinstance(meta, dict) and meta.get("name")
                else None
            )
        if status == "cut":
            summary["transitions_cut"] += 1
            continue
        if status != "ok" or meta is None:
            summary["transitions_degraded"] += 1
            warnings.append(
                f"[{tag}] boundary {i + 1} 转场资源未解析"
                f"(semantic={semantic!r}, pool={pool_key!r}),degraded 不写入"
            )
            if i == 0 and opening_boost_effective:
                warnings.append(
                    f"[{tag}] opening boost 边界 0 资源未解析(pool={pool_key!r}),"
                    "degraded —— 不记 applied"
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
        if i == 0 and opening_boost_effective:
            # 只有真实解析并写入草稿后才记 applied(P1-03/§6.3:不得假成功)
            summary["opening_boost_applied"] = True

    # 视频特效克制点缀:只挂首/尾/中间每第 3 段,按挂载序号轮换白名单
    whitelist_rows = template_lib.video_effect_candidates()
    candidates, failed_names = _resolve_video_effect_candidates(template_lib)
    summary["video_effects_failed"] = len(failed_names)
    if failed_names:
        warnings.append(
            f"[{tag}] 视频特效白名单 {failed_names!r} 未在资源库 video_effects 段解析,"
            "degraded 跳过(核验报告 §4.3 分段精确命中)"
        )
    if whitelist_rows and not candidates:
        warnings.append(f"[{tag}] 视频特效白名单全部未解析,degraded 不写入")
    if not whitelist_rows:
        summary["video_effects_empty_whitelist"] = True

    effect_positions = _effect_segment_positions(len(segments))
    kept_vfx_ids: set[str] = set()
    mount_seq = 0
    for k, seg in enumerate(segments):  # k 为 0-based 段索引
        if k not in effect_positions or not candidates:
            continue
        meta, value = candidates[mount_seq % len(candidates)]
        mount_seq += 1
        material_id = f"vfx-{k + 1}"
        material = build_native_video_effect_material(
            material_id=material_id, meta=meta, value=value
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


def inject_fx(state: WorkflowState, config: Optional[RunnableConfig] = None) -> dict:
    """读取草稿 → 消费转场计划 → 在剪辑边界原生绑定转场/特效 → 原子写回。

    K2=B(2026-10):对**中文主草稿 + 英文分支草稿**各注入一次(见 ``nodes/_dual_draft.py``)。
    两份草稿在同一节点内顺序处理,不跨超步,返回值契约不变(整份 state)。

    轮换 seed 用 ``job_id``,经公共 ``jy_common.job_identity.resolve_job_id``
    解析(LangGraph config.thread_id 权威 → state.session_id 兼容旧 checkpoint →
    剥 ``AutoVideo_`` 前缀的中文草稿目录名兜底)—— 对 zh/en 完全相同,保证双语
    草稿逐边界转场名一致(核验报告 §4.9;与 node_10/10b 字幕动画同一套解析规则,
    2026-11-18 缺陷修复收敛)。
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

    # targets 非空 ⇒ draft_path 必存在 ⇒ resolver 不会触发 ValueError;
    # 与 node_10/10b 共用同一套任务 ID 解析(config.thread_id 权威)
    job_id = resolve_job_id(state, config)

    required_transitions = bool(resolve_required_features(state).get("transitions"))

    semantics = _boundary_semantics_plan(state)
    # 第八期阶段3:【开场】标记解析(设计文档 §6.3;fail-closed,未提供 → False)。
    # 状态标签只含固定短语,不含用户文本(「提示词不进监控事件」契约)。
    opening_boost = opening_boost_requested(
        parse_user_directives(extract_user_request(state))
    )
    if opening_boost:
        status_log.append("node_09_inject_fx_opening_boost_requested")
    en_file = Path(state["draft_dir_en_branch"]) / "draft_content.json" if state.get("draft_dir_en_branch") else None

    for tag, draft_file in targets:
        draft = load_draft(draft_file)
        warnings, summary = _inject_fx_into(
            draft,
            template_lib,
            semantics,
            f"node_09_{tag}",
            job_id,
            required_transitions,
            opening_boost=opening_boost,
        )
        error_log.extend(warnings)
        if summary.get("not_applicable"):
            status_log.append(f"node_09_inject_fx_{tag}_not_applicable")
        if summary.get("all_cut_fallback"):
            status_log.append(f"node_09_inject_fx_{tag}_all_cut_fallback")
        if summary.get("video_effects_empty_whitelist"):
            status_log.append(f"node_09_inject_fx_{tag}_vfx_whitelist_empty")
        # 第八期阶段3:开场加强结果标签(三态,§6.3 —— requested 已在上方记过)
        if summary.get("opening_boost_applied"):
            status_log.append(f"node_09_inject_fx_{tag}_opening_boost_applied")
        elif summary.get("opening_boost_not_applicable"):
            status_log.append(f"node_09_inject_fx_{tag}_opening_boost_not_applicable")
        elif opening_boost:
            status_log.append(f"node_09_inject_fx_{tag}_opening_boost_degraded")
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

    return {**state, "status_log": status_log, "error_log": error_log}
