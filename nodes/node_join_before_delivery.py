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

2026-11-07 阶段 4(人工关卡与剪映草稿导入)扩展:

- 尾链四项检查从"只查英文"扩到**中英两份**(尾链 09/11/13 本就对两份生效);
- 深度 QA 委托 :mod:`draft_ops.draft_integrity`(双文件一致性 / 注册元数据 /
  根索引 / 素材路径 / 引用完整性 / 关卡② 音频镜像),结构化结果写入
  ``join_qa_report``(新字段;``join_qa_issues`` 字符串契约不变,error 级
  才进 issues/error_log,warning 只进报告);
- 经 custom stream 上报 ``delivery.qa_passed`` / ``delivery.qa_issues`` 事件,
  创建台据此展示"草稿生成 / 草稿发布 / 交付 QA / MP4 导出"四状态(计划
  §8 阶段 4 任务"区分四种状态"的前三态)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from draft_ops.draft_integrity import (
    SEV_ERROR,
    build_join_qa_report,
    issue_text,
)
from nodes._dual_draft import dual_draft_targets, en_draft_file_missing_path
from state import WorkflowState

#: delivery.qa 事件里 issue 明细的条数上限(事件载荷不无限膨胀;全量在 state 里)
_EVENT_ISSUE_CAP = 20


def _count(materials: dict, key: str) -> int:
    return len(materials.get(key, []) or [])


def _tail_chain_issues(draft: dict, label: str) -> list[str]:
    """对一份草稿做尾链四项检查(转场/特效/贴纸/BGM 音量)。

    ``label`` 取 ``"英文草稿"`` / ``"中文主草稿"``;英文侧的 issue 文本与
    K2=B 原实现逐字一致(单测契约),中文侧同构生成。
    """
    issues: list[str] = []
    materials = draft.get("materials", {}) or {}

    transitions = _count(materials, "transitions")
    video_effects = _count(materials, "video_effects")
    stickers = _count(materials, "stickers")
    if transitions == 0:
        issues.append(f"{label}转场缺失(尾链 node_09 未对{label}生效?)")
    if video_effects == 0:
        issues.append(f"{label}视频特效缺失(尾链 node_09 未对{label}生效?)")
    if stickers == 0:
        issues.append(f"{label}贴纸缺失(尾链 node_11 未对{label}生效?)")

    # BGM 轨 + 音量:草稿**可能**没有音轨(关卡② 未加 / 镜像失败),那时不报;
    # 有音轨却没音量才是真问题(尾链 node_13 该写没写)。
    audio_tracks = [t for t in (draft.get("tracks", []) or []) if t.get("type") == "audio"]
    if audio_tracks:
        seg_ids = {
            seg.get("id")
            for track in audio_tracks
            for seg in (track.get("segments", []) or [])
            if seg.get("id")
        }
        audios = [m for m in (materials.get("audios", []) or []) if m.get("id") in seg_ids]
        if seg_ids and not audios:
            issues.append(f"{label}音轨引用的 audio material 缺失(BGM 镜像不完整?)")
        elif audios and not any("volume" in m for m in audios):
            issues.append(f"{label}音量未写入(尾链 node_13 未对{label}生效?)")

    return issues


def _load_draft_dict(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """读草稿 JSON;返回 ``(dict, None)`` 或 ``(None, 错误文本)``。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return None, str(exc)


def _emit_qa_event(event: str, *, detail: dict) -> None:
    """经 LangGraph custom stream 上报 delivery.qa_* 观测事件(与 node_05 同款)。

    图运行时外调用 ``get_stream_writer()`` 会 RuntimeError —— 观测事件绝不影响
    主流程,任何异常吞掉即可。
    """
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if callable(writer):
            writer(
                {
                    "monitor_event": "progress",
                    "node_id": "join_before_delivery",
                    "detail": {"event": event, **detail},
                }
            )
    except Exception:  # noqa: BLE001 — 观测通道失败不阻断工作流
        pass


def join_before_delivery(state: WorkflowState) -> dict:
    """LangGraph 节点(交付前 QA 闸门):检查中英两份草稿的交付物是否齐全。

    只返回变更字段(``join_qa_issues`` + ``join_qa_report`` + ``error_log`` +
    ``status_log``),避免覆盖上游未变字段。

    K2=B(2026-10):英文草稿存在时额外做尾链四项检查(转场/特效/贴纸/BGM 音量)。
    英文草稿从未生成时整段跳过,不产生 issues。
    阶段 4(2026-11-07):尾链检查扩到中英两份;深度 QA 结构化进
    ``join_qa_report``;delivery.qa_* 事件供创建台展示。
    """
    issues: list[str] = []
    structured: list[dict[str, str]] = []  # 结构化明细(事件用)

    def _add(text: str) -> None:
        issues.append(text)
        structured.append(
            {"check": "交付物", "severity": SEV_ERROR, "draft": "-", "detail": text}
        )

    covers = state.get("covers") or []
    if len(covers) < 3:
        _add(f"封面缺失:需3种比例,实际 {len(covers)} 种")
    else:
        for c in covers:
            if c.get("en_path") is None:
                _add(f"封面英文版缺失:ratio={c.get('ratio', 'unknown')}")

    if not state.get("subtitle_srt_path"):
        _add("英文字幕文件缺失")

    # ---- 阶段 4:双语草稿深度 QA(draft_integrity)----
    # 口径与 K2=B 一致:英文分支从未生成 → 整段跳过(上游降级,不是交付缺陷)。
    qa_report = build_join_qa_report(state)
    zh_checked = False
    if not qa_report.get("skipped"):
        targets = dict(dual_draft_targets(state))
        if "zh" not in targets:
            _add(f"中文主草稿缺失: {state.get('draft_path')}")
        else:
            zh_dict, zh_err = _load_draft_dict(Path(targets["zh"]))
            if zh_err is not None:
                _add(f"中文主草稿不可读: {zh_err}")
            elif zh_dict is not None:
                zh_checked = True
                for it in _tail_chain_issues(zh_dict, "中文主草稿"):
                    _add(it)

        en_file = targets.get("en")
        if en_file is None:
            _add(f"英文分支草稿缺失: {en_draft_file_missing_path(state)}")
        else:
            en_dict, en_err = _load_draft_dict(Path(en_file))
            if en_err is not None:
                _add(f"英文分支草稿不可读: {en_err}")
            elif en_dict is not None:
                for it in _tail_chain_issues(en_dict, "英文草稿"):
                    _add(it)

    # 深度 QA 的 error 级 issue 并入 join_qa_issues(字符串契约);warning 只进报告。
    for it in qa_report.get("issues", []):
        if it.get("severity") == SEV_ERROR:
            text = issue_text(it)
            issues.append(text)
            structured.append(it)

    # 报告汇总:passed = 无任何 error(交付物 + 深度 QA)
    report_out: dict[str, Any] = {
        **qa_report,
        "passed": not issues,
        "node_issues": [s["detail"] for s in structured if s["check"] == "交付物"],
        "zh_checked": zh_checked,
    }

    event = "delivery.qa_passed" if report_out["passed"] else "delivery.qa_issues"
    _emit_qa_event(
        event,
        detail={
            "passed": report_out["passed"],
            "skipped": bool(qa_report.get("skipped")),
            "issue_count": len(issues),
            "issues": structured[:_EVENT_ISSUE_CAP],
            "zh_draft_dir": str(Path(state["draft_path"]).parent) if state.get("draft_path") else None,
            "en_draft_dir": state.get("draft_dir_en_branch"),
        },
    )

    new_errors: list[str] = [f"[join] {it}" for it in issues]
    return {
        "join_qa_issues": issues,
        "join_qa_report": report_out,
        "error_log": new_errors,
        "status_log": [
            "join_before_delivery_done",
            "delivery_qa_ok" if report_out["passed"] else f"delivery_qa_issues:{len(issues)}",
        ],
    }


__all__ = ["join_before_delivery"]
