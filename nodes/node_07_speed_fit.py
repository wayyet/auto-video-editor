"""节点 7:speed_fit — 护栏节点(分叉点,对应原文档 4.2 节)。

把"已存在"草稿的视频时间轴压到不超过目标时长 —— 目标读
``state.storyline_targets.target_duration_ms``(创建台"目标时长(秒)"输入框,
P1 决定 1 选 B 后可调),缺省 35s(``config.DEFAULT_TARGET_DURATION_MS``)。
- 帧对齐分配公式借鉴 ``E:\\Documents\\kuaishou\\.claude\\skills\\jianying-speed-fit-35s``
  (该 skill 在 7 段 64.981s → 35s 的真实草稿上已验证)
- 重试用 **图级条件边** 表达(``graph.route_after_speed_fit``),而非节点内部
  while 循环 —— 每次重试在 LangGraph 追踪里独立可见
- 放行时(2026-11-14 依赖约简阶段4 起)由节点本体把草稿目录复制为
  ``snapshots/snapshot2/`` 并写 ``state.snapshot2_path``(历史:该副作用曾由
  条件路由完成,经 bridge_snapshot2 桥接节点回写 state —— 桥接已删)
- 写入字段:
  - ``materials.speeds[]``: 速度 material 列表
  - ``segments[].speed``: 段速度引用
  - ``segments[].extra_material_refs``: 段对 speed material 的引用
  - ``segments[].target_timerange.{start, duration}``: 段在时间轴上的位置与时长
  - 顶层 ``draft["duration"]``: 视频轨总时长
"""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from config import (
    NODE_07_DEFAULT_FPS,
    NODE_07_MAX_RETRY,
    STORYLINE_MIN_CLIP_SPEED,
)
from nodes._draft_io import find_video_track, jianying_running_tags, load_draft, write_draft
from nodes.storyline._common import target_duration_ms
from state import WorkflowState


# ---------------------------------------------------------------------------
# 公共工具:读/写 draft_content.json
# ---------------------------------------------------------------------------
def _compute_total_duration_us(video_track: dict) -> int:
    return sum(int(seg.get("target_timerange", {}).get("duration", 0)) for seg in video_track.get("segments", []))


def _segment_source_duration_us(seg: dict) -> int | None:
    """段的真实源跨度(微秒);``source_timerange`` 缺失/非法时返回 None。

    2026-11-11 阶段3:速度语义 = 源跨度/目标跨度,判断与计算都以源跨度为基准;
    旧草稿(无 source_timerange)按 None 处理,调用方以 target 兜底(与旧版等价)。
    """
    tr = seg.get("source_timerange")
    if not isinstance(tr, dict):
        return None
    try:
        d = int(tr.get("duration") or 0)
    except (TypeError, ValueError):
        return None
    return d if d > 0 else None


def _set_segment_speed(draft: dict, seg: dict, speed: float) -> None:
    """写段速度并同步 speed material(node_07 唯一写入口)。

    同时落两处(剪映按 material 里的速度播放,段上 speed 仅作声明):
    ``seg["speed"]`` + ``materials.speeds[]``(已有引用则原位更新,否则新建)。
    """
    seg["speed"] = float(speed)
    materials = draft.setdefault("materials", {})
    speeds_material = materials.setdefault("speeds", [])
    refs = seg.setdefault("extra_material_refs", [])
    speed_ref = next(
        (ref for ref in refs if any(sm.get("id") == ref for sm in speeds_material)),
        None,
    )
    if speed_ref is None:
        mat_id = f"speed-{uuid4().hex[:8]}"
        speeds_material.append({
            "id": mat_id,
            "speed": float(speed),
            "mode": 0,           # 0 = 常规变速(曲线变速 mode != 0)
            "curve_speed": None,
            "type": "speed",     # 原生 speed material 必带(2026-11-07 样本核对)
        })
        refs.append(mat_id)
    else:
        for sm in speeds_material:
            if sm.get("id") == speed_ref:
                sm["speed"] = float(speed)


# ---------------------------------------------------------------------------
# 核心:帧对齐分配公式
# ---------------------------------------------------------------------------
def _frame_aligned_durations(
    src_durations_us: list[int],
    target_total_us: int,
    fps: int,
) -> list[int]:
    """借鉴 jianying-speed-fit-35s:总帧数预算 + 每段帧对齐,末段吸收余数。

    Args:
        src_durations_us: 每段源时长(微秒)
        target_total_us: 目标总时长(微秒)
        fps: 帧率

    Returns:
        每段新时长(微秒),列表长度等于 src_durations_us。
    """
    total_src = sum(src_durations_us)
    target_total_frames = target_total_us * fps // 1_000_000  # 微秒 → 帧

    new_durations: list[int] = []
    allocated_frames = 0
    n = len(src_durations_us)
    for i, src_dur in enumerate(src_durations_us):
        if i < n - 1:
            # 比例分配 + 四舍五入
            frames = round(target_total_frames * src_dur / total_src)
            frames = max(1, frames)  # 至少 1 帧
            new_durations.append(frames * 1_000_000 // fps)
            allocated_frames += frames
        else:
            # 最后一段吸收帧数余数
            remaining_frames = max(1, target_total_frames - allocated_frames)
            new_durations.append(remaining_frames * 1_000_000 // fps)
    return new_durations


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def speed_fit(state: WorkflowState) -> dict:
    """读取 draft → 逐段速度护栏 → 若超时则变速压缩 → 原子写回。

    2026-11-11 阶段3重写(设计文档 §4.4):速度语义唯一为
    ``speed = 源跨度 / 目标跨度``,处理不再只看目标总时长:

    1. **拉长护栏**(新):``target > 源跨度/STORYLINE_MIN_CLIP_SPEED`` 的段
       钳回上限(实机事故:0.883s 源被铺成 11.593s,停帧冒充画面);
       允许范围内的慢动作段,把与区间矛盾的段速度纠偏成真实速度;
    2. **总时长压缩**(原护栏):超目标时帧对齐压缩,速度同样按源跨度计算
       (旧版误用 target 作分子,对已带真实速度的段会算错变速值)。
    """
    raw_draft_path = state.get("draft_path")
    if not raw_draft_path:
        # node_05 因契约不合法早退时 draft_path 是 None(D7,2026-10-20 修)。
        # 降级跳过,不让 Path(None) 抛 TypeError 把整个 worker 带走。
        state.setdefault("error_log", []).append(
            "[node_07] state.draft_path 缺失,跳过变速处理"
        )
        status_log = list(state.get("status_log", []) or [])
        status_log.append("node_07_speed_fit_skipped")
        return {**state, "status_log": status_log}
    draft_path = Path(raw_draft_path)
    draft = load_draft(draft_path)
    video_track = find_video_track(draft)

    src_segments = list(video_track.get("segments", []))
    fps = video_track.get("fps", NODE_07_DEFAULT_FPS)

    # P1(决定 1 选 B):目标时长读 state(创建台输入框),毫秒 → 微秒;缺省 35s
    target_us = target_duration_ms(state) * 1000
    min_speed = float(STORYLINE_MIN_CLIP_SPEED)
    changed = False

    # ---- Pass 1:逐段拉长护栏(不允许把短素材拉长冒充有效画面)----
    for seg in src_segments:
        src_dur = _segment_source_duration_us(seg)
        if not src_dur:
            continue
        tgt_dur = int((seg.get("target_timerange") or {}).get("duration") or 0)
        if tgt_dur <= 0:
            continue
        # int 向下取整:派生 speed = src/new 恒 >= 阈值,不踩下限边界
        cap = int(src_dur / min_speed) if min_speed > 0 else None
        new_dur = min(tgt_dur, cap) if cap is not None else tgt_dur
        expected_speed = src_dur / new_dur if new_dur > 0 else 1.0
        current = float(seg.get("speed") or 1.0)
        if new_dur != tgt_dur or abs(current - expected_speed) > 1e-6:
            (seg.setdefault("target_timerange", {}))["duration"] = new_dur
            _set_segment_speed(draft, seg, expected_speed)
            changed = True

    # ---- Pass 2:总时长压缩(原护栏,速度改按源跨度计算)----
    src_total_us = _compute_total_duration_us(video_track)
    if src_total_us > target_us:
        src_durations = [int(seg["target_timerange"]["duration"]) for seg in src_segments]
        new_durations = _frame_aligned_durations(src_durations, target_us, fps)
        for seg, new_dur in zip(src_segments, new_durations):
            # 速度分子必须是真实源跨度;无 source_timerange 的旧草稿按 target
            # 兜底(与旧版行为等价)。
            src_dur = _segment_source_duration_us(seg) or int(
                seg["target_timerange"]["duration"]
            )
            (seg.setdefault("target_timerange", {}))["duration"] = int(new_dur)
            _set_segment_speed(draft, seg, src_dur / new_dur if new_dur > 0 else 1.0)
            changed = True

    if not changed:
        # 已达标且无逐段违例:产出快照②并放行(2026-11-14 依赖约简阶段4:
        # 快照生产从 route_after_speed_fit 移入节点本体,路由函数不再有
        # copytree 副作用)。仍递增重试计数便于联调断言(Week 3 兼容 Week 2 测试期望)。
        retry = dict(state.get("retry_counts") or {})
        retry["node_07"] = retry.get("node_07", 0) + 1
        update: dict = {**state, "retry_counts": retry}
        update["snapshot2_path"] = _produce_snapshot2(draft_path)
        return update

    # 重算 target_timerange.start + 顶层 duration(拉长钳制/压缩都改变了时长)
    cursor = 0
    for seg in src_segments:
        seg["target_timerange"]["start"] = cursor
        cursor += int(seg["target_timerange"]["duration"])
    video_track["segments"] = src_segments
    draft["duration"] = cursor

    # Week 5:safe_write_draft 双写(content + info)+ duration 索引同步;签名从
    # (draft_file, content) 改为 (draft_dir, content),参数提升为目录。
    write_result = write_draft(
        draft_path, draft, duration_us=int(draft.get("duration", 0)),
        audit_node="node_07",
    )
    log = list(state.get("status_log", []) or [])
    log.extend(jianying_running_tags("node_07", (write_result,)))

    retry = dict(state.get("retry_counts") or {})
    retry["node_07"] = retry.get("node_07", 0) + 1

    # 2026-11-14 依赖约简阶段4:写失败(write_draft 返回 ok=False,不抛异常)时
    # 不产快照、不写 snapshot2_path —— 路由按"草稿仍超时长"走 retry/escalate,
    # 防御语义与写失败重试等价;写成功才产出快照②并放行。
    write_ok = bool(write_result.get("ok"))
    if write_ok:
        snapshot2_path: str = _produce_snapshot2(draft_path)
    else:
        snapshot2_path = None
        log.append("node_07_speed_fit_write_failed")

    update = {**state, "retry_counts": retry, "status_log": log}
    if snapshot2_path:
        update["snapshot2_path"] = snapshot2_path
    return update


# ---------------------------------------------------------------------------
# 快照②生产(2026-11-14 依赖约简阶段4:从 route_after_speed_fit 移入)
# ---------------------------------------------------------------------------
def _produce_snapshot2(draft_path: Path) -> str:
    """达标放行时把草稿目录复制为 snapshots/snapshot2/,返回快照文件路径。

    node_08/fork 等下游节点读 state["snapshot2_path"] 消费;复制失败向上抛
    (与原 route 内联行为一致,炸图可见而非静默用旧草稿)。
    """
    snap_dir = draft_path.parent.parent / "snapshots" / "snapshot2"
    snap_dir.mkdir(parents=True, exist_ok=True)
    if draft_path.parent.exists():
        shutil.copytree(draft_path.parent, snap_dir, dirs_exist_ok=True)
    return str(snap_dir / "draft_content.json")


# ---------------------------------------------------------------------------
# 条件边路由(由 graph.py 引用;2026-11-14 依赖约简阶段4 起纯路由 —— 快照
# 生产移入 speed_fit 节点本体,本函数只读 state/草稿返回标签,不再有文件副作用)
# ---------------------------------------------------------------------------
def route_after_speed_fit(state: WorkflowState) -> str:
    """节点 7 之后的条件边:达标 → node_08(快照已由节点写入 state);未达标 → 自循环 / 升级。"""
    raw_draft_path = state.get("draft_path")
    if not raw_draft_path:
        # draft 没生成(D7,2026-10-20 修):没有可调的对象,直接放行到 node_08
        # 让它自己降级,而不是在这里抛 TypeError。
        return "node_08_add_subtitles"
    draft_path = Path(raw_draft_path)
    draft = load_draft(draft_path)
    duration = draft.get("duration", 0)

    # P1(决定 1 选 B):与 speed_fit 同源,目标读 state,缺省 35s
    target_us = target_duration_ms(state) * 1000

    if duration <= target_us:
        # 快照②已由 speed_fit 节点本体在放行路径产出并写入 state
        # (写失败时不产快照,草稿保持违例状态 → 走下方 retry/escalate)。
        return "node_08_add_subtitles"

    retry = (state.get("retry_counts") or {}).get("node_07", 0)
    if retry >= NODE_07_MAX_RETRY:
        return "escalate_guardrail_failure"
    return "node_07_speed_fit"