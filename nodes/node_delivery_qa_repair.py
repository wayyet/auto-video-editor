"""delivery_qa_repair / delivery_qa_failed — 交付 QA 阻断路由的两个节点(阶段 6)。

计划 §8 阶段 6 任务 4:「将 QA 的 passed=false 接入 LangGraph 条件边;按错误
类型进入定向修复,无法修复则终止为 failed,不允许静默 END。」

- :func:`delivery_qa_repair` —— **定向修复**:对"可自动重试"的 required 类别
  (转场/字幕动画/贴纸/音量淡入淡出)重跑对应尾链节点函数;spec 重发射后以
  ``force_rebuild`` 重编译并**消费编译回执**(R-04/R-05:attempted ≠ compiled,
  编译失败/跳过如实进 state);编译成功的分支其 hook 产物被 SDK 全量重建抹掉
  → 按分支重放开场钩子(R-07)。修复后回到 ``join_before_delivery`` 复检
  (条件边路由)——**复检才是最终成功判据,修复尝试本身不等于交付成功**。
  字幕/配音/BGM/草稿结构性缺失属数据性缺陷,重跑无意义 → 不在分派表内,
  直接走失败终止。
- :func:`delivery_qa_failed` —— **失败终止**:raise RuntimeError,
  ``monitoring/workflow_runner.run_with_events`` 捕获图执行异常后发
  ``run.failed``,运行状态置 failed(对照 ``escalate_guardrail_failure`` 的
  "写 error 后连 END"——那仍会被创建台调和为 completed,不满足
  「不静默 END」的验收标准)。

图接线(graph.py)::

    join_before_delivery → route_after_join_qa
        gate 未失败            → export_final_video
        gate 失败 且未修复过    → delivery_qa_repair → join_before_delivery(回边复检)
        gate 失败 且已修复过    → delivery_qa_failed → END(raise → run.failed)

2026-11-23 交付 QA 修复(console-3e866b87d5d6):

- **编译回执必须消费** —— ``compile_and_publish_draft`` 把异常折叠为
  ``failed`` / ``failed_blocked_features`` 字典返回,**不上抛**;旧实现丢弃
  返回值,节点永远记 done(审查 R-04)。现在分支级回执进
  ``state.delivery_qa_compile``,状态标签区分 attempted / compiled / failed
  (§4.6),"已尝试"不再伪装成"已修复"。
- **强制重建** —— 指纹相同 ≠ 草稿当前完整(QA 已判定构建块缺失);repair
  重编译传 ``force_rebuild=True`` 跳过指纹短路(仍经 blocked_features
  fail-closed 与安全写入,审查 R-05/计划 §4.4)。
- **hook 重放** —— 编译成功的分支 hook 被抹,按分支调
  ``node_09b.replay_hook_for_draft`` 恢复;日志增量合并进 repair state;
  重放失败如实记 error_log,不伪装成功(计划 §4.5)。

2026-11-23 BGM 镜像修复(console-4f97c7818e4c):

- **BGM 重镜像** —— force_rebuild 让两份草稿的 BGM 段 id 各自换成新 uuid,
  编译成功后补调 ``node_12.mirror_bgm_to_en_draft`` 恢复镜像不变量。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from draft_ops.draft_integrity import (
    REQUIRED_FEATURE_KEYS,
    CAT_DEGRADED,
    CAT_FAILED,
    resolve_required_features,
)
from state import WorkflowState

#: 修复上限:1 次(repair → join 复检一轮;再失败即终止,不与上游降级缠斗)
DELIVERY_QA_MAX_REPAIRS = 1


def _compile_receipt_status(record: dict) -> str:
    """把编译回执折叠为修复语义的 status(compiled + 发布失败 → failed)。

    R-05:回执必须检查两层 —— ``record["status"]`` 与 ``record["write"].ok``;
    ``compiled`` 但 ``write.ok=false`` 不是修复成功。
    """
    status = str(record.get("status") or "failed")
    if status == "compiled" and not bool((record.get("write") or {}).get("ok")):
        return "failed"
    return status


def _recompile_drafts(state: WorkflowState, *, force_rebuild: bool = True) -> dict:
    """spec 重发射后对中英两份草稿各重编译一次,**消费编译回执**。

    修复动作里 ``inject_fx`` / ``inject_text_fx(10/10b)`` 只发射 DraftBuildSpec
    (六段 legacy 原生写入已退役);草稿真实更新必须经 SDK 重编译。
    ``inject_sticker``(node_11)自身就是编译汇合入口,不需要本步骤。

    Args:
        state: 工作流状态(draft_path / draft_dir_en_branch / storyline_plan)。
        force_rebuild: 修复场景恒 True —— QA 已判定构建块缺失,指纹相同不能
            等同于"草稿当前完整";只跳过指纹短路,blocked_features 与安全写入
            照常(审查 R-05)。

    Returns:
        state 增量 dict(status_log / error_log / delivery_qa_compile)。
        分支独立记录(pipeline 把编译/发布异常折叠为 failed 字典返回,**不上抛**
        —— 旧 docstring"编译失败直接上抛"不实,正是回执必须显式消费的原因);
        ``skipped_no_spec`` / ``skipped_fingerprint`` 等跳过态按"未修复"如实记
        error,不伪装成功。
    """
    from jianying_sdk.pipeline import compile_and_publish_draft

    canonical_plan = state.get("storyline_plan")
    plan = canonical_plan if isinstance(canonical_plan, dict) else None
    job_id = str(state.get("session_id") or "")

    zh_path = state.get("draft_path")
    en_dir = state.get("draft_dir_en_branch")
    targets: list[tuple[str, Path, Path]] = []
    if zh_path:
        targets.append(("zh", Path(zh_path).parent, Path(zh_path)))
    if en_dir and (Path(en_dir) / "draft_content.json").exists():
        targets.append(("en", Path(en_dir), Path(en_dir) / "draft_content.json"))

    status_log: list[str] = []
    error_log: list[str] = []
    compile_results: dict[str, dict[str, Any]] = {}
    for lang, draft_dir, draft_file in targets:
        audit_node = f"delivery_qa_recompile_{lang}"
        try:
            record = compile_and_publish_draft(
                draft_dir,
                draft_file,
                job_id=job_id,
                canonical_plan=plan,
                audit_node=audit_node,
                force_rebuild=force_rebuild,
            )
        except Exception as exc:  # noqa: BLE001 — 管线异常兜底(与 node_11 同款)
            record = {"status": "failed", "note": repr(exc)}
        status = _compile_receipt_status(record)
        note = str(record.get("note") or "")
        compile_results[lang] = {
            "status": status,
            "note": note[:200],
            "fingerprint": record.get("fingerprint"),
        }
        status_log.append(f"{audit_node}:{status}")
        if status != "compiled":
            error_log.append(
                f"[delivery_qa] SDK 重编译未成功({lang}): {status}; {note[:200]}"
            )
    return {
        "status_log": status_log,
        "error_log": error_log,
        "delivery_qa_compile": compile_results,
    }


def _node_fn_delta(fn: Callable[[WorkflowState], dict]) -> Callable[[WorkflowState], dict]:
    """把"返回整份 state"的节点函数适配为**增量契约**(R-04 统一 state-update)。

    节点函数(status_log/error_log 走 _append_unique reducer)实现均为
    ``list(state.get(...)) + append`` 后整份返回;按调用前后长度截取的增量
    即本节点应并入 state 的部分。适配后 repair 节点对全部动作统一合并,
    不再丢弃任何返回值。
    """

    def _runner(state: WorkflowState) -> dict:
        before_status = len(list(state.get("status_log") or []))
        before_error = len(list(state.get("error_log") or []))
        out = fn(state)
        if not isinstance(out, dict):
            return {}
        out_status = list(out.get("status_log") or [])
        out_error = list(out.get("error_log") or [])
        return {
            "status_log": out_status[before_status:],
            "error_log": out_error[before_error:],
        }

    return _runner


def _build_dispatch() -> dict[str, Callable[[WorkflowState], dict]]:
    """required 类别 → 幂等修复动作(**增量契约**:返回 state 增量 dict)。

    未列出的类别(subtitles_*/voiceover_*/bgm)是数据性缺失,不可自动修复。
    """
    """延迟 import 尾链节点函数(避免 nodes 包内初始化顺序依赖)。"""
    from nodes.node_09_inject_fx import inject_fx
    from nodes.node_10_inject_text_fx import inject_text_fx, inject_text_fx_en
    from nodes.node_11_inject_sticker import inject_sticker
    from nodes.node_13_adjust_volume import adjust_volume

    def _transitions(state: WorkflowState) -> dict:
        fx_delta = _node_fn_delta(inject_fx)(state)
        compile_delta = _recompile_drafts(state)
        return {
            "status_log": fx_delta["status_log"] + compile_delta["status_log"],
            "error_log": fx_delta["error_log"] + compile_delta["error_log"],
            "delivery_qa_compile": compile_delta["delivery_qa_compile"],
        }

    def _text_animation(state: WorkflowState) -> dict:
        # 装饰对两份草稿 spec 各自发射;英文分支未生成时 10b 内部自带降级跳过
        fx_delta = _node_fn_delta(inject_text_fx)(state)
        fx_en_delta = _node_fn_delta(inject_text_fx_en)(state)
        compile_delta = _recompile_drafts(state)
        return {
            "status_log": (
                fx_delta["status_log"] + fx_en_delta["status_log"]
                + compile_delta["status_log"]
            ),
            "error_log": (
                fx_delta["error_log"] + fx_en_delta["error_log"]
                + compile_delta["error_log"]
            ),
            "delivery_qa_compile": compile_delta["delivery_qa_compile"],
        }

    return {
        "transitions": _transitions,
        "text_animation": _text_animation,
        "stickers": _node_fn_delta(inject_sticker),
        "volume_fades": _node_fn_delta(adjust_volume),
    }


def _blocked_categories(state: WorkflowState) -> list[str]:
    """处于 failed/degraded 的 required 类别(gate 阻断清单)。"""
    report = state.get("join_qa_report") or {}
    categories = report.get("categories") or {}
    required = report.get("required_features") or resolve_required_features(state)
    blocked: list[str] = []
    for key in REQUIRED_FEATURE_KEYS:
        if not required.get(key):
            continue
        status = str((categories.get(key) or {}).get("status") or "")
        if status in (CAT_FAILED, CAT_DEGRADED):
            blocked.append(key)
    return blocked


def _replay_hooks_for_compiled(
    compile_results: dict[str, dict[str, Any]],
    state: WorkflowState,
    merged: dict[str, list[str]],
) -> list[str]:
    """对**编译成功**的分支重放开场钩子(编译是全量重建,hook 产物被抹)。

    编译失败/跳过的分支草稿未动、hook 仍在,不重放(计划 §4.5:只对确实
    完成重建的分支恢复)。重放日志增量并入 ``merged``;失败如实记
    error_log —— 重放失败不伪装成功,QA 若把 hook 设为 required,由复检验证。

    Returns:
        已重放的语言列表(zh/en)。
    """
    from nodes.node_09b_inject_opening_hook import replay_hook_for_draft

    replayed: list[str] = []
    for lang in sorted(compile_results):
        if compile_results[lang].get("status") != "compiled":
            continue
        if lang == "zh":
            zh_path = state.get("draft_path")
            draft_file = Path(zh_path) if zh_path else None
        else:
            en_dir = state.get("draft_dir_en_branch")
            draft_file = Path(en_dir) / "draft_content.json" if en_dir else None
        if draft_file is None or not draft_file.exists():
            continue
        try:
            update = replay_hook_for_draft(draft_file, state, lang=lang)
        except Exception as exc:  # noqa: BLE001 — 重放失败不阻断复检路由,如实记错
            merged["error_log"].append(f"[delivery_qa] hook 重放失败({lang}): {exc!r}")
            continue
        update_status = list(update.get("status_log") or [])
        merged["status_log"].extend(update_status)
        merged["error_log"].extend(update.get("error_log") or [])
        if not any("_noop" in line for line in update_status):
            # noop(开关关闭/无标题且无特效可写)不计入 replayed_hook,标签语义
            # 保持诚实(§4.6:attempted/replayed 不互相冒充)。
            replayed.append(lang)
    return replayed


def _remirror_bgm_after_compile(
    compile_results: dict[str, dict[str, Any]],
    state: WorkflowState,
    merged: dict[str, list[str]],
) -> bool:
    """重编译成功后把中文 BGM 重新镜像进英文草稿(2026-11-23,console-4f97c7818e4c)。

    正常尾链顺序是 node_11 编译 → node_12 镜像,镜像在编译**之后**,两份草稿的
    BGM 段 id 一致;repair 的 ``force_rebuild`` 在镜像**之后**又各自重建两份草稿,
    SDK 用 uuid4 重新生成段/素材 id,镜像关系被打散。这里补回 node_12 的镜像
    (按角色合并、幂等、失败只记 error_log 不抛),恢复正常链的不变量。

    只要有任一分支编译成功就执行:zh 重建 → 英文侧要跟上新 id;en 重建 →
    英文侧的 BGM 段 id 也已漂移。两侧都未编译(失败/跳过)时草稿未动,不执行。

    Returns:
        是否执行了镜像(用于状态标签)。
    """
    if not any(rec.get("status") == "compiled" for rec in compile_results.values()):
        return False
    from nodes.node_12_human_add_bgm import mirror_bgm_to_en_draft

    try:
        notes = mirror_bgm_to_en_draft(state)
    except Exception as exc:  # noqa: BLE001 — 镜像失败不阻断复检路由,如实记错
        merged["error_log"].append(f"[delivery_qa] BGM 重镜像异常: {exc!r}")
        return True
    for note in notes:
        key = "status_log" if note.startswith("node_12_bgm_mirrored_en_done") else "error_log"
        merged[key].append(note)
    return True


def _emit_qa_event(event: str, *, node_id: str, detail: dict) -> None:
    """经 LangGraph custom stream 上报 delivery.qa_* 观测事件(与 join 同款)。"""
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if callable(writer):
            writer(
                {
                    "monitor_event": "progress",
                    "node_id": node_id,
                    "detail": {"event": event, **detail},
                }
            )
    except Exception:  # noqa: BLE001 — 观测通道失败不阻断工作流
        pass


def delivery_qa_repair(state: WorkflowState) -> dict:
    """LangGraph 节点:分派幂等修复 → 消费编译回执 → 重放 hook → 回 join 复检。

    所有动作按**增量契约**返回 state 更新,本节点统一合并(R-04:禁止调用后
    丢弃返回值);状态标签区分 attempted / compiled / failed / replayed_hook
    (§4.6:监控端不再把"执行过修复动作"误读为"修复成功")。
    """
    blocked = _blocked_categories(state)
    attempted: list[str] = []
    unrepairable: list[str] = []
    merged: dict[str, list[str]] = {"status_log": [], "error_log": []}
    compile_results: dict[str, dict[str, Any]] = {}
    replayed: list[str] = []
    remirrored = False
    if blocked:
        dispatch = _build_dispatch()
        for key in blocked:
            action = dispatch.get(key)
            if action is None:
                unrepairable.append(key)
                continue
            update = action(state) or {}  # 幂等重放;增量必须合并(编译失败可见)
            attempted.append(key)
            for log_key in ("status_log", "error_log"):
                merged[log_key].extend(update.get(log_key) or [])
            compile_update = update.get("delivery_qa_compile")
            if isinstance(compile_update, dict):
                compile_results.update(compile_update)
        replayed = _replay_hooks_for_compiled(compile_results, state, merged)
        remirrored = _remirror_bgm_after_compile(compile_results, state, merged)

    compiled_langs = sorted(
        lang for lang, rec in compile_results.items() if rec.get("status") == "compiled"
    )
    failed_langs = sorted(
        lang for lang, rec in compile_results.items() if rec.get("status") != "compiled"
    )

    tags = ["delivery_qa_repair_done"]
    if attempted:
        tags.append(f"delivery_qa_repair_attempted:{','.join(attempted)}")
    if compiled_langs:
        tags.append(f"delivery_qa_repair_compiled:{','.join(compiled_langs)}")
    if failed_langs:
        tags.append(f"delivery_qa_repair_failed:{','.join(failed_langs)}")
    if replayed:
        tags.append(f"delivery_qa_repair_replayed_hook:{','.join(replayed)}")
    if remirrored:
        tags.append("delivery_qa_repair_remirrored_bgm")
    if unrepairable:
        tags.append(f"delivery_qa_unrepairable:{','.join(unrepairable)}")
    _emit_qa_event(
        "delivery.qa_repair",
        node_id="delivery_qa_repair",
        detail={
            "attempted": attempted,
            "unrepairable": unrepairable,
            "compiled": compiled_langs,
            "failed": failed_langs,
            "replayed_hook": replayed,
            "remirrored_bgm": remirrored,
        },
    )
    return {
        "delivery_qa_retry_count": DELIVERY_QA_MAX_REPAIRS,
        "delivery_qa_compile": compile_results,
        "status_log": tags + merged["status_log"],
        "error_log": merged["error_log"],
    }


def delivery_qa_failed(state: WorkflowState) -> dict:
    """LangGraph 节点:required 特性复检仍失败 → 终止运行(run.failed)。

    raise 前先上报 ``delivery.qa_failed`` 事件(创建台交付卡显示失败与
    阻断类别);异常由 ``run_with_events`` 转成 ``run.failed``。
    """
    report = state.get("join_qa_report") or {}
    categories = report.get("categories") or {}
    required = report.get("required_features") or resolve_required_features(state)
    blocked = _blocked_categories(state)
    summary = {
        key: {
            "status": str((categories.get(key) or {}).get("status") or "?"),
            "issues": [
                str(i.get("detail") or "")
                for i in ((categories.get(key) or {}).get("issues") or [])
                if isinstance(i, dict) and i.get("severity") == "error"
            ][:5],
        }
        for key in blocked
    }
    _emit_qa_event(
        "delivery.qa_failed",
        node_id="delivery_qa_failed",
        detail={
            "blocked": blocked,
            "required_features": {k: bool(required.get(k)) for k in REQUIRED_FEATURE_KEYS},
            "categories": summary,
            "sha256": report.get("sha256") or {},
        },
    )
    detail_text = "; ".join(f"{k}={v['status']}" for k, v in summary.items()) or "未知类别"
    raise RuntimeError(f"[delivery_qa] 必需功能未通过,交付阻断(复检仍失败): {detail_text}")


__all__ = [
    "DELIVERY_QA_MAX_REPAIRS",
    "delivery_qa_failed",
    "delivery_qa_repair",
]
