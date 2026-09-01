"""节点 7:speed_fit — 护栏节点(分叉点,对应原文档 4.2 节)。

把"已存在"草稿的视频时间轴压到不超过目标时长(默认 35s)。
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
    TARGET_DURATION_US,
)
from draft_ops.atomic_writer import atomic_write_draft
from state import WorkflowState


# ---------------------------------------------------------------------------
# 公共工具:读/写 draft_content.json
# ---------------------------------------------------------------------------
def _load_draft(path: Path) -> dict:
    import json
    return json.loads(path.read_text(encoding="utf-8"))


def _find_video_track(draft: dict) -> dict:
    """Week 3 占位:取第一个 type=='video' 的轨道;Week 4 接入真实轨道结构时细化。"""
    for track in draft.get("tracks", []):
        if track.get("type") == "video":
            return track
    # fallback: 直接返回 tracks[0] 或空 dict
    tracks = draft.get("tracks", [])
    return tracks[0] if tracks else {"type": "video", "segments": []}


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
    draft_path = Path(state["draft_path"])
    draft = _load_draft(draft_path)
    video_track = _find_video_track(draft)

    src_segments = list(video_track.get("segments", []))
    fps = video_track.get("fps", NODE_07_DEFAULT_FPS)

    src_total_us = _compute_total_duration_us(video_track)
    # 已达标:直接返回(由条件边判断后产出 snapshot2 → node_08)
    if src_total_us <= TARGET_DURATION_US:
        # 仍递增重试计数便于联调断言(Week 3 兼容 Week 2 测试期望)
        retry = dict(state.get("retry_counts") or {})
        retry["node_07"] = retry.get("node_07", 0) + 1
        return {**state, "retry_counts": retry}

    src_durations = [int(seg["target_timerange"]["duration"]) for seg in src_segments]
    new_durations = _frame_aligned_durations(src_durations, TARGET_DURATION_US, fps)

    # 应用速度:写 segment.speed + materials.speeds[].speed + extra_material_refs
    materials = draft.setdefault("materials", {})
    speeds_material = materials.setdefault("speeds", [])

    for seg, new_dur in zip(src_segments, new_durations):
        src_dur = int(seg["target_timerange"]["duration"])
        speed = src_dur / new_dur if new_dur > 0 else 1.0
        seg["speed"] = speed

        # 补建 speed material 引用(若段无 extra_material_refs)
        refs = seg.setdefault("extra_material_refs", [])
        # 检查是否已有 speed 引用
        has_speed_ref = any(
            any(sm.get("id") == ref for sm in speeds_material)
            for ref in refs
        )
        if not has_speed_ref:
            mat_id = f"speed-{uuid4().hex[:8]}"
            speeds_material.append({
                "id": mat_id,
                "speed": speed,
                "mode": 0,           # 0 = 常规变速(曲线变速 mode != 0)
                "curve_speed": None,
            })
            refs.append(mat_id)

    # 重算 target_timerange.start + 顶层 duration
    cursor = 0
    for seg, new_dur in zip(src_segments, new_durations):
        seg["target_timerange"]["start"] = cursor
        seg["target_timerange"]["duration"] = new_dur
        cursor += new_dur
    video_track["segments"] = src_segments
    draft["duration"] = cursor

    atomic_write_draft(draft_path, draft)

    retry = dict(state.get("retry_counts") or {})
    retry["node_07"] = retry.get("node_07", 0) + 1

    return {**state, "retry_counts": retry}


# ---------------------------------------------------------------------------
# 条件边路由(由 graph.py 引用)
# ---------------------------------------------------------------------------
def route_after_speed_fit(state: WorkflowState) -> str:
    """节点 7 之后的条件边:达标 → 产出 snapshot2 + node_08;未达标 → 自循环 / 升级。"""
    draft_path = Path(state["draft_path"])
    draft = _load_draft(draft_path)
    duration = draft.get("duration", 0)

    if duration <= TARGET_DURATION_US:
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
    draft_path = Path(state["draft_path"])
    snap = draft_path.parent.parent / "snapshots" / "snapshot2" / "draft_content.json"
    return str(snap) if snap.exists() else None