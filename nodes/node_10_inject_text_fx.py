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

默认动画:``config.TEXT_FX_INTRO_ANIMATION``(缺省"新年打字机",库内实证存在的
VIP 资源;旧默认列表四个名字全不在库内)未命中时回落库内第一个 VIP intro;
资源库缺失 → 动画降级不写入(不伪装成功,degraded 计数)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config import TEXT_FX_INTRO_ANIMATION
from jy_common.native_fx import OUR_TEXT_ANIMATION_PREFIX, build_native_text_animation_material
from jy_common.template_library import TemplateLibrary, load_resource_libraries
from nodes._draft_io import apply_and_write, find_text_segment_by_material_id, load_draft
from state import WorkflowState


#: 我方字幕素材 id 前缀(node_08 / node_08b 产出)
_OUR_TEXT_PREFIXES = ("text-zh-", "text-en-")
#: 旧默认动画名(保留为兜底顺序的一部分;经实证全不在 VIP 库内)
_LEGACY_INTRO_NAMES = ("居中打字机", "渐次出现", "渐显", "打字机 II")


def _is_our_text(text_obj: dict) -> bool:
    text_id = str(text_obj.get("id") or "")
    return any(text_id.startswith(p) for p in _OUR_TEXT_PREFIXES)


def _pick_intro_animation(template_lib: TemplateLibrary) -> dict | None:
    """默认入场动画:config 名 → 旧默认名兜底 → 库内第一个 VIP intro。"""
    if template_lib.is_empty or not template_lib.has_text_lib:
        return None
    for name in (TEXT_FX_INTRO_ANIMATION, *_LEGACY_INTRO_NAMES):
        anim = template_lib.pick_text_animation("intro", name)
        if anim is not None:
            return anim
    intro_list = template_lib.list_text_animations("intro", vip_only=True)
    if not intro_list:
        return None
    return template_lib.pick_text_animation("intro", intro_list[0])


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
) -> tuple[list[str], dict]:
    """为我方字幕写原生入场动画(就地改 ``draft``),返回 (告警, 摘要)。"""
    warnings: list[str] = []
    anim_meta = _pick_intro_animation(template_lib)

    summary: dict[str, Any] = {
        "texts_scanned": 0,
        "animations_written": 0,
        "degraded": 0,
        "no_text_segment": 0,
        "cleaned_stale": 0,
    }
    if anim_meta is None:
        warnings.append(f"[{tag}] 无可用入场动画资源,动画降级不写入")

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

    summary["cleaned_stale"] = _cleanup_stale_animations(draft, kept_ids)
    return warnings, summary


def apply_text_fx(
    draft_path: Path,
    status_log: list[str] | None,
    *,
    node_tag: str,
    done_tag: str,
) -> tuple[list[str], dict]:
    """读取草稿 → 为我方字幕写原生入场动画 → 原子写回。

    中文主线(``inject_text_fx``)与英文分支(``inject_text_fx_en``)共用的核心。

    Returns:
        (新的 status_log 列表, text_fx 摘要 dict)。
    """
    draft = load_draft(draft_path)
    template_lib = load_resource_libraries(
        fx_template="templates/fx_template.json",
        fx_resource="templates/fx_resource_library.json",
        text_resource="templates/text_resource_library.json",
    )

    warnings, summary = _apply_text_animations(draft, template_lib, node_tag)

    log = list(status_log or [])
    log.extend(warnings)
    log.extend(apply_and_write(draft_path, draft, node_tag, done_tag=done_tag))
    return log, summary


def inject_text_fx(state: WorkflowState) -> dict:
    """中文主线节点 10:对中文草稿(``state["draft_path"]``)的字幕写原生入场动画。

    返回整份 state + 新 status_log + ``text_fx_summary``(zh 条目)。
    """
    raw_draft_path = state.get("draft_path")
    if not raw_draft_path:
        # node_05 早退时草稿不存在(D7,2026-10-20 修):降级跳过,别抛 TypeError
        state.setdefault("error_log", []).append(
            "[node_10] state.draft_path 缺失,跳过字幕动画注入"
        )
        status_log = list(state.get("status_log", []) or [])
        status_log.append("node_10_inject_text_fx_skipped")
        return {**state, "status_log": status_log}
    log, summary = apply_text_fx(
        Path(raw_draft_path),
        state.get("status_log"),
        node_tag="node_10",
        done_tag="node_10_inject_text_fx_done",
    )
    return {**state, "status_log": log, "text_fx_summary": {"zh": summary}}


def inject_text_fx_en(state: WorkflowState) -> dict:
    """英文分支节点 10b:对英文分支草稿的字幕写原生入场动画(与中文共用同一核心)。

    - 目标草稿:``state["draft_dir_en_branch"]/draft_content.json``。
    - **只返回 delta**:与中文主线节点同一超步并行,不能返回整份 state
      (摘要走 status_log 标签,避免新增同超步键冲突)。
    - done_tag 用独立名字,避免被 status_log 的去重 reducer 与中文 node_10 合并成一条。
    """
    draft_dir_raw = state.get("draft_dir_en_branch")
    if not draft_dir_raw:
        return {"error_log": ["[node_10b] draft_dir_en_branch 缺失,跳过英文花字"]}
    draft_file = Path(draft_dir_raw) / "draft_content.json"
    if not draft_file.exists():
        return {"error_log": [f"[node_10b] 英文分支草稿不存在,跳过: {draft_file}"]}
    log, summary = apply_text_fx(
        draft_file,
        [],
        node_tag="node_10b",
        done_tag="node_10b_inject_text_fx_en_done",
    )
    return {
        "status_log": [
            *log,
            f"node_10b_text_animations_written:{summary.get('animations_written', 0)}",
        ],
    }
