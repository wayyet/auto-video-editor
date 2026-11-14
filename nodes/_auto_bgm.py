"""自动铺轨 helper:向原生剪映草稿注入云音乐 BGM(20261114 阶段4,修订 §2.14/§4.5)。

职责边界:本模块只负责"把选好的云音乐素材变成原生 audio 轨",不负责选曲
(``storyline_select_bgm``)、不负责写盘(node_05 的 writer 原子双写)、不负责
音量/fade 终值(node_13)。node_05 接线在阶段5(本次不执行)。

事务式失败语义(修订 §2.14):
- 全程在 ``deepcopy`` 副本上构造;素材/轨段/伴随/校验全部通过后才返回副本;
- 任何一步失败,原 ``draft`` 保持完全不变,只返回结构化 errors;
- 幂等:同 (job_id, music_id) 重放 → 确定性 UUID → 先移除本工具生成的 BGM 轨
  (name="BGM",客户端人工轨 name="" 不受影响)再按相同 ID 重新插入。

形状依据(阶段0黄金样本盘点,执行记录 §1.2):
- 37 键 type="music" 素材,music_id/request_id/category_id/duration 原样保留;
- segment 34 键 + speed 伴随(复用 ``build_native_audio_segment``);
- SCM(4键,type="none")与 vocal_separation(5键)伴随;
- fade 不造(node_13 upsert);beats 按 D5 省略(en_branch 镜像先例兜底)。
- 短音乐循环铺轨(用户已拍板):按源音乐长度重复 segment 铺满视频尾,末段裁剪。
"""
from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from config import NODE13_VOLUME_BGM
from nodes._draft_io import upsert_by_id
from storyline.native_draft import (
    _new_id,
    build_native_audio_segment,
    build_native_music_library_material,
    build_native_music_library_track,
    build_native_sound_channel_mapping_material,
    build_native_vocal_separation_material,
    is_native_draft,
    music_library_track_id,
)

logger = logging.getLogger(__name__)

#: 自动轨轨道名(node_13 非配音分类 / _bgm_segment_count / node_12 镜像的识别口径)
AUTO_BGM_TRACK_NAME = "BGM"

#: 伴随素材所在桶(移除旧自动轨时按段引用清理)
_COMPANION_BUCKETS = ("speeds", "sound_channel_mappings", "vocal_separations")


def _video_max_end_us(draft: dict[str, Any]) -> int:
    """视频轨最大 target 终点(µs);无视频轨返回 0。"""
    best = 0
    for t in draft.get("tracks") or []:
        if not isinstance(t, dict) or t.get("type") != "video":
            continue
        for s in t.get("segments") or []:
            tr = (s or {}).get("target_timerange") or {}
            try:
                best = max(best, int(tr.get("start") or 0) + int(tr.get("duration") or 0))
            except (TypeError, ValueError):
                continue
    return best


def _refs_resolvable(candidate: dict[str, Any], refs: list[str]) -> list[str]:
    """返回 candidate materials 中解析不到的 ref id 列表(空 = 全部可解析)。"""
    known: set[str] = set()
    for items in (candidate.get("materials") or {}).values():
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("id"):
                    known.add(str(it["id"]))
    return [r for r in refs if r not in known]


def _strip_material_ids(candidate: dict[str, Any], ids: set[str]) -> None:
    """从 materials 桶里移除指定 id 的条目(清理被替换旧轨的孤立素材)。"""
    if not ids:
        return
    mats = candidate.get("materials") or {}
    for bucket in ("audios",) + _COMPANION_BUCKETS:
        items = mats.get(bucket)
        if isinstance(items, list):
            mats[bucket] = [it for it in items if not (isinstance(it, dict) and str(it.get("id")) in ids)]


def apply_auto_bgm_track(
    *,
    draft: dict[str, Any],
    selection: dict[str, Any] | None,
    library_entry: dict[str, Any] | None,
    job_id: str = "",
) -> dict[str, Any]:
    """向原生草稿注入云音乐 BGM 轨(事务式,原 draft 不被部分修改)。

    Args:
        draft: 原生格式草稿(``is_native_draft`` 为真),只读,不被修改。
        selection: ``storyline_bgm_selection``(须含 music_id)。
        library_entry: 本机曲库归一化条目(须含 raw_material/cache_path)。
        job_id: 任务 ID(参与确定性 UUID 种子;空则只用 music_id)。

    Returns:
        ``{"applied": bool, "draft": dict|None, "music_id": str, "music_name": str,
        "track_id": str, "status_notes": list[str], "errors": list[str]}``
    """
    result: dict[str, Any] = {
        "applied": False,
        "draft": None,
        "music_id": "",
        "music_name": "",
        "track_id": "",
        "status_notes": [],
        "errors": [],
    }

    def _fail(msg: str) -> dict[str, Any]:
        result["errors"].append(msg)
        logger.warning("[auto_bgm] %s", msg)
        return result

    # ---- 守卫(fail-closed,原 draft 未动) ----
    if not isinstance(draft, dict) or not is_native_draft(draft):
        return _fail("草稿不是原生格式(缺 new_version 标记),不注入自动 BGM 轨")
    music_id = str((selection or {}).get("music_id") or "").strip()
    if not music_id:
        return _fail("selection 无 music_id,跳过自动铺轨")
    entry = library_entry or {}
    raw_material = entry.get("raw_material")
    if not isinstance(raw_material, dict) or not raw_material:
        return _fail(f"曲库条目缺 raw_material(music_id={music_id}),重新 harvest 后重试")
    cache_path = str(entry.get("cache_path") or "")
    try:
        cache_ok = bool(cache_path) and Path(cache_path).is_file() and Path(cache_path).stat().st_size > 0
    except OSError:
        cache_ok = False
    if not cache_ok:
        # 状态日志纪律(修订 §阶段7 任务6):error_log 会透出到监控/创建台,
        # 不写本机缓存绝对路径,只留文件名供定位。
        return _fail(
            f"缓存文件不可用({Path(cache_path).name if cache_path else '未配置'}),"
            "跳过自动铺轨(素材可能已被清理)"
        )
    music_duration_us = int(entry.get("duration_us") or raw_material.get("duration") or 0)
    if music_duration_us <= 0:
        return _fail(f"音乐时长非法({music_duration_us}µs),拒绝铺轨")

    video_end_us = _video_max_end_us(draft)
    if video_end_us <= 0:
        return _fail("草稿无视频轨/视频轨终点为 0,无处铺轨")

    # ---- 在副本上构造 ----
    candidate = deepcopy(draft)
    seed = f"{job_id}:{music_id}" if job_id else music_id
    track_id = music_library_track_id(seed)

    # 幂等:先移除本工具生成的旧 BGM 轨(name="BGM" 或同确定性 id),
    # 并清理其孤立素材;客户端人工轨 name="" 不受影响。
    removed_ids: set[str] = set()
    kept_tracks: list[dict[str, Any]] = []
    for t in candidate.get("tracks") or []:
        if (
            isinstance(t, dict)
            and t.get("type") == "audio"
            and (t.get("name") == AUTO_BGM_TRACK_NAME or t.get("id") == track_id)
        ):
            for s in t.get("segments") or []:
                removed_ids.add(str((s or {}).get("material_id") or ""))
                removed_ids.update(str(r) for r in (s or {}).get("extra_material_refs") or [])
            continue
        kept_tracks.append(t)
    _strip_material_ids(candidate, removed_ids)
    candidate["tracks"] = kept_tracks

    # 1. 素材(fail-closed 校验在 builder 内,守卫已预检,这里只会成功)
    material = build_native_music_library_material(
        id_seed=seed, raw_material=raw_material, cache_path=cache_path
    )
    material_id = material["id"]

    # 2. 循环铺轨:按源音乐长度重复 segment 铺满视频尾,末段裁剪
    segments: list[dict[str, Any]] = []
    speeds: list[dict[str, Any]] = []
    scms: list[dict[str, Any]] = []
    vocals: list[dict[str, Any]] = []
    written = 0
    idx = 0
    while written < video_end_us:
        piece = min(music_duration_us, video_end_us - written)
        seg, companions = build_native_audio_segment(
            id_seed=f"{seed}:seg{idx}",
            material_id=material_id,
            start_us=written,
            duration_us=piece,
        )
        seg["volume"] = NODE13_VOLUME_BGM
        if NODE13_VOLUME_BGM > 0:
            seg["last_nonzero_volume"] = NODE13_VOLUME_BGM
        scm_id = _new_id(seg["id"], "scm")
        vocal_id = _new_id(seg["id"], "vocal")
        seg["extra_material_refs"] = [c["id"] for c in companions] + [scm_id, vocal_id]
        segments.append(seg)
        speeds.extend(companions)
        scms.append(build_native_sound_channel_mapping_material(material_id=scm_id))
        vocals.append(build_native_vocal_separation_material(material_id=vocal_id))
        written += piece
        idx += 1

    # 3. 7 键原生轨(name="BGM")并替换/追加
    track = build_native_music_library_track(id_seed=seed, segments=segments)
    candidate.setdefault("tracks", []).append(track)

    # 4. materials 并桶(幂等 upsert)
    mats = candidate.setdefault("materials", {})
    audios = mats.setdefault("audios", [])
    upsert_by_id(audios, material)
    for bucket, items in (
        ("speeds", speeds),
        ("sound_channel_mappings", scms),
        ("vocal_separations", vocals),
    ):
        bucket_list = mats.setdefault(bucket, [])
        for it in items:
            upsert_by_id(bucket_list, it)

    # ---- 校验(全部通过才替换) ----
    all_refs = [r for s in segments for r in s.get("extra_material_refs") or []]
    dangling = _refs_resolvable(candidate, all_refs)
    if dangling:
        return _fail(f"伴随素材引用悬空,放弃注入: {dangling}")
    seg_ids = [s["id"] for s in segments]
    if len(set(seg_ids)) != len(seg_ids):
        return _fail("segment ID 冲突,放弃注入")
    new_end = int(segments[-1]["target_timerange"]["start"]) + int(
        segments[-1]["target_timerange"]["duration"]
    )
    if new_end > video_end_us:
        return _fail(f"自动轨终点({new_end})超过视频轨终点({video_end_us}),放弃注入")
    if track.get("type") != "audio" or len(track) != 7 or track.get("name") != AUTO_BGM_TRACK_NAME:
        return _fail("轨道结构不是 7 键原生 BGM 轨,放弃注入")
    if str(material.get("music_id") or "") != music_id:
        return _fail("素材 music_id 与 selection 不一致,放弃注入")

    music_name = str(entry.get("name") or raw_material.get("name") or music_id)
    result.update(
        {
            "applied": True,
            "draft": candidate,
            "music_id": music_id,
            "music_name": music_name,
            "track_id": track_id,
            "status_notes": [
                f"自动铺轨: {music_name}({music_id}) "
                f"{len(segments)}段 target={new_end / 1_000_000:.2f}s "
                f"volume={NODE13_VOLUME_BGM}(fade 由 node_13 统一应用)"
            ],
        }
    )
    return result


__all__ = ["AUTO_BGM_TRACK_NAME", "apply_auto_bgm_track"]
