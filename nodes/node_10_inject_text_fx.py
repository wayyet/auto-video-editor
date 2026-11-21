"""节点 10:inject_text_fx — 字幕入场动画原生落地(对应原文档 4.5 节)。

2026-11-09 阶段 4(效果缺失修复)重写:入场动画不再写私有 ``style.entrance_animation``
键(剪映不识别、人工保存即丢),改为原生通道 —— ``materials.material_animations``
条目(``jy_common/native_fx.build_native_text_animation_material``)+ text 轨段
``extra_material_refs`` 绑定。描边已由 node_08 写入 content JSON strokes 原生生效;
阴影/背景的原生字段留待 GUI 样本校准(本节点不再写无效的私有样式键)。

守卫(对齐阶段 1 定向替换哲学):只处理**我方**字幕素材(id 前缀
``text-zh-`` / ``text-en-``),人工在剪映里建的文本素材一律不动。
动画 id 确定性(``text-anim-{字幕 material id}``)→ 重放按 id 覆盖;重放前剥离
我方旧 refs、清理孤儿动画素材(人工动画是 uuid,不受影响)。

默认动画(2026-11-18 池化):``config.TEXT_FX_INTRO_POOL_MODE`` = auto(缺省)时,
每任务从 text_resource_library intro 段全部 VIP 入场动画按
``sha256(job_id|"text_intro_pool")`` 确定性选 1 个 —— 同一支视频所有字幕同一动画
(避免花哨),zh/en 双草稿一致(与 node_09 转场池轮换同款机制、同 seed 原则);
= fixed 时走旧行为:``config.TEXT_FX_INTRO_ANIMATION``(缺省"新年打字机",库内
实证存在的 VIP 资源;旧默认列表四个名字全不在库内)未命中时回落库内第一个 VIP
intro。资源库缺失 → 动画降级不写入(不伪装成功,degraded 计数)。

缓存可用性守卫(2026-11-19 动画丢失根因修复):池条目只是 vendor 元数据扫描,
资源须已下载到剪映本地缓存才能真正渲染(「螺旋上升」下载不完整即「动画丢失」
实证)。选取只落在「verified(GUI 实证)+ 缓存目录存在」的条目上:seed 命中
不可用条目时按池序向后回退,全不可用则降级不写 —— 宁可没有动画,不出丢失徽章。
降级是**非破坏性**的:草稿已有的我方动画素材与轨段引用原样保留(审查 P1-01),
旧动画清理只发生在「本轮已写入新动画」的替换场景。

job_id 解析(2026-11-18 缺陷修复):两个入口统一走公共层
``jy_common.job_identity.resolve_job_id`` —— LangGraph config 的
``configurable.thread_id`` 为权威源,``state.session_id`` 兼容旧 checkpoint,
末级兜底剥 ``AutoVideo_`` 前缀的中文草稿目录名;解析失败显式降级
(记 error_log 并跳过注入),禁止静默退化为共享根目录名(如
``com.lveditor.draft``)或固定伪 ID(此前 en 兜底 ``Path(draft_dir).parent.name``
正是该缺陷,zh/en 动画因此分裂)。
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
from jy_common.native_fx import (
    OUR_TEXT_ANIMATION_PREFIX,
    build_native_text_animation_material,
    text_animation_cache_available,
)
from jy_common.template_library import TemplateLibrary, load_resource_libraries
from langchain_core.runnables import RunnableConfig
from nodes._draft_io import apply_and_write, find_text_segment_by_material_id, load_draft
from nodes._text_flower import apply_text_flower_effects
from state import WorkflowState


#: 我方字幕素材 id 前缀(node_08 / node_08b 产出)
_OUR_TEXT_PREFIXES = ("text-zh-", "text-en-")
#: 旧默认动画名(保留为兜底顺序的一部分;经实证全不在库内)
_LEGACY_INTRO_NAMES = ("居中打字机", "渐次出现", "渐显", "打字机 II")
#: 原生花字池文件(2026-11-21;缺失/为空 = 空花字库,花字降级不写)
FLOWER_LIBRARY_PATH = "templates/text_flower_resource_library.json"


def _is_our_text(text_obj: dict) -> bool:
    text_id = str(text_obj.get("id") or "")
    return any(text_id.startswith(p) for p in _OUR_TEXT_PREFIXES)


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
      且本地缓存存在)—— 同任务同结果,zh/en 一致,跨任务多样化;seed 命中
      不可用条目时向后回退,不产生「动画丢失」草稿。
    - fixed:config 名 → 旧默认名兜底(均须过守卫)→ 可用子集池序第一个。
    - 无可用条目 → ``(None, audit)``,调用方降级不写动画(非破坏,审查 P1-01)。

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


def _cleanup_stale_animations(draft: dict, kept_ids: set[str]) -> int:
    """清理我方旧动画(未续用的 text-anim-* 素材与 refs),返回清理数。"""
    materials = draft.setdefault("materials", {})
    anim_bucket = materials.setdefault("material_animations", [])
    removed = 0
    kept: list[dict] = []
    for item in anim_bucket:
        item_id = str(item.get("id") or "")
        if item_id.startswith(OUR_TEXT_ANIMATION_PREFIX) and item_id not in kept_ids:
            removed += 1
            continue
        kept.append(item)
    anim_bucket[:] = kept

    for track in draft.get("tracks", []) or []:
        if track.get("type") != "text":
            continue
        for seg in track.get("segments", []) or []:
            refs = seg.get("extra_material_refs")
            if not isinstance(refs, list):
                continue
            seg["extra_material_refs"] = [
                r for r in refs
                if not (str(r).startswith(OUR_TEXT_ANIMATION_PREFIX) and str(r) not in kept_ids)
            ]
    return removed


def _apply_text_animations(
    draft: dict,
    template_lib: TemplateLibrary,
    tag: str,
    job_id: str = "",
) -> tuple[list[str], dict]:
    """为我方字幕写原生入场动画(就地改 ``draft``),返回 (告警, 摘要)。"""
    warnings: list[str] = []
    anim_meta, selection_audit = _pick_intro_animation(template_lib, job_id)

    summary: dict[str, Any] = {
        "texts_scanned": 0,
        "animations_written": 0,
        "degraded": 0,
        "no_text_segment": 0,
        "cleaned_stale": 0,
        "selection_audit": selection_audit,
    }
    if anim_meta is None:
        warnings.append(f"[{tag}] 无可用入场动画资源,动画降级不写入")
    else:
        # 选中信息只进摘要与 status_log 标签(text_fx_summary 已在死键治理中移除)
        # 2026-11-21 第10期阶段4:补 resource_id/duration_s(spec 发射消费)
        summary["selected"] = {
            "name": str(anim_meta.get("name") or ""),
            "effect_id": str(anim_meta.get("effect_id") or ""),
            "resource_id": str(anim_meta.get("resource_id") or anim_meta.get("effect_id") or ""),
            "duration_s": float(anim_meta.get("duration_s") or 0.0),
        }

    materials = draft.setdefault("materials", {})
    anim_bucket = materials.setdefault("material_animations", [])
    kept_ids: set[str] = set()

    for text_obj in materials.get("texts", []) or []:
        if not _is_our_text(text_obj):
            continue
        summary["texts_scanned"] += 1
        if anim_meta is None:
            summary["degraded"] += 1
            continue
        text_id = str(text_obj.get("id") or "")
        anim_material = build_native_text_animation_material(
            material_id=f"{OUR_TEXT_ANIMATION_PREFIX}{text_id}",
            anim_meta=anim_meta,
            kind="in",
        )
        # 先移除同 id 旧条目再追加(upsert 语义)
        anim_bucket[:] = [a for a in anim_bucket if a.get("id") != anim_material["id"]]
        anim_bucket.append(anim_material)
        kept_ids.add(anim_material["id"])

        seg = find_text_segment_by_material_id(draft, text_id)
        if seg is None:
            summary["no_text_segment"] += 1
            warnings.append(f"[{tag}] 字幕 {text_id} 无 text 轨段,动画素材已写但未绑定")
            continue
        refs = seg.setdefault("extra_material_refs", [])
        if anim_material["id"] not in refs:
            refs.append(anim_material["id"])
        summary["animations_written"] += 1

    # 非破坏性降级(2026-11-19 审查 P1-01):选取失败 ≠ 删除旧数据。缓存根配置
    # 错误/缓存暂时不可见/资源池异常时,若以空 kept_ids 走清理,会把草稿里上一轮
    # 已有的我方字幕动画连 refs 一起删掉 —— 与 fail-closed 目标相反。因此清理
    # 只在「本轮已选中并写入新动画」分支执行;旧动画的显式移除由单独的修复动作
    # 负责,不与普通任务重跑绑定。
    if anim_meta is not None:
        summary["cleaned_stale"] = _cleanup_stale_animations(draft, kept_ids)
    return warnings, summary


def apply_text_fx(
    draft_path: Path,
    status_log: list[str] | None,
    *,
    node_tag: str,
    done_tag: str,
    job_id: str = "",
) -> tuple[list[str], dict]:
    """读取草稿 → 为我方字幕写原生入场动画与原生花字 → 原子写回。

    中文主线(``inject_text_fx``)与英文分支(``inject_text_fx_en``)共用的核心。
    ``job_id`` 仅用于 auto 池模式的确定性轮换 seed(zh/en 传同一值保证动画与
    花字一致)。花字逻辑见 ``nodes._text_flower``(2026-11-21 接入,摘要随返回
    值 ``text_flower`` 子键携带,不覆盖动画摘要)。

    Returns:
        (新的 status_log 列表, text_fx 摘要 dict;花字摘要在 ``text_flower`` 键)。
    """
    draft = load_draft(draft_path)
    template_lib = load_resource_libraries(
        fx_template="templates/fx_template.json",
        fx_resource="templates/fx_resource_library.json",
        text_resource="templates/text_resource_library.json",
        text_flower_resource=FLOWER_LIBRARY_PATH,
    )

    warnings, summary = _apply_text_animations(draft, template_lib, node_tag, job_id)
    # 原生花字(2026-11-21):动画之后、同一份草稿、同一原子写回;摘要不覆盖
    # 动画摘要,以 "text_flower" 子键随返回值携带(审查 §4.7)
    flower_warnings, flower_summary = apply_text_flower_effects(
        draft,
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
    # 第10期阶段4:文本装饰计划同步入 spec(SDK 编译数据源;legacy 行为不变)。
    # 非破坏性降级语义与 legacy 一致:本轮未选中任何资源 → 不动 spec 既有装饰段
    # (审查 P1-01 同款,避免降级清空上一轮计划)。
    _emit_text_decoration_spec(draft, draft_path.parent, summary, flower_summary, log, node_tag)
    log.extend(apply_and_write(draft_path, draft, node_tag, done_tag=done_tag))
    return log, {**summary, "text_flower": flower_summary}


def _emit_text_decoration_spec(
    draft: dict,
    draft_dir: Path,
    animation_summary: dict,
    flower_summary: dict,
    log: list[str],
    node_tag: str,
) -> None:
    """文本装饰计划 → DraftBuildSpec.text_decorations(第10期阶段4)。

    数据源与 legacy 写入完全同源(同一份选取审计):动画取 ``animation_summary
    ["selected"]``,花字取 ``flower_summary["selected"]``;逐字幕展开到
    ``text-zh-``/``text-en-`` 我方字幕。失败降级不阻断。
    """
    try:
        from jianying_sdk.spec import ResourceRef, TextAnimationEntry, TextDecorationSpec
        from nodes._draft_build_spec import replace_spec_sections

        anim_meta = animation_summary.get("selected") or {}
        flower_meta = flower_summary.get("selected") or {}
        has_animation = bool(anim_meta.get("effect_id"))
        flower_written = (
            str(flower_summary.get("status") or "") == "written"
            and bool(flower_meta.get("effect_id"))
        )
        if not has_animation and not flower_written:
            return  # 本轮零选取:非破坏,spec 既有装饰段保持原样

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
        if flower_written:
            text_effect = ResourceRef(
                effect_id=str(flower_meta.get("effect_id") or ""),
                resource_id=str(flower_meta.get("resource_id") or flower_meta.get("effect_id") or ""),
                name=str(flower_meta.get("name") or ""),
                verification="cache_verified",
            )

        specs: list[TextDecorationSpec] = []
        for text_obj in (draft.get("materials") or {}).get("texts") or []:
            if not isinstance(text_obj, dict) or not _is_our_text(text_obj):
                continue
            specs.append(
                TextDecorationSpec(
                    subtitle_id=str(text_obj.get("id") or ""),
                    text_effect=text_effect,
                    animations=list(animations or []),
                )
            )
        replace_spec_sections(draft_dir, text_decorations=specs)
        log.append(
            f"[{node_tag}] 文本装饰计划已入 spec:{len(specs)} 条字幕"
            f"(动画={1 if has_animation else 0},花字={'有' if flower_written else '无'})"
        )
    except Exception as exc:  # noqa: BLE001 — spec 是增强路径,绝不阻断 node_10
        log.append(f"[{node_tag}] 文本装饰 spec 写入失败(不阻断): {exc!r}")


def inject_text_fx(state: WorkflowState, config: Optional[RunnableConfig] = None) -> dict:
    """中文主线节点 10:对中文草稿(``state["draft_path"]``)的字幕写原生入场动画。

    返回 **增量 patch**(仅 status_log / error_log)。2026-11-20 修正:此前
    ``{**state}`` 全量回传会把通道里的既有值原样重发 —— 本节点与
    ``fork_draft_for_english_branch`` 同一超步并发执行(fork 唯一写
    ``draft_dir_en_branch``),当线程复用旧 checkpoint(该键残留旧值)时,
    全量回传 = 同一超步对该键二次写入 → LangGraph ``InvalidUpdateError:
    Can receive only one value per step`` 崩溃(video-001 预热线程首次暴露;
    2026-11-20 关卡①条件挂起后预热线程首次自动穿过该超步)。改为增量后
    语义不变(patch 合并结果一致),并发双写源消除。

    2026-11-12 阶段5批2 移除无人消费的 ``text_fx_summary`` 审计摘要;注入
    结果以草稿内原生动画与 status_log 为准。

    ``config`` 由 LangGraph 注入(``Optional[RunnableConfig]`` 在其节点签名
    白名单内);job_id 经公共 ``resolve_job_id`` 解析(config.thread_id 权威,
    session_id 兼容旧 checkpoint)。draft_path 已前置校验,末级兜底必命中
    中文草稿目录名,``ValueError`` 分支仅为防御性显式降级。
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
        Path(raw_draft_path),
        state.get("status_log"),
        node_tag="node_10",
        done_tag="node_10_inject_text_fx_done",
        job_id=job_id,
    )
    return {"status_log": log}


def inject_text_fx_en(state: WorkflowState, config: Optional[RunnableConfig] = None) -> dict:
    """英文分支节点 10b:对英文分支草稿的字幕写原生入场动画(与中文共用同一核心)。

    - 目标草稿:``state["draft_dir_en_branch"]/draft_content.json``。
    - **只返回 delta**:与中文主线节点同一超步并行,不能返回整份 state
      (摘要走 status_log 标签,避免新增同超步键冲突)。
    - done_tag 用独立名字,避免被 status_log 的去重 reducer 与中文 node_10 合并成一条。
    - job_id 与中文主线 node_10 同源(公共 ``resolve_job_id``:config.thread_id →
      state.thread_id → state.session_id → 中文草稿目录名)—— **禁止**用英文
      草稿目录的上级名兜底(那会把草稿库根名当成任务 ID,seed 恒定、轮换失效);
      四级全空时显式降级跳过注入。
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
        draft_file,
        [],
        node_tag="node_10b",
        done_tag="node_10b_inject_text_fx_en_done",
        job_id=job_id,
    )
    return {
        "status_log": [
            *log,
            f"node_10b_text_animations_written:{summary.get('animations_written', 0)}",
        ],
    }
