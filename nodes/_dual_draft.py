"""K2=B 公共辅助:把"中英两份草稿"的定位收敛到一处(尾链 09/11/12/13 共用)。

背景(2026-10 调序):``node_09/11/12/13`` 被推迟到 ``node_17`` 之后,此时**两份**草稿
都已经是最终形态 ——

- 中文主草稿 ``state["draft_path"]``
- 英文分支草稿 ``state["draft_dir_en_branch"]/draft_content.json``

K2=B 要求这四个尾链节点**两份都生效**(不再只改中文),但不新增 09b/11b/12b/13b
四个英文实例节点:同一超步内并排写两个整份 state 会撞 LangGraph 的 LastValue
通道(``InvalidUpdateError``),而且关卡② 会被迫挂起两次(人工选两次曲)。

设计取舍:

- **在同一节点内部顺序处理两份草稿**,不跨超步 → 不产生新的并发写冲突。
- **英文草稿缺失 / 目录不存在 / 文件不存在 → 只返回中文那一份**,由调用方写
  ``error_log`` 降级通过。尾链不阻塞交付(与节点 09/11/13 既有的"模板库缺失
  降级""无音轨降级"同一档)。
- 草稿文件路径只在这里推导一次,避免 4 个节点各写一遍 ``Path(...) / "draft_content.json"``
  之后某天改错目录名。

用法::

    targets = dual_draft_targets(state)          # [(tag, draft_file), ...]
    if len(targets) == 1 and targets[0][0] == "zh":
        error_log.append("[node_xx] 英文草稿缺失,降级只处理中文草稿")
    for tag, draft_file in targets:
        ...
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# 英文分支草稿在目录下的固定文件名,与 nodes/node_08b_add_subtitles_en.py、
# nodes/node_10_inject_text_fx.py、nodes/node_16a_translate_and_check.py 共用同一约定。
_DRAFT_FILENAME = "draft_content.json"

# 中文标签:与 plan §2.3 的 done_tag 命名约定一致(``..._done`` / ``..._en_done``)
ZH = "zh"
EN = "en"

# P3b(2026-11-08):node_17 写入英文分支草稿的配音轨标记(track 与 segment 同名)。
# node_12 镜像按此标记做"按角色合并"(只替换镜像轨,保留配音轨);
# draft_ops.draft_integrity.check_audio_mirror 据此把配音轨排除出镜像一致性比对。
EN_VOICEOVER_TRACK_NAME = "en_voiceover"

# 2026-11-17 P0(英文分支配音隔离):node_08 写入中文主草稿的配音轨名(权威定义,
# node_08 原私有常量 _ZH_VOICEOVER_TRACK_NAME 改为引用此处)。英文分支 v2 策略
# (en_branch_audio_policy_version)以"轨名精确匹配"为唯一清理条件 —— node_fork
# 清理、node_12 镜像过滤、QA 语言隔离检查三方共用,禁止按文件名/路径含 zh 等
# 模糊条件删轨(人工 BGM 可能撞名)。
ZH_VOICEOVER_TRACK_NAME = "Voiceover_zh"

# 2026-11-17 P0:英文分支音频策略版本(node_fork 写
# state.en_branch_audio_policy_version)。"v2" = 本次运行新建/重放的英文分支
# 已按新策略清理中文配音,node_12 镜像过滤与 QA 语言隔离检查仅对 v2 生效;
# legacy = 历史目录(无标记),三方保持旧行为,不追溯改写历史任务草稿。
EN_BRANCH_AUDIO_POLICY_V2 = "v2"
EN_BRANCH_AUDIO_POLICY_LEGACY = "legacy"


def _norm_path_key(path: str) -> str:
    """路径归一化键(与 node_08._norm_path_key 同口径):统一分隔符 + 小写。"""
    return str(path or "").replace("\\", "/").strip().lower()


def _iter_track_refs(track: dict) -> set[str]:
    """收集一条轨道全部 segment 引用的素材 id(material_id + extra_material_refs)。"""
    refs: set[str] = set()
    for seg in track.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        mid = seg.get("material_id")
        if mid:
            refs.add(str(mid))
        for ref in seg.get("extra_material_refs") or []:
            if ref:
                refs.add(str(ref))
    return refs


def remove_chinese_voiceover_from_draft(draft: dict, storyline_root: str = "") -> dict:
    """从草稿 dict 移除中文配音轨与链产孤儿配音素材(纯函数,就地改 draft)。

    2026-11-17 P0(英文分支配音隔离)。清理对象(修订计划 §4.2):
    1. **轨**:``type == "audio"`` 且 ``name == ZH_VOICEOVER_TRACK_NAME`` ——
       唯一权威匹配条件,禁止按文件名/路径模糊删轨;
    2. **素材**:被删轨引用、且移除后全草稿引用归零的 materials 条目
       (audios 及 speeds/audio_fades 等 companion 桶一并回收);
    3. **链产孤儿素材**(可选,``storyline_root`` 非空时):无任何轨道段引用、
       但素材路径位于本次运行 storyline 输出目录内的 audio 条目 —— snapshot②
       实证 node_05 时代会把 voiceover WAV 写进 materials.audios 却不铺轨,
       剪映素材面板可见,属用户可感知的"中文配音残留"。

    不删除磁盘上的 WAV 文件(素材可能被中文主草稿/其它任务引用),不落盘 ——
    调用方负责 ``write_draft`` 与回读校验。

    Returns:
        摘要 dict:``{"removed_tracks", "removed_segments", "removed_materials",
        "removed_material_ids", "changed"}``。
    """
    tracks = draft.get("tracks") or []
    removed_tracks = [
        t
        for t in tracks
        if isinstance(t, dict)
        and t.get("type") == "audio"
        and t.get("name") == ZH_VOICEOVER_TRACK_NAME
    ]
    removed_ref_ids: set[str] = set()
    removed_segments = 0
    for t in removed_tracks:
        removed_ref_ids |= _iter_track_refs(t)
        removed_segments += len(t.get("segments") or [])

    if removed_tracks:
        draft["tracks"] = [
            t
            for t in tracks
            if not (
                isinstance(t, dict)
                and t.get("type") == "audio"
                and t.get("name") == ZH_VOICEOVER_TRACK_NAME
            )
        ]
        tracks = draft["tracks"]

    # 剩余轨道的全部引用(判孤儿/判共享的唯一依据)
    still_referenced: set[str] = set()
    for t in tracks:
        if not isinstance(t, dict):
            continue
        still_referenced |= _iter_track_refs(t)

    materials = draft.setdefault("materials", {})
    removed_material_ids: list[str] = []
    root_key = _norm_path_key(storyline_root)
    for bucket_name, bucket in list(materials.items()):
        if not isinstance(bucket, list):
            continue
        kept: list[Any] = []
        bucket_changed = False
        for m in bucket:
            if not isinstance(m, dict):
                kept.append(m)
                continue
            mid = str(m.get("id") or "")
            drop = False
            if mid and mid in removed_ref_ids and mid not in still_referenced:
                drop = True  # 被删轨引用、现已无主
            elif (
                bool(mid)  # 无 id 条目不回收,保守
                and mid not in still_referenced
                and bucket_name == "audios"
                and root_key
                and _norm_path_key(str(m.get("path") or "")).startswith(root_key)
            ):
                drop = True  # 链产孤儿配音素材(snapshot② 实证形态)
            if drop:
                removed_material_ids.append(mid)
                bucket_changed = True
            else:
                kept.append(m)
        if bucket_changed:
            materials[bucket_name] = kept

    return {
        "removed_tracks": len(removed_tracks),
        "removed_segments": removed_segments,
        "removed_materials": len(removed_material_ids),
        "removed_material_ids": removed_material_ids,
        "changed": bool(removed_tracks or removed_material_ids),
    }


def en_draft_file(state: dict[str, Any]) -> Path | None:
    """返回英文分支草稿文件路径;``draft_dir_en_branch`` 缺失或文件不存在时返回 ``None``。

    只判存在性,不读盘 —— 调用方拿到 ``None`` 时按"降级只处理中文"处理。
    """
    draft_dir_raw = state.get("draft_dir_en_branch")
    if not draft_dir_raw:
        return None
    draft_file = Path(draft_dir_raw) / _DRAFT_FILENAME
    return draft_file if draft_file.exists() else None


def dual_draft_targets(state: dict[str, Any]) -> list[tuple[str, Path]]:
    """返回 ``[(tag, draft_file), ...]``,中文永远第一个。

    中文草稿:``state["draft_path"]``(不判存在性 —— 缺失是上游故障,应由调用方抛错,
    与重构前行为一致:``node_09/11`` 原本就对中文草稿直接 ``load_draft``,
    ``FileNotFoundError`` 会冒泡)。

    英文草稿:``state["draft_dir_en_branch"]/draft_content.json``,**存在才返回**;
    缺失时列表只含中文那一份(降级,不抛异常)。

    同一份路径只出现一次:若调用方误把 ``draft_dir_en_branch`` 指到中文草稿目录,
    会被去重成一份,避免同节点对同一文件写两次。
    """
    targets: list[tuple[str, Path]] = []

    zh_raw = state.get("draft_path")
    if zh_raw:
        targets.append((ZH, Path(zh_raw)))

    en_file = en_draft_file(state)
    if en_file is not None:
        resolved_zh = str(Path(zh_raw).resolve()) if zh_raw else None
        if resolved_zh is None or str(en_file.resolve()) != resolved_zh:
            targets.append((EN, en_file))

    return targets


def missing_en_note(state: dict[str, Any]) -> str | None:
    """英文草稿不可用时给 ``error_log`` 的说明;可用则返回 ``None``。

    统一措辞,方便 grep 日志时一眼分辨"降级"与"正常"。
    """
    if state.get("draft_dir_en_branch") is None:
        return "[dual_draft] draft_dir_en_branch 缺失,降级只处理中文草稿"
    return f"[dual_draft] 英文草稿不存在,降级只处理中文草稿: {en_draft_file_missing_path(state)}"


def en_draft_file_missing_path(state: dict[str, Any]) -> str:
    draft_dir_raw = state.get("draft_dir_en_branch") or "<未设置>"
    return str(Path(draft_dir_raw) / _DRAFT_FILENAME)


__all__ = [
    "ZH",
    "EN",
    "EN_VOICEOVER_TRACK_NAME",
    "ZH_VOICEOVER_TRACK_NAME",
    "EN_BRANCH_AUDIO_POLICY_V2",
    "EN_BRANCH_AUDIO_POLICY_LEGACY",
    "dual_draft_targets",
    "en_draft_file",
    "missing_en_note",
    "en_draft_file_missing_path",
    "remove_chinese_voiceover_from_draft",
]