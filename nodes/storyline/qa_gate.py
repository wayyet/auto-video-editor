"""storyline_qa_gate — plan_v4 §4.2 硬约束 + fan-in 汇合节点。

职责:
1. **读取** ``storyline_timeline_plan``:由 plan_timeline_pro 写出,需 Pydantic
   校验通过(否则 ``StorylineErrorCode.CONTRACT_INVALID``)。
2. **算出** 实际总时长 ``actual_ms = clips[-1].timeline_out_ms``。
3. **对照** ``storyline_targets.target_duration_ms``,若
   ``actual_ms > target_ms * 1.05`` 且 ``storyline_qa_retry_count < 3`` →
   ``status_log`` append ``storyline_qa_retry``,触发条件边回到
   ``storyline_group_clips`` 重新跑。
4. **通过 / retry 耗尽** → 写 ``storyline_qa_done`` 走 join_storyline。

设计纪律(plan §4.2):
- 节点 **不** 触发副作用(只读产物 + 写 status_log),避免重放副作用
  (与主图人工关卡节点 ``nodes/_checkpoint.py`` 收敛的安全模式同纪律)。
- retry 计数用 state 字段而不是文件,3 次封顶后强制走 join(失败也继续,不阻塞
  node_05,符合 ``node_07`` 的"超限升级给人工,不阻断"已有行为 — 与
  ``graph.py:escalate_guardrail_failure`` 同思路)。
"""
from __future__ import annotations

import json
from pathlib import Path

from state import WorkflowState
from storyline.contract import (
    CanonicalTimeline,
    ContractInvalid,
    StorylineErrorCode,
)
from nodes.storyline._common import append_status_tag, target_duration_ms


def _load_timeline_from_state(state: WorkflowState) -> CanonicalTimeline | None:
    """从 ``storyline_timeline_plan``(JSON 路径)读 CanonicalTimeline。"""
    path = state.get("storyline_timeline_plan")
    if not path:
        return None
    p = Path(str(path))
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return CanonicalTimeline.model_validate(data)
    except (json.JSONDecodeError, OSError):
        return None
    except Exception as e:  # noqa: BLE001 — Pydantic ValidationError
        raise ContractInvalid(
            f"timeline_plan invalid: {e!s}",
            error_code=StorylineErrorCode.CONTRACT_INVALID,
            path=str(p),
        )


def _target_ms(state: WorkflowState) -> int:
    """目标时长(毫秒);统一走 ``_common.target_duration_ms``。

    P1(2026-11-08)前本函数自己兜底 35000,且对非正数原样返回;统一后缺省/
    非正数/非法值一律回落 ``config.DEFAULT_TARGET_DURATION_MS``(35000),
    与 ``_common`` 解析规则一致(由 test_target_duration_single_source 锁定)。
    """
    return target_duration_ms(state)


def _per_clip_issues(canonical: CanonicalTimeline) -> list[str]:
    """逐片段 source/target/speed 一致性复查(2026-11-11 阶段3,§4.4 错误码)。

    总时长达标 ≠ 每段时间映射合法(实机事故:0.883s 源被铺成 11.593s 而全片
    时长完全达标)。正常情况下 contract 不变量已在加载时判死这类计划;本函数
    是防御性复查(阈值 ``STORYLINE_MIN_CLIP_SPEED <= 0`` 关闭时与 contract
    同步放行)并把结构化错误码留痕。违例**不走 retry** —— retry 只对总时长
    超标有意义,逐段违例必须阻止交付。
    """
    from config import STORYLINE_MIN_CLIP_SPEED

    min_speed = float(STORYLINE_MIN_CLIP_SPEED)
    media_by_id = {s.media_id: s for s in canonical.source_media}
    issues: list[str] = []
    for clip in canonical.clips:
        media = media_by_id.get(clip.source_media_id)
        if media is None:
            issues.append(
                f"SOURCE_RANGE_INVALID clip {clip.clip_id}: source_media_id 未闭合"
            )
            continue
        if clip.source_out_ms <= clip.source_in_ms or clip.source_out_ms > media.duration_ms:
            issues.append(
                f"SOURCE_RANGE_INVALID clip {clip.clip_id}: 源区间 "
                f"[{clip.source_in_ms}, {clip.source_out_ms}] 非法或超出媒体时长 "
                f"{media.duration_ms}ms"
            )
        if min_speed > 0:
            target_ms = clip.timeline_out_ms - clip.timeline_in_ms
            source_ms = clip.source_out_ms - clip.source_in_ms
            speed = source_ms / target_ms if target_ms > 0 else 0.0
            if speed < min_speed - 1e-6:
                issues.append(
                    f"CLIP_SPEED_INVALID clip {clip.clip_id}: speed={speed:.4f} < "
                    f"{min_speed}(源 {source_ms}ms → 目标 {target_ms}ms,"
                    "短素材拉长冒充有效画面)"
                )
    return issues


def storyline_qa_gate_node(state: WorkflowState) -> dict:
    """QA 闸门主入口(纯函数副作用:仅写 status_log / error_log / 重试计数)。

    返回 ``dict`` 里:
    - ``status_log``: 总是 append,要么 ``_retry`` 要么 ``_done``
    - ``storyline_qa_retry_count``: 失败时 +1
    - ``error_log``: 产线合约违例时按 ADR-007 写结构化记录
    """
    try:
        canonical = _load_timeline_from_state(state)
    except ContractInvalid as e:
        return {
            "status_log": append_status_tag(state, "storyline_qa_failed"),
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:qa_gate] {e.error_code}: {e}",
            ],
            "storyline_error_code": e.error_code,
        }

    if canonical is None or not canonical.clips:
        # 空 plan / timeline 路径缺失:qa 不阻塞,直接走 join(对应 plan §4.2 第 5 条)
        return {
            "status_log": append_status_tag(
                state, "storyline_qa_done_empty"
            ),
        }

    # 逐片段一致性校验(阶段3):任一违例直接 fail,不进入总时长 retry 循环。
    clip_issues = _per_clip_issues(canonical)
    if clip_issues:
        code = (
            "CLIP_SPEED_INVALID"
            if any(i.startswith("CLIP_SPEED_INVALID") for i in clip_issues)
            else "SOURCE_RANGE_INVALID"
        )
        return {
            "status_log": append_status_tag(state, "storyline_qa_failed"),
            "error_log": [
                *list(state.get("error_log", []) or []),
                *[f"[storyline:qa_gate] {msg}" for msg in clip_issues],
            ],
            "storyline_error_code": code,
        }

    actual_ms = int(canonical.clips[-1].timeline_out_ms)
    target_ms = _target_ms(state)
    retry = int(state.get("storyline_qa_retry_count", 0) or 0)

    # 1.05 容忍阈值:与 node_07 已有 "1.0 + 1e-3" 类似,允许少量 overshoot
    if actual_ms > target_ms * 1.05 and retry < 3:
        return {
            "status_log": append_status_tag(state, "storyline_qa_retry"),
            "storyline_qa_retry_count": retry + 1,
        }

    # 通过 / 重试耗尽(强制走 join)
    return {
        "status_log": append_status_tag(
            state,
            f"storyline_qa_done: actual_ms={actual_ms} target_ms={target_ms}",
        ),
    }


# ---------------------------------------------------------------------------
# 条件边:qa_gate 之后走向(plan §4.2 §2.2 拓扑)
# ---------------------------------------------------------------------------
def route_after_storyline_qa(state: WorkflowState) -> str:
    """读 ``status_log[-1]`` 判断走向 — 与 plan §4.2 route_after_storyline_qa 等价。

    Returns:
        ``"storyline_group_clips"`` — 回退再跑(仅 qa 触发 retry 时)
        ``"storyline_render_video"`` — 通过 / 失败 / 空 plan,统一走
        render_video(默认 noop)→ join_storyline(让 qa_gate 不直接连 join,
        而是经过 render_video 旁路,与 plan §2.2 拓扑一致)
    """
    log = state.get("status_log", []) or []
    if log and log[-1] == "storyline_qa_retry":
        return "storyline_group_clips"
    return "storyline_render_video"


__all__ = [
    "storyline_qa_gate_node",
    "route_after_storyline_qa",
]
