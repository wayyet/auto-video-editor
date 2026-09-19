"""节点 13:adjust_volume — 音量 / 淡入淡出真实实现(Week 3 补全)。

参考计划文档 §4.8 的 `/jianying-adjust-volume` 从零设计 Skill。本实现把
"placeholder → real"的字段名标定责任显式留给用户:

# FIXME 字段名逆向 TODO(Week 4 用户行动项):
#   本机当前无法启动剪映 v5.9.0 客户端做 ``draft_content.json`` diff 逆向,
#   因此本文件中 ``materials.audio_fades[]`` 与 audio_material.volume 字段
#   名为**设计占位**。Week 4 用户需:
#     1. 备份 ``drafts/default/draft_content.json``
#     2. 剪映 v5.9.0 客户端内手工给音频片段加淡入 2s / 淡出 3s / 音量 -6dB,保存
#     3. ``diff`` 出真实字段名
#     4. 把 ``_PLACEHOLDER_FADE_IN_KEY`` / ``_PLACEHOLDER_FADE_OUT_KEY``
#        / ``_PLACEHOLDER_VOLUME_KEY`` / ``_PLACEHOLDER_TRACK_ID_KEY`` 替换为
#        实际键名(详见下文)。

设计占位字段名:
- audio_fades[].fade_in_duration_us  →  实际剪映键名待逆向
- audio_fades[].fade_out_duration_us →  实际剪映键名待逆向
- audio_fades[].track_id             →  实际剪映键名待逆向
- audio_material.volume              →  实际剪映键名待逆向

降级策略:
- 草稿无 ``tracks[type="audio"]`` → 写 error_log + volume_adjusted=False,通过
- audio material 缺失 → 写 error_log + 通过
- 写入失败 → 抛异常(原子写入契约:目标文件不被污染)

调用入口与计划文档 §4.8 一致:
    jianying_adjust_volume(state["draft_path"], "audio_main", volume_level=1.0)
    jianying_adjust_volume(state["draft_path"], "audio_bgm",  volume_level=0.35,
                            fade_in_seconds=2.0, fade_out_seconds=3.0)
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from draft_ops.atomic_writer import safe_write_draft
from state import WorkflowState


# ---------------------------------------------------------------------------
# 设计占位字段名 — 用户完成剪映客户端逆向后回填
# ---------------------------------------------------------------------------
_PLACEHOLDER_FADE_IN_KEY = "fade_in_duration_us"        # FIXME 字段名待逆向
_PLACEHOLDER_FADE_OUT_KEY = "fade_out_duration_us"      # FIXME 字段名待逆向
_PLACEHOLDER_VOLUME_KEY = "volume"                     # FIXME 字段名待逆向
_PLACEHOLDER_TRACK_ID_KEY = "track_id"                 # FIXME 字段名待逆向


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _load_draft(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_audio_segments(draft: dict) -> list[dict]:
    """返回 ``tracks[type='audio'].segments`` 列表;无音频轨时返回空列表。"""
    for track in draft.get("tracks", []):
        if track.get("type") == "audio":
            return list(track.get("segments", []) or [])
    return []


def _find_audio_material_by_id(draft: dict, segment_id: str) -> dict | None:
    """在 ``materials.audios`` 中按 id 找对应的 audio material 对象。"""
    for mat in draft.get("materials", {}).get("audios", []) or []:
        if mat.get("id") == segment_id:
            return mat
    return None


def _audio_fades_list(draft: dict) -> list[dict]:
    return draft.setdefault("materials", {}).setdefault("audio_fades", [])


def _apply_volume(audio_material: dict, volume_level: float) -> None:
    """设置 audio material 的 volume 字段(占位字段名)。"""
    audio_material[_PLACEHOLDER_VOLUME_KEY] = float(volume_level)


def _apply_fade(
    audio_fades: list[dict],
    audio_segment_id: str,
    fade_in_seconds: float,
    fade_out_seconds: float,
) -> dict:
    """追加一条 audio_fade 条目,返回新条目(便于 state 记录)。"""
    entry = {
        "id": f"audio-fade-{uuid4().hex[:8]}",
        _PLACEHOLDER_TRACK_ID_KEY: audio_segment_id,
        _PLACEHOLDER_FADE_IN_KEY: int(fade_in_seconds * 1_000_000),
        _PLACEHOLDER_FADE_OUT_KEY: int(fade_out_seconds * 1_000_000),
        "_fade_in_curve_type": "linear",     # FIXME 曲线类型枚举待逆向
        "_fade_out_curve_type": "linear",    # FIXME 曲线类型枚举待逆向
    }
    audio_fades.append(entry)
    return entry


# ---------------------------------------------------------------------------
# 公开 API:与计划文档 §4.8 签名一致
# ---------------------------------------------------------------------------
def jianying_adjust_volume(
    draft_path: str,
    track_selector: str,
    volume_level: float = 1.0,
    fade_in_seconds: float = 0.0,
    fade_out_seconds: float = 0.0,
) -> dict | None:
    """应用音量与淡入淡出到指定音轨;返回实际写入的 audio_fade 条目(或 None)。

    Args:
        draft_path: 草稿 JSON 文件路径。
        track_selector: "audio_main" / "audio_bgm" — 按 ``segments[].name`` 匹配。
        volume_level: 0.0~1.0 线性音量(占位口径,实际剪映口径待逆向)。
        fade_in_seconds: 淡入时长(秒)。
        fade_out_seconds: 淡出时长(秒)。

    Returns:
        实际写入的 audio_fades 条目 dict(便于 state 记录);无匹配音轨时返回 None。

    Raises:
        FileNotFoundError: draft_path 不存在。
        json.JSONDecodeError: 草稿不是合法 JSON(原子写入前的合法性校验捕获)。
    """
    draft_file = Path(draft_path)
    draft = _load_draft(draft_file)

    segments = _find_audio_segments(draft)
    # 优先按 segment.name 匹配;无 name 时退到按 type 字符串匹配 selector
    matched: list[dict] = [
        s for s in segments
        if s.get("name") == track_selector
        or s.get("type") == track_selector
        or s.get("audio_kind") == track_selector
    ]
    if not matched:
        return None

    audio_fades = _audio_fades_list(draft)
    written_entry: dict | None = None

    for seg in matched:
        seg_id = seg.get("id")
        audio_mat = _find_audio_material_by_id(draft, seg_id) if seg_id else None
        if audio_mat is not None:
            _apply_volume(audio_mat, volume_level)
        if fade_in_seconds > 0 or fade_out_seconds > 0:
            if seg_id is None:
                continue
            written_entry = _apply_fade(audio_fades, seg_id, fade_in_seconds, fade_out_seconds)

    # Week 5:从旧单写入口切到 safe_write_draft 双写(draft_dir 由 draft_file.parent 提供)。
    safe_write_draft(draft_file.parent, draft)
    return written_entry


# ---------------------------------------------------------------------------
# LangGraph 节点包装
# ---------------------------------------------------------------------------
def _try_audio_track(
    state: WorkflowState,
    track_selector: str,
    volume_level: float,
    fade_in_seconds: float = 0.0,
    fade_out_seconds: float = 0.0,
) -> dict | None:
    """包装 jianying_adjust_volume,失败时把错误写到 state.error_log。"""
    try:
        return jianying_adjust_volume(
            state["draft_path"],
            track_selector,
            volume_level=volume_level,
            fade_in_seconds=fade_in_seconds,
            fade_out_seconds=fade_out_seconds,
        )
    except FileNotFoundError:
        err = list(state.get("error_log", []) or [])
        err.append(f"[node_13] 草稿文件不存在: {state.get('draft_path')}")
        state["error_log"] = err  # type: ignore[index]
        return None


def adjust_volume(state: WorkflowState) -> dict:
    """LangGraph 节点函数:对主音轨 + BGM 各执行一次音量/淡入淡出。"""
    draft_path = state.get("draft_path")
    audio_fade_targets: list[dict] = list(state.get("audio_fade_targets", []) or [])
    status_log = list(state.get("status_log", []) or [])

    if not draft_path:
        # 在 state 上直接追加,避免与 _try_audio_track 重复写 error_log
        state.setdefault("error_log", []).append("[node_13] state.draft_path 缺失,跳过音量调整")
        status_log.append("node_13_adjust_volume_skipped")
        return {
            **state,
            "volume_adjusted": False,
            "status_log": status_log,
            "audio_fade_targets": audio_fade_targets,
        }

    # 主音轨:音量 1.0,无淡入淡出(占位惯例 — 计划文档 §4.8)
    main_entry = _try_audio_track(state, "audio_main", volume_level=1.0)

    # BGM:音量 0.35,fade-in 2s,fade-out 3s(计划文档 §4.8)
    bgm_entry = _try_audio_track(
        state,
        "audio_bgm",
        volume_level=0.35,
        fade_in_seconds=2.0,
        fade_out_seconds=3.0,
    )
    if bgm_entry is not None:
        audio_fade_targets.append(bgm_entry)

    # 仅当任一通道实际写入成功时,volume_adjusted=True
    adjusted = main_entry is not None or bgm_entry is not None
    if not adjusted:
        # 不覆盖 _try_audio_track 已写入的错误信息(如 FileNotFoundError)
        existing_errors = list(state.get("error_log", []) or [])
        if not any("audio track" in e for e in existing_errors):
            existing_errors.append("[node_13] 草稿无 audio track(可能关卡②未添加),降级通过")
            state["error_log"] = existing_errors  # type: ignore[index]

    status_log.append(
        "node_13_adjust_volume_done" if adjusted else "node_13_adjust_volume_no_audio_track"
    )
    return {
        **state,
        "volume_adjusted": adjusted,
        "status_log": status_log,
        "audio_fade_targets": audio_fade_targets,
    }