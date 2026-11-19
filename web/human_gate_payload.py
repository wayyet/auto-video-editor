"""人工关卡展示载荷统一构造(2026-11-06 人工关卡与剪映草稿导入 阶段 2)。

计划 §8 阶段 2:创建台等待卡片要显示**真实 payload**——具体关卡名称、节点、
操作指引、草稿路径、issues、预览与证据 —— 而不是通用"等待人工"。

数据来源:
- interrupt payload(``scripts.resume_workflow.describe_interrupts`` 的归一形态):
  ①/② 带 ``draft_path`` / ``instructions``;
- checkpoint state values:``draft_path`` / ``draft_dir_en_branch`` 等不进
  payload 的展示字段。(2026-11-15 第六期:关卡③删除,preview/evidence/issues
  三类 QC 展示字段随 16a qc 链一并下线,本模块不再输出。)

⚠️ 契约纪律:payload 字段名不能改(nodes/_checkpoint.py 的取值匹配依赖
``checkpoint`` / ``legacy_id``);本模块只**读**payload,不回写。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: 每道关卡的展示元数据(标题 / 节点 / 专属操作提示)。
#: checkpoint 键与 interrupt payload 逐字一致(①/②)。
GATE_META: dict[str, dict[str, str]] = {
    "①": {
        "title": "① QC 降级复核",
        "node": "node_06_human_reorder",
        "action_hint": (
            "assembly QC 修复重试后仍未达标(非结构性降级);请打开 QC 报告与"
            "预览确认问题,可接受则回创建台放行(恢复值 true),需要终止则输入"
            "恢复值 reject。放行后工作流继续 node_07 时长护栏。"
        ),
    },
    "②": {
        "title": "② 添加 BGM",
        "node": "node_12_human_add_bgm",
        "action_hint": (
            "只需在中文主草稿添加一次 BGM;确认恢复后工作流会自动把音轨镜像到"
            "英文分支草稿(人工不必在两份草稿里重复添加)。"
        ),
    },
}

#: 未知 checkpoint 的兜底元数据(payload 契约字段被改动时 UI 不至于全空)
_FALLBACK_META = {
    "title": "人工关卡",
    "node": "",
    "action_hint": "请按操作指引完成剪映编辑,保存并关闭剪映后确认恢复。",
}


def _as_path_str(raw: Any) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    return str(raw)


def _dir_of(draft_file: str | None) -> str | None:
    """草稿文件路径 → 其所在目录(打开草稿目录用)。"""
    if not draft_file:
        return None
    p = Path(draft_file)
    return str(p.parent if p.suffix == ".json" else p)


def build_gate_view(item: dict[str, Any], state_values: dict[str, Any]) -> dict[str, Any]:
    """把一个挂起 interrupt 归一成 UI 展示形态(纯函数;不抛异常)。

    Args:
        item: ``describe_interrupts`` 形态 {id, checkpoint, legacy_id, step,
            instructions, payload}。
        state_values: checkpoint state values(供 payload 缺路径字段时兜底)。
    """
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    cp = str(item.get("checkpoint") or "")
    meta = GATE_META.get(cp, _FALLBACK_META)

    draft_paths: list[str] = []
    if _as_path_str(payload.get("draft_path")):
        draft_paths.append(str(payload["draft_path"]))
    if _as_path_str(payload.get("draft_dir")):
        draft_paths.append(str(payload["draft_dir"]))
    # state 兜底(payload 缺 draft_path 的旧 checkpoint)
    if not draft_paths:
        for key in ("draft_path", "draft_dir_en_branch"):
            v = _as_path_str(state_values.get(key))
            if v:
                draft_paths.append(v)

    gate: dict[str, Any] = {
        "checkpoint": cp or None,
        "legacy_id": item.get("legacy_id"),
        "step": item.get("step"),
        "interrupt_id": item.get("id"),
        "node": meta.get("node") or "",
        "title": meta.get("title") or _FALLBACK_META["title"],
        # payload 缺 instructions 键 → 用 payload.reason 兜底(历史 ③ 契约,
        # 保留为通用兜底;未知 checkpoint 由 _FALLBACK_META 接住)
        "instructions": item.get("instructions") or payload.get("reason") or "",
        "action_hint": meta.get("action_hint") or _FALLBACK_META["action_hint"],
        "draft_paths": draft_paths,
        "draft_dirs": sorted({_dir_of(p) for p in draft_paths if _dir_of(p)}),
    }

    # P1-7(2026-11-15):assembly 报告/预览/QC 状态 —— 关卡① 挂起时即可见。
    # payload 优先(① 的 extra_payload 携带),旧 checkpoint 缺字段回落 state
    # 值;① 之外关卡(②)payload 无这些键 → 走 state 兜底(通常也缺 → None,
    # UI 按缺省隐藏入口)。恢复旧的"QC 报告可从恢复面板打开"能力。
    assembly_report_path = _as_path_str(payload.get("assembly_report_path")) or _as_path_str(
        state_values.get("assembly_report_path")
    )
    assembly_preview_path = _as_path_str(payload.get("assembly_preview_path")) or _as_path_str(
        state_values.get("assembly_preview_path")
    )
    assembly_qc_status = _as_path_str(payload.get("assembly_qc_status")) or _as_path_str(
        state_values.get("assembly_qc_status")
    )
    gate["assembly_report_path"] = assembly_report_path
    gate["assembly_preview_path"] = assembly_preview_path
    gate["assembly_qc_status"] = assembly_qc_status
    warnings = payload.get("assembly_qc_warnings")
    gate["assembly_qc_warnings"] = [
        str(w) for w in warnings if isinstance(w, (str, int, float)) and str(w).strip()
    ] if isinstance(warnings, list) else []
    # assembly 产物目录(受控 open 端点用;report.md/preview.mp4 都是文件,
    # 恒取父目录 —— 与 _dir_of 的 .json 特判无关)
    artifact_dirs: list[str] = []
    for p in (assembly_report_path, assembly_preview_path):
        if not p:
            continue
        try:
            artifact_dirs.append(str(Path(p).parent))
        except OSError:  # pragma: no cover — 非法路径直接忽略
            continue
    gate["assembly_artifact_dirs"] = sorted(set(artifact_dirs))
    return gate


def build_gate_views(
    interrupts: list[dict[str, Any]], state_values: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """批量构造(顺序保持 snapshot.interrupts 原序,UI 单挂起自动选中依赖它)。"""
    values = state_values or {}
    return [build_gate_view(item, values) for item in (interrupts or [])]


#: 创建台恢复面板的全局提示(计划 §8 阶段 2:恢复值固定默认 JSON true,
#: 页面必须说明"该操作只恢复工作流,不会自动完成剪辑"。2026-11-20 关卡①
#: 改条件挂起后,关卡动作不再默认是"剪映编辑",改为按各关卡指引处理)
COMMON_RESUME_NOTE = (
    "确认恢复只负责继续工作流;请先按当前关卡的操作指引完成处理"
    "(如需在剪映中编辑,先保存并关闭剪映),再点击恢复。放行值保持 true 即可。"
)


__all__ = [
    "COMMON_RESUME_NOTE",
    "GATE_META",
    "build_gate_view",
    "build_gate_views",
]
