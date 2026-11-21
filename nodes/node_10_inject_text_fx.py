"""节点 10:inject_text_fx — 字幕文本装饰**构建计划**发射(对应原文档 4.5 节)。

2026-11-22 SDK 链转正(六段 legacy 链退役):本节点不再直接改写草稿,只做
选取决策并把装饰计划写入 DraftBuildSpec.text_decorations;node_11 编译时经
``TextSegment.add_animation`` / ``add_effect`` 落草稿(SDK in/out/loop 语义,
先入/出场后循环)。字幕对象来源 = spec sidecar 的 ``subtitles`` 段(node_08/
08b 发射,subtitle_id 即 ``text-zh-``/``text-en-`` 口径)—— legacy 链的
``materials.texts`` 扫描随原生文本轨写入一并退役。

选取逻辑保持既有语义(2026-11-18 池化 + 2026-11-19 缓存守卫):

1. **默认动画池化**:``config.TEXT_FX_INTRO_POOL_MODE`` = auto(缺省)时,
   每任务从 intro 段全部 VIP 入场动画按 ``sha256(job_id|"text_intro_pool")``
   确定性选 1 个 —— 同一支视频所有字幕同一动画,zh/en 双稿一致;= fixed 时走
   ``config.TEXT_FX_INTRO_ANIMATION`` + 旧默认名兜底。
2. **缓存可用性守卫**(动画丢失根因修复):选取只落在「verified(GUI 实证)
   + 缓存目录存在」的条目上;seed 命中不可用条目按池序向后回退,全不可用则
   降级不进 spec —— 宁可没有动画,不出丢失徽章。
3. **花字**(nodes/_text_flower):三条件守卫(verified + Cache/artistEffect
   目录 + manifest sha256)选取;SDK 编译侧另有三重门禁(双 ID 一致 +
   cache_verified + 交叉表 supported)。
4. **非破坏降级**:本轮零选取 → 不动 spec 既有装饰段(与 legacy 同语义)。

失败语义(SDK 链无 legacy 兜底,§13.2 禁静默缺段):模板库缺失/spec 读取
异常/装饰 spec 写入失败 → 抛异常使任务 fail-closed;资源级降级(无可用动画/
花字)不阻断。

job_id 解析(2026-11-18 缺陷修复):两个入口统一走公共层
``jy_common.job_identity.resolve_job_id`` —— LangGraph config 的
``configurable.thread_id`` 为权威源;解析失败显式降级跳过,禁止静默退化。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

from config import (
    JIANYING_CACHE_ROOT,
    TEXT_FLOWER_ENABLED,
    TEXT_FX_INTRO_ANIMATION,
    TEXT_FX_INTRO_POOL_MODE,
)
from jy_common.job_identity import resolve_job_id
from jy_common.native_fx import text_animation_cache_available
from jy_common.template_library import TemplateLibrary, load_resource_libraries
from langchain_core.runnables import RunnableConfig
from nodes._text_flower import select_text_flower
from state import WorkflowState


#: 旧默认动画名(保留为兜底顺序的一部分;经实证全不在库内)
_LEGACY_INTRO_NAMES = ("居中打字机", "渐次出现", "渐显", "打字机 II")
#: 原生花字池文件(2026-11-21;缺失/为空 = 空花字库,花字降级)
FLOWER_LIBRARY_PATH = "templates/text_flower_resource_library.json"


def _intro_usable(anim_meta: dict) -> bool:
    """池条目可用守卫(2026-11-19 动画丢失根因修复)。

    双条件缺一不可:``verified=True``(只能由剪映 GUI 实证授予,元数据扫描
    不携带)且本地缓存目录 ``Cache/effect/<effect_id>/<md5>`` 存在。目录存在
    不代表可渲染(坏包占目录,「螺旋上升」实证),故以 verified 为主判据、
    缓存存在为辅(防缓存被清理后的陈旧 verified)。
    """
    if not anim_meta.get("verified"):
        return False
    return text_animation_cache_available(anim_meta, JIANYING_CACHE_ROOT)


def _pick_intro_animation(
    template_lib: TemplateLibrary, job_id: str = ""
) -> tuple[dict | None, dict[str, Any]]:
    """入场动画选取(池化,2026-11-18;缓存可用性守卫,2026-11-19)。

    - auto(缺省):intro 段全部 VIP 动画按 ``sha256(job_id|"text_intro_pool")``
      定位全池起点,从该位置起**按池序轮转**,选取第一个可用条目(verified
      且本地缓存存在)—— 同任务同结果,zh/en 一致,跨任务多样化。
    - fixed:config 名 → 旧默认名兜底(均须过守卫)→ 可用子集池序第一个。
    - 无可用条目 → ``(None, audit)``,调用方降级(不进 spec)。

    返回 ``(选中条目或 None, 选取审计)``;审计含池大小/可用数/seed 起点/
    最终选中 index/回退偏移/缓存根 —— 事件流与测试均可复算选择结果(审查 P2-01)。
    """
    audit: dict[str, Any] = {
        "mode": "fixed" if TEXT_FX_INTRO_POOL_MODE == "fixed" else "auto",
        "cache_root": JIANYING_CACHE_ROOT,
        "pool_size": 0,
        "usable_count": 0,
        "seed_start_index": None,
        "selected_index": None,
        "fallback_offset": None,
    }
    if template_lib.is_empty or not template_lib.has_text_lib:
        return None, audit
    intro_rows = template_lib.list_text_animation_rows("intro", vip_only=True)
    audit["pool_size"] = len(intro_rows)
    if not intro_rows:
        return None, audit
    usable_ids = {id(row) for row in intro_rows if _intro_usable(row)}
    audit["usable_count"] = len(usable_ids)
    if not usable_ids:
        return None, audit
    if TEXT_FX_INTRO_POOL_MODE != "fixed":
        seed_input = f"{job_id}|text_intro_pool"
        seed = int(hashlib.sha256(seed_input.encode("utf-8")).hexdigest()[:8], 16)
        start = seed % len(intro_rows)
        audit["seed_start_index"] = start
        for offset in range(len(intro_rows)):
            candidate = intro_rows[(start + offset) % len(intro_rows)]
            if id(candidate) in usable_ids:
                audit["selected_index"] = (start + offset) % len(intro_rows)
                audit["fallback_offset"] = offset
                return candidate, audit
        return None, audit
    for name in (TEXT_FX_INTRO_ANIMATION, *_LEGACY_INTRO_NAMES):
        anim = template_lib.pick_text_animation("intro", name)
        if anim is not None and _intro_usable(anim):
            audit["selected_index"] = next(
                (
                    i
                    for i, r in enumerate(intro_rows)
                    if r.get("name") == anim.get("name")
                ),
                None,
            )
            return anim, audit
    # fixed 兜底:可用子集按池序第一个(不再无条件回退 rows[0] —— 它可能不可用)
    for idx, row in enumerate(intro_rows):
        if id(row) in usable_ids:
            audit["selected_index"] = idx
            return row, audit
    return None, audit


def _select_text_animations(
    template_lib: TemplateLibrary,
    tag: str,
    job_id: str = "",
) -> tuple[list[str], dict]:
    """入场动画选取(**纯选取,不改草稿**),返回 (告警, 摘要)。"""
    warnings: list[str] = []
    anim_meta, selection_audit = _pick_intro_animation(template_lib, job_id)

    summary: dict[str, Any] = {
        "animations_selected": 0,
        "degraded": 0,
        "selection_audit": selection_audit,
        "selected": None,
    }
    if anim_meta is None:
        summary["degraded"] = 1
        warnings.append(f"[{tag}] 无可用入场动画资源,动画降级不进 spec")
    else:
        # 选中信息只进摘要与 status_log 标签(text_fx_summary 已在死键治理中移除)
        summary["animations_selected"] = 1
        summary["selected"] = {
            "name": str(anim_meta.get("name") or ""),
            "effect_id": str(anim_meta.get("effect_id") or ""),
            "resource_id": str(anim_meta.get("resource_id") or anim_meta.get("effect_id") or ""),
            "duration_s": float(anim_meta.get("duration_s") or 0.0),
        }
    return warnings, summary


def apply_text_fx(
    draft_dir: Path,
    status_log: list[str] | None,
    *,
    node_tag: str,
    done_tag: str,
    job_id: str = "",
) -> tuple[list[str], dict]:
    """选取动画/花字 → 发射 TextDecorationSpec 到 DraftBuildSpec(**不改草稿**)。

    中文主线(``inject_text_fx``)与英文分支(``inject_text_fx_en``)共用的核心。
    ``job_id`` 仅用于 auto 池模式的确定性轮换 seed(zh/en 传同一值保证动画与
    花字一致)。字幕对象来源 = spec sidecar ``subtitles`` 段(node_08/08b 已发射)。
    花字逻辑见 ``nodes._text_flower``(摘要随返回值 ``text_flower`` 子键携带)。

    Returns:
        (新的 status_log 列表, 摘要 dict;花字摘要在 ``text_flower`` 键)。
    """
    template_lib = load_resource_libraries(
        fx_template="templates/fx_template.json",
        fx_resource="templates/fx_resource_library.json",
        text_resource="templates/text_resource_library.json",
        text_flower_resource=FLOWER_LIBRARY_PATH,
    )

    warnings, summary = _select_text_animations(template_lib, node_tag, job_id)
    flower_warnings, flower_summary = select_text_flower(
        template_lib,
        job_id=job_id,
        enabled=TEXT_FLOWER_ENABLED,
        tag=node_tag,
    )

    log = list(status_log or [])
    selected = summary.get("selected") or {}
    if selected.get("name"):
        # 审计标签(P2-01):池大小/可用数/seed 起点/选中 index/回退偏移齐全,
        # 不打开草稿也能复算并解释选择结果(降级时走 warnings)
        audit = summary.get("selection_audit") or {}
        log.append(
            f"{node_tag}_text_anim_selected:{selected['name']}@{selected.get('effect_id')}"
            f"(pool={audit.get('pool_size')},usable={audit.get('usable_count')},"
            f"start={audit.get('seed_start_index')},pick={audit.get('selected_index')},"
            f"offset={audit.get('fallback_offset')},mode={audit.get('mode')})"
        )
    flower_selected = flower_summary.get("selected") or {}
    if flower_selected.get("name"):
        # 花字审计标签(同款可复算口径;不落本机缓存绝对路径,审查 §4.7)
        audit = flower_summary.get("selection_audit") or {}
        status_counts = audit.get("status_counts") or {}
        log.append(
            f"{node_tag}_text_flower_selected:{flower_selected['name']}"
            f"@{flower_selected['effect_id']}"
            f"(pool={audit.get('pool_size')},usable={status_counts.get('ready', 0)},"
            f"start={audit.get('seed_start_index')},pick={audit.get('selected_index')},"
            f"offset={audit.get('fallback_offset')},mode={audit.get('mode')})"
        )
    log.extend(warnings)
    log.extend(flower_warnings)
    # 文本装饰计划发射(SDK 编译数据源;失败 fail-closed,§13.2 禁静默缺段)
    _emit_text_decoration_spec(draft_dir, summary, flower_summary, log, node_tag)
    log.append(done_tag)
    return log, {**summary, "text_flower": flower_summary}


def _emit_text_decoration_spec(
    draft_dir: Path,
    animation_summary: dict,
    flower_summary: dict,
    log: list[str],
    node_tag: str,
) -> None:
    """文本装饰计划 → DraftBuildSpec.text_decorations(SDK 编译数据源)。

    数据源:动画取 ``animation_summary["selected"]``,花字取
    ``flower_summary["selected"]``;逐字幕展开到 spec ``subtitles`` 段的全部
    字幕(subtitle_id 即 ``text-zh-``/``text-en-`` 口径)。失败直接抛出
    (SDK 链无 legacy 兜底,§13.2 禁静默缺段)。
    """
    from jianying_sdk.spec import ResourceRef, TextAnimationEntry, TextDecorationSpec
    from nodes._draft_build_spec import load_build_spec, replace_spec_sections

    anim_meta = animation_summary.get("selected") or {}
    flower_meta = flower_summary.get("selected") or {}
    has_animation = bool(anim_meta.get("effect_id"))
    flower_selected = (
        str(flower_summary.get("status") or "") == "selected"
        and bool(flower_meta.get("effect_id"))
    )
    if not has_animation and not flower_selected:
        return  # 本轮零选取:非破坏,spec 既有装饰段保持原样

    spec = load_build_spec(draft_dir)
    subtitle_ids = [s.subtitle_id for s in (spec.subtitles or [])] if spec else []

    animations = None
    if has_animation:
        duration_s = float(anim_meta.get("duration_s") or 0.0)
        animations = [
            TextAnimationEntry(
                kind="in",
                effect_id=str(anim_meta.get("effect_id") or ""),
                resource_id=str(anim_meta.get("resource_id") or anim_meta.get("effect_id") or ""),
                name=str(anim_meta.get("name") or ""),
                duration_us=(int(round(duration_s * 1_000_000)) or None),
            )
        ]
    text_effect = None
    if flower_selected:
        text_effect = ResourceRef(
            effect_id=str(flower_meta.get("effect_id") or ""),
            resource_id=str(flower_meta.get("resource_id") or flower_meta.get("effect_id") or ""),
            name=str(flower_meta.get("name") or ""),
            verification="cache_verified",
        )

    specs: list[TextDecorationSpec] = [
        TextDecorationSpec(
            subtitle_id=sid,
            text_effect=text_effect,
            animations=list(animations or []),
        )
        for sid in subtitle_ids
    ]
    replace_spec_sections(draft_dir, text_decorations=specs)
    log.append(
        f"[{node_tag}] 文本装饰计划已入 spec:{len(specs)} 条字幕"
        f"(动画={1 if has_animation else 0},花字={'有' if flower_selected else '无'})"
    )


def inject_text_fx(state: WorkflowState, config: Optional[RunnableConfig] = None) -> dict:
    """中文主线节点 10:为中文草稿(``state["draft_path"]``)发射字幕装饰计划。

    返回 **增量 patch**(仅 status_log / error_log)。2026-11-20 修正:此前
    ``{**state}`` 全量回传会把通道里的既有值原样重发 —— 本节点与
    ``fork_draft_for_english_branch`` 同一超步并发执行(fork 唯一写
    ``draft_dir_en_branch``),当线程复用旧 checkpoint(该键残留旧值)时,
    全量回传 = 同一超步对该键二次写入 → LangGraph ``InvalidUpdateError`` 崩溃。
    改为增量后语义不变(patch 合并结果一致)。

    ``config`` 由 LangGraph 注入(``Optional[RunnableConfig]`` 在其节点签名
    白名单内);job_id 经公共 ``resolve_job_id`` 解析。draft_path 已前置校验,
    末级兜底必命中中文草稿目录名,``ValueError`` 分支仅为防御性显式降级。
    """
    raw_draft_path = state.get("draft_path")
    if not raw_draft_path:
        # node_05 早退时草稿不存在(D7,2026-10-20 修):降级跳过,别抛 TypeError
        status_log = list(state.get("status_log", []) or [])
        status_log.append("node_10_inject_text_fx_skipped")
        return {
            "error_log": ["[node_10] state.draft_path 缺失,跳过字幕动画注入"],
            "status_log": status_log,
        }
    # job_id 与 node_09/node_10b 同源解析(公共 resolver)—— zh/en 池轮换一致
    try:
        job_id = resolve_job_id(state, config)
    except ValueError as exc:
        status_log = list(state.get("status_log", []) or [])
        status_log.append("node_10_inject_text_fx_skipped")
        return {
            "error_log": [f"[node_10] {exc},跳过字幕动画注入"],
            "status_log": status_log,
        }
    log, _ = apply_text_fx(
        Path(raw_draft_path).parent,
        state.get("status_log"),
        node_tag="node_10",
        done_tag="node_10_inject_text_fx_done",
        job_id=job_id,
    )
    return {"status_log": log}


def inject_text_fx_en(state: WorkflowState, config: Optional[RunnableConfig] = None) -> dict:
    """英文分支节点 10b:为英文分支草稿发射字幕装饰计划(与中文共用同一核心)。

    - 目标:``state["draft_dir_en_branch"]`` 草稿目录的 spec sidecar。
    - **只返回 delta**:与中文主线节点同一超步并行,不能返回整份 state。
    - done_tag 用独立名字,避免被 status_log 的去重 reducer 与中文 node_10 合并成一条。
    - job_id 与中文主线 node_10 同源(公共 ``resolve_job_id``)—— **禁止**用英文
      草稿目录的上级名兜底;四级全空时显式降级跳过注入。
    """
    draft_dir_raw = state.get("draft_dir_en_branch")
    if not draft_dir_raw:
        return {"error_log": ["[node_10b] draft_dir_en_branch 缺失,跳过英文字幕动画与花字"]}
    draft_file = Path(draft_dir_raw) / "draft_content.json"
    if not draft_file.exists():
        return {"error_log": [f"[node_10b] 英文分支草稿不存在,跳过: {draft_file}"]}
    try:
        job_id = resolve_job_id(state, config)
    except ValueError as exc:
        return {
            "error_log": [f"[node_10b] {exc},跳过英文字幕动画注入(降级,不伪装成功)"],
            "status_log": ["node_10b_inject_text_fx_en_skipped"],
        }
    log, summary = apply_text_fx(
        Path(draft_dir_raw),
        [],
        node_tag="node_10b",
        done_tag="node_10b_inject_text_fx_en_done",
        job_id=job_id,
    )
    return {
        "status_log": [
            *log,
            f"node_10b_text_animations_selected:{summary.get('animations_selected', 0)}",
        ],
    }
