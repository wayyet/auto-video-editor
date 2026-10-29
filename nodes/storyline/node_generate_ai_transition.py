"""Phase 4/5 C 类旁支节点:storyline_generate_ai_transition(plan_v4 §5 阶段 5 / ADR-005)。

2026-10 接入主图(见 ``graph.py`` 建图处),与 ``storyline_plan_timeline_pro`` **并排**::

    group_clips / generate_script / generate_voiceover / select_bgm
        ──(同一道 4 源屏障)──┬─▶ plan_timeline_pro ───────────┐
                             └─▶ generate_ai_transition ──────┴─▶ plan_timeline_ai_transition ─▶ qa_gate

为什么挂在这里,而不是直接挂 ``recommend_transition`` / ``split_shots``:
- 要读的分组(group_clips)和转场方案(recommend_transition)到这一步都已就绪,
  和 plan_timeline_pro 看到的是同一份输入。
- ``recommend_transition`` 每轮会被触发 3 次(group_clips / select_bgm 各一),直接挂它,
  本节点每轮会重复 3 次。
- 到 ``split_shots`` 时分组和转场方案都还不存在。

开关:``STORYLINE_ENABLE_AI_TRANSITION``(默认开;设 ``0`` 关闭)。**建图时读取**:关闭时图里
根本不接这两个节点,拓扑与接入前一致。节点自己也检查开关(旧 checkpoint / 单测直接调用时仍安全)。

⚠️ 本节点在 qa 重试回路里,会随 plan_timeline_pro **每轮重跑一次**。当前只有
``StubAITransitionClient``(每个衔接点写一份 500ms 占位 JSON,不改时间线,无成本);
接入真实客户端时,请在客户端里按输入哈希做缓存,避免重复生成 / 重复付费。

⚠️ 状态标签必须固定:重试回路里不能出现"每轮都不一样"的 ``status_log`` 标签
(如带条数的 ``..._done:items=N``),否则新标签会排到 ``storyline_qa_retry`` 之后,
qa 路由(看 ``status_log[-1]``)会悄悄跳过重试。生成条数写在产物 JSON 里。

输入:
- ``storyline_groups_artifact``:相邻两个 group 的衔接点 = 前一组最后一个 clip → 后一组第一个 clip
- ``storyline_transition_plan``(仅参考):推荐转场按 ``group_id`` 对号,作为
  ``recommended_transition`` 提示随 clip 传给客户端(桩客户端忽略)

输出:
- ``storyline_ai_transition_artifact``:
  ``{"items": [{from_clip_id, to_clip_id, duration_ms, provider, is_stub, error, artifact}],
     "provider": str, "is_stub": bool}``
- ``status_log`` append ``storyline_generate_ai_transition_done``(固定标签)

失败语义:整个节点包在 try/except 里,任何异常只写 ``error_log`` + ``..._failed`` 标签,不抛、
不产出 artifact;下游 ``plan_timeline_ai_transition`` 会因此保留基础时间线,屏障照常凑齐。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from state import WorkflowState
from storyline_capabilities.ai_transition_client import (
    is_ai_transition_enabled,
    get_default_ai_transition_client,
)
from nodes.storyline._common import (
    _read_groups,
    append_status_tag,
    _resolve_outputs_root,
)

logger = logging.getLogger(__name__)


def _adjacent_pairs(groups: list[dict]) -> list[tuple[dict, dict]]:
    """相邻 group 的衔接点:前一组最后一个 clip → 后一组第一个 clip。

    返回的 clip 是**副本**,并补上所属 ``group_id``(用来对上转场方案)。
    """
    ends: list[tuple[dict, dict]] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        clips = [c for c in (g.get("clips") or []) if isinstance(c, dict)]
        if clips:
            gid = str(g.get("group_id") or "")
            ends.append(({**clips[0], "group_id": gid}, {**clips[-1], "group_id": gid}))  # (第一个, 最后一个)
    return [(ends[i][1], ends[i + 1][0]) for i in range(len(ends) - 1)]


def _transition_hints(plan: object) -> dict[str, dict[str, str]]:
    """把 ``storyline_transition_plan.items`` 按 ``group_id`` 建索引(仅参考)。

    格式不对 / 缺字段一律当作"没有提示",绝不抛。
    """
    out: dict[str, dict[str, str]] = {}
    items = plan.get("items") if isinstance(plan, dict) else None
    for it in items or []:
        if isinstance(it, dict) and it.get("group_id"):
            out[str(it["group_id"])] = {
                "transition_in": str(it.get("transition_in") or ""),
                "transition_out": str(it.get("transition_out") or ""),
            }
    return out


def _clip_arg(clip: dict, hint: str) -> dict[str, Any]:
    """传给客户端的 clip 描述:``clip_id`` + ``group_id``,有推荐转场时再带 ``recommended_transition``。"""
    arg: dict[str, Any] = {
        "clip_id": str(clip.get("clip_id") or ""),
        "group_id": str(clip.get("group_id") or ""),
    }
    if hint:
        arg["recommended_transition"] = hint
    return arg


def storyline_generate_ai_transition_node(state: WorkflowState) -> dict:
    if not is_ai_transition_enabled():
        return {
            "status_log": append_status_tag(
                state, "storyline_generate_ai_transition_skipped"
            ),
        }

    try:
        outputs_root = _resolve_outputs_root(state)
        out_dir = outputs_root / "storyline"
        out_dir.mkdir(parents=True, exist_ok=True)

        pairs = _adjacent_pairs(_read_groups(state))
        hints = _transition_hints(state.get("storyline_transition_plan"))
        client = get_default_ai_transition_client()

        results: list[dict] = []
        for from_clip, to_clip in pairs:
            from_hint = hints.get(str(from_clip.get("group_id") or ""), {}).get("transition_out", "")
            to_hint = hints.get(str(to_clip.get("group_id") or ""), {}).get("transition_in", "")
            try:
                r = client.generate(
                    from_clip=_clip_arg(from_clip, from_hint),
                    to_clip=_clip_arg(to_clip, to_hint),
                    output_dir=out_dir,
                )
            except Exception as e:  # noqa: BLE001 — 单个衔接点失败只跳过它
                logger.warning("ai_transition generate exception: %r", e)
                continue
            provider = str(r.provider or "stub")
            results.append(
                {
                    "from_clip_id": r.from_clip_id,
                    "to_clip_id": r.to_clip_id,
                    "duration_ms": int(r.duration_ms),
                    "provider": provider,
                    "is_stub": provider == "stub",
                    "error": r.error,
                    "artifact": str(r.artifact_path),
                }
            )

        providers = {it["provider"] for it in results}
        if not results:
            provider = "none"
        elif len(providers) == 1:
            provider = next(iter(providers))
        else:
            provider = "mixed"

        out_path = out_dir / "ai_transition.json"
        out_path.write_text(
            json.dumps(
                {
                    "items": results,
                    "provider": provider,
                    "is_stub": all(it["is_stub"] for it in results),
                },
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
        logger.info("generate_ai_transition: items=%d provider=%s", len(results), provider)
    except Exception as e:  # noqa: BLE001 — 旁支节点,绝不拖垮主线
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:generate_ai_transition] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_generate_ai_transition_failed"
            ),
        }

    return {
        "storyline_ai_transition_artifact": str(out_path),
        "status_log": append_status_tag(state, "storyline_generate_ai_transition_done"),
    }
