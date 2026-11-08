"""节点 7:speed_fit — 护栏节点(分叉点,对应原文档 4.2 节)。

把"已存在"草稿的视频时间轴压到不超过目标时长 —— 目标读
``state.storyline_targets.target_duration_ms``(创建台"目标时长(秒)"输入框,
P1 决定 1 选 B 后可调),缺省 35s(``config.DEFAULT_TARGET_DURATION_MS``)。
- 帧对齐分配公式借鉴 ``E:\\Documents\\kuaishou\\.claude\\skills\\jianying-speed-fit-35s``
  (该 skill 在 7 段 64.981s → 35s 的真实草稿上已验证)
- 重试用 **图级条件边** 表达(``graph.route_after_speed_fit``),而非节点内部
  while 循环 —— 每次重试在 LangGraph 追踪里独立可见
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
)
from nodes._draft_io import find_video_track, jianying_running_tags, load_draft, write_draft
from nodes.storyline._common import target_duration_ms
from state import WorkflowState


# ---------------------------------------------------------------------------
# 公共工具:读/写 draft_content.json
# ---------------------------------------------------------------------------
def _compute_total_duration_us(video_track: dict) -> int:
    return sum(int(seg.get("target_timerange", {}).get("duration", 0)) for seg in video_track.get("segments", []))


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
    """读取 draft → 校验时长 → 若超时则变速 → 原子写回。"""
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

    src_total_us = _compute_total_duration_us(video_track)
    # 已达标:直接返回(由条件边判断后产出 snapshot2 → node_08)
    if src_total_us <= target_us:
        # 仍递增重试计数便于联调断言(Week 3 兼容 Week 2 测试期望)
        retry = dict(state.get("retry_counts") or {})
        retry["node_07"] = retry.get("node_07", 0) + 1
        return {**state, "retry_counts": retry}

    src_durations = [int(seg["target_timerange"]["duration"]) for seg in src_segments]
    new_durations = _frame_aligned_durations(src_durations, target_us, fps)

    # 应用速度:写 segment.speed + materials.speeds[].speed + extra_material_refs
    materials = draft.setdefault("materials", {})
    speeds_material = materials.setdefault("speeds", [])

    for seg, new_dur in zip(src_segments, new_durations):
        src_dur = int(seg["target_timerange"]["duration"])
        speed = src_dur / new_dur if new_dur > 0 else 1.0
        seg["speed"] = speed

        refs = seg.setdefault("extra_material_refs", [])
        # 找该段已引用的 speed material(原生伴随结构里每段一个;旧链路可能没有)
        speed_ref = next(
            (ref for ref in refs if any(sm.get("id") == ref for sm in speeds_material)),
            None,
        )
        if speed_ref is None:
            mat_id = f"speed-{uuid4().hex[:8]}"
            speeds_material.append({
                "id": mat_id,
                "speed": speed,
                "mode": 0,           # 0 = 常规变速(曲线变速 mode != 0)
                "curve_speed": None,
                "type": "speed",     # 原生 speed material 必带(2026-11-07 样本核对)
            })
            refs.append(mat_id)
        else:
            # 原生伴随结构(转换器引入):剪映按 material 里的速度播放,
            # 段引用已存在时必须同步写新速度,否则变速不生效
            for sm in speeds_material:
                if sm.get("id") == speed_ref:
                    sm["speed"] = speed

    # 重算 target_timerange.start + 顶层 duration
    cursor = 0
    for seg, new_dur in zip(src_segments, new_durations):
        seg["target_timerange"]["start"] = cursor
        seg["target_timerange"]["duration"] = new_dur
        cursor += new_dur
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

    return {**state, "retry_counts": retry, "status_log": log}


# ---------------------------------------------------------------------------
# 条件边路由(由 graph.py 引用)
# ---------------------------------------------------------------------------
def route_after_speed_fit(state: WorkflowState) -> str:
    """节点 7 之后的条件边:达标 → 产出 snapshot2 + node_08;未达标 → 自循环 / 升级。"""
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
        # 产出快照②:cp 草稿目录到 snapshots/snapshot2/
        snap_dir = draft_path.parent.parent / "snapshots" / "snapshot2"
        snap_dir.mkdir(parents=True, exist_ok=True)
        if draft_path.parent.exists():
            shutil.copytree(draft_path.parent, snap_dir, dirs_exist_ok=True)
        snapshot2_path = str(snap_dir / "draft_content.json")
        # 通过 Command 透传 snapshot2_path:这里不能直接改 state dict 返回值(条件边不接受 state)
        # graph.py 会在节点 8 之前从 draft_path.parent.parent/snapshots/snapshot2 读
        return "node_08_add_subtitles"

    retry = (state.get("retry_counts") or {}).get("node_07", 0)
    if retry >= NODE_07_MAX_RETRY:
        return "escalate_guardrail_failure"
    return "node_07_speed_fit"


def get_snapshot2_path(state: WorkflowState) -> str | None:
    """节点 8 之前调用:从文件系统读 snapshot2 路径,写入 state 供后续节点使用。"""
    raw_draft_path = state.get("draft_path")
    if not raw_draft_path:
        return None
    draft_path = Path(raw_draft_path)
    snap = draft_path.parent.parent / "snapshots" / "snapshot2" / "draft_content.json"
    return str(snap) if snap.exists() else None