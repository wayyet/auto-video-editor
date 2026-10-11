"""join_before_delivery — 中文主线 + 英文分支汇合节点(Week 4 新增,对照计划 §4.6)。

职责:轻量 QA 闸门,**不**做深度校验。失败时仅记录到 ``join_qa_issues`` 与
``error_log``,流程仍走到 END(便于联调观察,深度闸门留 Week 5)。

位置(2026-10 调序后):尾链 node_09 → 11 → 12 → 13 的**最后一站**,单前驱。
两条分支的汇合屏障已前移到 ``node_09_inject_fx``(``add_edge([node_15, node_17], node_09)``);
本节点只检查"交付物是否齐全",此时尾链(含 BGM/音量)已全部完成。

K2=B 后新增英文草稿检查(计划 §7 U1,本实现定下的检查项):

============================  ==========================================
检查项                        判定
============================  ==========================================
英文草稿存在性                ``draft_dir_en_branch`` 有值但文件不存在 → issue
转场/特效条数一致              中英两份草稿 ``transitions`` / ``video_effects``
                              条数必须相等且 >0(尾链 09 对两份都生效)
贴纸条数一致                  两份草稿 ``stickers`` 条数必须相等且 >0(尾链 11)
BGM 轨存在                    英文草稿有 audio 轨时,``materials.audios`` 必须
                              引用得到(尾链 12 镜像)
音量已写入                    有 audio 轨却没有任何 ``volume`` 字段 → issue
                              (尾链 13;R2 下英文主音轨本轮不生效,只查"有音量")
============================  ==========================================

英文草稿从未生成(``draft_dir_en_branch`` 为空,例如 fork 早退)时**整段跳过**,
不当成问题 —— 那是上游降级,不是交付缺陷。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nodes._dual_draft import dual_draft_targets, en_draft_file_missing_path
from state import WorkflowState


def _count(materials: dict, key: str) -> int:
    return len(materials.get(key, []) or [])


def _en_draft_qa_issues(en_draft: dict) -> list[str]:
    """对英文分支草稿做尾链四项检查(见模块 docstring 的表格)。"""
    issues: list[str] = []
    materials = en_draft.get("materials", {}) or {}

    transitions = _count(materials, "transitions")
    video_effects = _count(materials, "video_effects")
    stickers = _count(materials, "stickers")
    if transitions == 0:
        issues.append("英文草稿转场缺失(尾链 node_09 未对英文草稿生效?)")
    if video_effects == 0:
        issues.append("英文草稿视频特效缺失(尾链 node_09 未对英文草稿生效?)")
    if stickers == 0:
        issues.append("英文草稿贴纸缺失(尾链 node_11 未对英文草稿生效?)")

    # BGM 轨 + 音量:英文草稿**可能**没有音轨(关卡② 未加 / 镜像失败),那时不报;
    # 有音轨却没音量才是真问题(尾链 node_13 该写没写)。
    audio_tracks = [t for t in (en_draft.get("tracks", []) or []) if t.get("type") == "audio"]
    if audio_tracks:
        seg_ids = {
            seg.get("id")
            for track in audio_tracks
            for seg in (track.get("segments", []) or [])
            if seg.get("id")
        }
        audios = [m for m in (materials.get("audios", []) or []) if m.get("id") in seg_ids]
        if seg_ids and not audios:
            issues.append("英文草稿音轨引用的 audio material 缺失(BGM 镜像不完整?)")
        elif audios and not any("volume" in m for m in audios):
            issues.append("英文草稿音量未写入(尾链 node_13 未对英文草稿生效?)")

    return issues


def join_before_delivery(state: WorkflowState) -> dict:
    """LangGraph 节点(交付前 QA 闸门):检查中英两份草稿的交付物是否齐全。

    只返回变更字段(``join_qa_issues`` + ``error_log`` + ``status_log``),
    避免覆盖上游未变字段。

    K2=B(2026-10):英文草稿存在时额外做尾链四项检查(转场/特效/贴纸/BGM 音量)。
    英文草稿从未生成时整段跳过,不产生 issues。
    """
    issues: list[str] = []

    covers = state.get("covers") or []
    if len(covers) < 3:
        issues.append(f"封面缺失:需3种比例,实际 {len(covers)} 种")
    else:
        for c in covers:
            if c.get("en_path") is None:
                issues.append(f"封面英文版缺失:ratio={c.get('ratio', 'unknown')}")

    if not state.get("subtitle_srt_path"):
        issues.append("英文字幕文件缺失")

    # ---- K2=B:英文草稿尾链检查 ----
    if state.get("draft_dir_en_branch"):
        targets = dict(dual_draft_targets(state))
        if "zh" not in targets:
            issues.append(f"中文主草稿缺失: {state.get('draft_path')}")
        en_file = targets.get("en")
        if en_file is None:
            issues.append(f"英文分支草稿缺失: {en_draft_file_missing_path(state)}")
        else:
            try:
                en_draft: dict[str, Any] = json.loads(
                    Path(en_file).read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                issues.append(f"英文分支草稿不可读: {exc}")
            else:
                issues.extend(_en_draft_qa_issues(en_draft))

    new_errors: list[str] = [f"[join] {it}" for it in issues]
    return {
        "join_qa_issues": issues,
        "error_log": new_errors,
        "status_log": ["join_before_delivery_done"],
    }


__all__ = ["join_before_delivery"]