"""节点 13:adjust_volume — 分角色音量 / 原生淡入淡出(阶段 5,2026-11-09 重写)。

旧实现的两处根因(阶段 0 基线实证 ``node_13_adjust_volume_no_audio_track``):

1. **selector 匹配永远落空**:按段的 ``name``/``type``/``audio_kind`` 匹配
   "audio_main"/"audio_bgm",但真实原生段(34 键)根本没有这些键 → 节点空转。
   现改为**按轨名识别角色**(轨名是剪映保存后仍在的字段):
   - 配音轨:``Voiceover_zh``(node_08 中文配音)/ ``en_voiceover``(node_17 标记);
   - BGM:其余全部 audio 轨(人工在剪映拖的 BGM 轨名不可控,不按名匹配);
   - 主音轨:video 轨段(原始画面自带声音)。
2. **字段是设计占位**:``audio_fades[].fade_in_duration_us/track_id`` 与
   audio material ``volume`` 均为 FIXME,剪映不识别。现改为仓库内已实证的
   原生口径:
   - 音量:段级 ``volume`` + ``last_nonzero_volume``(34 键样本原值,唯一经
     真实剪映 5.9.0.11632 样本验证的音量键);
   - 淡入淡出:原生 ``materials.audio_fades`` 条目
     (``jy_common/native_fx.build_native_audio_fade_material``,µs)+
     音频段 ``extra_material_refs`` 绑定(与转场/特效/贴纸同通道)。
     曲线类型按 linear,待 GUI diff 复核。

数值来源 ``config.NODE13_VOLUME_*`` / ``NODE13_FADE_*_SECONDS``(env 可覆盖)。
注意:节点按配置覆盖关卡② 人工调过的 BGM 音量(需要人工值时改配置)。

幂等:fade id 确定性(``audio-fade-{segment_id}``)+ upsert 覆盖;重放先剥离
我方旧 fade refs、清理孤儿 fade 素材。**守卫**:只改段的音量字段与 fade 引用,
不删轨、不移动/替换任何素材 —— 人工 BGM 轨本体不受影响。
"""

from __future__ import annotations

from pathlib import Path

from config import (
    NODE13_FADE_IN_SECONDS,
    NODE13_FADE_OUT_SECONDS,
    NODE13_VOLUME_BGM,
    NODE13_VOLUME_MAIN,
    NODE13_VOLUME_VOICEOVER,
)
from jy_common.native_fx import (
    OUR_AUDIO_FADE_PREFIX,
    build_native_audio_fade_material,
)
from nodes._draft_io import (
    jianying_running_tags,
    load_draft,
    upsert_by_id,
    write_draft,
)
from nodes._dual_draft import EN_VOICEOVER_TRACK_NAME, dual_draft_targets, missing_en_note
from state import WorkflowState


#: 配音轨名标记(node_08 中文配音轨 / node_17 英文配音轨)
VOICEOVER_TRACK_NAMES = ("Voiceover_zh", EN_VOICEOVER_TRACK_NAME)


def is_voiceover_track(track: dict) -> bool:
    """轨道是否为工作流配音轨(按轨名标记;node_12 镜像与 join QA 同口径)。"""
    return str(track.get("name") or "") in VOICEOVER_TRACK_NAMES


def _audio_tracks_by_role(draft: dict) -> dict[str, list[dict]]:
    """audio 轨按角色分组:voiceover(按轨名)/ bgm(其余)。"""
    voiceover: list[dict] = []
    bgm: list[dict] = []
    for track in draft.get("tracks", []) or []:
        if track.get("type") != "audio":
            continue
        (voiceover if is_voiceover_track(track) else bgm).append(track)
    return {"voiceover": voiceover, "bgm": bgm}


def _role_segments(draft: dict, role: str) -> list[dict]:
    """角色的全部 segments:main = video 轨段;bgm/voiceover = 对应 audio 轨段。"""
    if role == "main":
        for track in draft.get("tracks", []) or []:
            if track.get("type") == "video":
                return list(track.get("segments", []) or [])
        return []
    tracks = _audio_tracks_by_role(draft).get(role, [])
    return [seg for track in tracks for seg in (track.get("segments", []) or [])]


def _apply_volume_to_segment(seg: dict, volume_level: float) -> None:
    """段级音量(34 键样本实证键);last_nonzero_volume 仅在非零时同步。"""
    seg["volume"] = float(volume_level)
    if float(volume_level) > 0:
        seg["last_nonzero_volume"] = float(volume_level)


def _apply_fade_to_segment(
    draft: dict,
    seg: dict,
    fade_in_seconds: float,
    fade_out_seconds: float,
) -> dict | None:
    """为音频段写原生 fade 素材并绑定 ref;返回 fade 条目(段无 id 时 None)。"""
    seg_id = str(seg.get("id") or "")
    if not seg_id:
        return None
    fade_in_us = int(round(float(fade_in_seconds) * 1_000_000))
    fade_out_us = int(round(float(fade_out_seconds) * 1_000_000))
    # 钳制:淡入/淡出不超过段长(超长的 fade 剪映不生效)
    seg_duration_us = int((seg.get("target_timerange") or {}).get("duration") or 0)
    if seg_duration_us > 0:
        fade_in_us = min(fade_in_us, seg_duration_us)
        fade_out_us = min(fade_out_us, seg_duration_us)
    entry = build_native_audio_fade_material(
        material_id=f"{OUR_AUDIO_FADE_PREFIX}{seg_id}",
        fade_in_us=fade_in_us,
        fade_out_us=fade_out_us,
    )
    bucket = draft.setdefault("materials", {}).setdefault("audio_fades", [])
    upsert_by_id(bucket, entry)
    refs = seg.setdefault("extra_material_refs", [])
    if entry["id"] not in refs:
        refs.append(entry["id"])
    return entry


def _cleanup_stale_fades(draft: dict, kept_ids: set[str]) -> int:
    """清理未续用的我方 fade(素材 + BGM 段上的旧 refs),返回清理数。"""
    bucket = draft.setdefault("materials", {}).setdefault("audio_fades", [])
    removed = 0
    kept: list[dict] = []
    for item in bucket:
        item_id = str(item.get("id") or "")
        if item_id.startswith(OUR_AUDIO_FADE_PREFIX) and item_id not in kept_ids:
            removed += 1
            continue
        kept.append(item)
    bucket[:] = kept

    for track in _audio_tracks_by_role(draft).get("bgm", []):
        for seg in track.get("segments", []) or []:
            refs = seg.get("extra_material_refs")
            if not isinstance(refs, list):
                continue
            seg["extra_material_refs"] = [
                r for r in refs
                if not (str(r).startswith(OUR_AUDIO_FADE_PREFIX) and str(r) not in kept_ids)
            ]
    return removed


def jianying_adjust_volume(
    draft_path: str,
    role: str,
    volume_level: float | None = None,
    fade_in_seconds: float | None = None,
    fade_out_seconds: float | None = None,
) -> dict | None:
    """按角色应用音量与淡入淡出;返回实际写入的 audio_fade 条目(或 None)。

    Args:
        draft_path: 草稿 JSON 文件路径。
        role: ``"main"``(video 轨段)/ ``"bgm"``(非配音 audio 轨)/
            ``"voiceover"``(配音轨)。
        volume_level / fade_in_seconds / fade_out_seconds: None 时按角色取
            config 缺省(main 1.0 无 fade;bgm 0.35 + 2s/3s;voiceover 1.0)。

    Raises:
        FileNotFoundError: draft_path 不存在。
        json.JSONDecodeError: 草稿不是合法 JSON。
    """
    entry, _write_result = _adjust_volume_with_write(
        draft_path,
        role,
        volume_level=volume_level,
        fade_in_seconds=fade_in_seconds,
        fade_out_seconds=fade_out_seconds,
    )
    return entry


#: 角色缺省音量/淡入淡出(计划文档 §4.8 口径:main 1.0 无 fade,bgm 0.35 + 2s/3s)
_ROLE_DEFAULTS: dict[str, dict] = {
    "main": {"volume": NODE13_VOLUME_MAIN, "fade_in": 0.0, "fade_out": 0.0},
    "bgm": {
        "volume": NODE13_VOLUME_BGM,
        "fade_in": NODE13_FADE_IN_SECONDS,
        "fade_out": NODE13_FADE_OUT_SECONDS,
    },
    "voiceover": {"volume": NODE13_VOLUME_VOICEOVER, "fade_in": 0.0, "fade_out": 0.0},
}


def _adjust_volume_with_write(
    draft_path: str,
    role: str,
    volume_level: float | None = None,
    fade_in_seconds: float | None = None,
    fade_out_seconds: float | None = None,
) -> tuple[dict | None, dict | None]:
    """:func:`jianying_adjust_volume` 的内部实现,额外回传 ``safe_write_draft`` 结果。

    拆分原因见旧版注释:节点层要读 ``safe_write_draft`` 的 ``jianying_running``
    标志写告警,而公开 API 的返回契约(被单测/集成测试直接断言)不能改成元组。

    Returns:
        ``(written_entry, write_result)``。角色无任何 segment 时 ``written_entry``
        为 None 且**不发生写入**,``write_result`` 也是 None。
    """
    defaults = _ROLE_DEFAULTS.get(role)
    if defaults is None:
        raise ValueError(f"[node_13] 未知角色: {role!r}(可选 main/bgm/voiceover)")
    level = float(defaults["volume"] if volume_level is None else volume_level)
    fade_in = float(defaults["fade_in"] if fade_in_seconds is None else fade_in_seconds)
    fade_out = float(defaults["fade_out"] if fade_out_seconds is None else fade_out_seconds)

    draft_file = Path(draft_path)
    draft = load_draft(draft_file)

    segments = _role_segments(draft, role)
    if not segments and role != "bgm":
        return None, None

    written_entry: dict | None = None
    kept_fade_ids: set[str] = set()
    for seg in segments:
        _apply_volume_to_segment(seg, level)
        if role == "bgm" and (fade_in > 0 or fade_out > 0):
            entry = _apply_fade_to_segment(draft, seg, fade_in, fade_out)
            if entry is not None:
                kept_fade_ids.add(entry["id"])
                written_entry = entry

    if role == "bgm":
        # fade 管理权只在 bgm 角色:即使 BGM 段被删空也要清掉孤儿 fade
        removed = _cleanup_stale_fades(draft, kept_fade_ids)
        if not segments and removed == 0:
            return None, None

    write_result = write_draft(draft_file, draft, audit_node="node_13")
    return written_entry, write_result


# ---------------------------------------------------------------------------
# LangGraph 节点包装
# ---------------------------------------------------------------------------
def _try_role(
    state: WorkflowState,
    role: str,
    draft_file: str | Path | None = None,
    log_prefix: str = "node_13",
) -> tuple[dict | None, dict | None]:
    """包装单角色调整,草稿文件不存在时把错误写进 ``state.error_log``。"""
    target = draft_file if draft_file is not None else state.get("draft_path")
    try:
        return _adjust_volume_with_write(target, role)
    except FileNotFoundError:
        err = list(state.get("error_log", []) or [])
        err.append(f"[{log_prefix}] 草稿文件不存在: {target}")
        state["error_log"] = err  # type: ignore[index]
        return None, None


def adjust_volume(state: WorkflowState) -> dict:
    """LangGraph 节点函数:对 main / bgm / voiceover 三个角色各执行一次。

    K2=B(2026-10):对**中文主草稿 + 英文分支草稿**各跑一遍(见
    ``nodes/_dual_draft.py``)。两份草稿在同一节点内顺序处理,不跨超步,
    ``volume_adjusted`` / ``status_log`` / ``audio_fade_targets`` 的语义不变。
    无任何可调段时降级通过 + error_log,不抛异常。
    """
    draft_path = state.get("draft_path")
    audio_fade_targets: list[dict] = list(state.get("audio_fade_targets", []) or [])
    status_log = list(state.get("status_log", []) or [])

    if not draft_path:
        state.setdefault("error_log", []).append("[node_13] state.draft_path 缺失,跳过音量调整")
        status_log.append("node_13_adjust_volume_skipped")
        return {
            **state,
            "volume_adjusted": False,
            "status_log": status_log,
            "audio_fade_targets": audio_fade_targets,
        }

    targets = dual_draft_targets(state)
    if not targets:
        state.setdefault("error_log", []).append("[node_13] state.draft_path 缺失,跳过音量调整")
        status_log.append("node_13_adjust_volume_skipped")
        return {
            **state,
            "volume_adjusted": False,
            "status_log": status_log,
            "audio_fade_targets": audio_fade_targets,
        }

    has_en_dir = bool(state.get("draft_dir_en_branch"))
    adjusted_any = False
    write_results: list[dict | None] = []

    for tag, draft_file in targets:
        prefix = f"node_13_{tag}"
        # 角色与缺省值(计划文档 §4.8):main 1.0 无 fade;bgm 0.35 + 2s/3s;voiceover 1.0
        for role in ("main", "bgm", "voiceover"):
            entry, write_result = _try_role(
                state, role, draft_file=draft_file, log_prefix=prefix
            )
            if entry is not None:
                audio_fade_targets.append(entry)
            adjusted_any = adjusted_any or entry is not None
            write_results.append(write_result)

    if not adjusted_any:
        existing_errors = list(state.get("error_log", []) or [])
        if not any("audio" in e or "video" in e for e in existing_errors):
            existing_errors.append("[node_13] 草稿无 audio track(可能关卡②未添加),降级通过")
            state["error_log"] = existing_errors  # type: ignore[index]

    if len(targets) == 1 and has_en_dir:
        err = list(state.get("error_log", []) or [])
        err.append(missing_en_note(state))
        state["error_log"] = err  # type: ignore[index]

    status_log.append(
        "node_13_adjust_volume_done" if adjusted_any else "node_13_adjust_volume_no_audio_track"
    )
    if len(targets) == 2 and adjusted_any:
        status_log.append("node_13_adjust_volume_en_done")
    # 本节点多次写入(3 角色 × 中英两份)只产出一条剪映在跑告警。
    status_log.extend(jianying_running_tags("node_13", write_results))
    return {
        **state,
        "volume_adjusted": adjusted_any,
        "status_log": status_log,
        "audio_fade_targets": audio_fade_targets,
    }
