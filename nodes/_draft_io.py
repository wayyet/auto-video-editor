"""nodes 层草稿读写的公共样板(冗余收敛 R1 + R2 + R5)。

收敛三件逐字重复的样板,让"读-改-写"这条链在节点层只剩**业务差异**:

- **R1 读盘** — 6 份一字不差的 ``_load_draft``(node_07 / 08 / 09 / 10 / 11 / 13)
  统一为 :func:`load_draft`。
- **R2 写回** — 5 处 ``safe_write_draft(...)`` + "剪映进程在跑"告警拼接,统一为
  :func:`apply_and_write`(节点 08/09/10/11)与 :func:`jianying_running_tags`
  (节点 13:写入发生在公开 API :func:`nodes.node_13_adjust_volume.jianying_adjust_volume`
  内部,拿不到 ``write_result``,所以只复用告警标签生成)。
- **R5 草稿结构小工具** — node_07 / 09 / 11 各抄一份的 ``_find_video_track`` 与
  ``_upsert_by_id``,统一为 :func:`find_video_track` 与 :func:`upsert_by_id`
  (后者统一返回 bool,True = 本次新增)。

⚠️ **入参陷阱(抽错会静默坏掉剪映 5.9+)**
:func:`write_draft` 传给 ``safe_write_draft`` 的是 ``draft_path.parent``(目录),
**不是** ``draft_path``(文件)。Week 5 把 ``safe_write_draft`` 的入参从文件级提升到
目录级,就是为了同时双写 ``draft_info.json``(剪映 5.9+ 要求它与
``draft_content.json`` 内容一致,否则缩略图与时间轴显示异常)。传成文件路径会退化成
在**名为 draft_content.json 的目录**下建子目录,写出
``.../draft_content.json/draft_content.json``,而 ``draft_info.json`` 永远缺失。

本模块只做 I/O 样板收敛,不含任何草稿业务字段知识;``status_log`` 的内容由调用方
决定(返回 tag 列表而非直接改 state),这样调用方仍能按自己的顺序追加别的 tag
(例如 node_08 的 scout 变更说明必须排在告警之后)。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from draft_ops.atomic_writer import safe_write_draft
from draft_ops.draft_lock import hash_draft_files

#: 节点级草稿修改审计文件(阶段 6b;点前缀,剪映不读;每次写入 append 一行)
AUDIT_FILE_NAME = ".autovideo_draft_mutations.jsonl"

#: 审计分桶的 materials 桶名(其余桶不 diff,防止计数噪声)
_AUDIT_BUCKETS: tuple[str, ...] = (
    "videos", "audios", "texts", "transitions", "video_effects",
    "stickers", "material_animations", "audio_fades", "speeds",
)


# ---------------------------------------------------------------------------
# R1:读盘
# ---------------------------------------------------------------------------
def load_draft(draft_file: Path | str) -> dict[str, Any]:
    """读 ``draft_content.json`` 并返回 dict。

    Args:
        draft_file: 草稿**文件**路径(``.../draft_content.json``)。允许传目录
            之外的任意 str/Path,调用方负责给对。

    Raises:
        FileNotFoundError: 文件不存在。
        json.JSONDecodeError: 内容不是合法 JSON。
    """
    return json.loads(Path(draft_file).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 阶段 6b:节点级草稿修改审计(draft_mutations)
# ---------------------------------------------------------------------------
def draft_role_of(draft_path: Path | str) -> str:
    """按路径推断草稿角色:英文分支目录(``en_branch_*``)→ ``en``,其余 ``zh``。"""
    return "en" if "en_branch" in Path(draft_path).as_posix() else "zh"


def _bucket_counts(draft: dict[str, Any] | None) -> dict[str, int]:
    """materials 各桶条目数 + tracks 段总数(轻量计数,免深比较)。"""
    if not isinstance(draft, dict):
        return {}
    materials = draft.get("materials") or {}
    counts = {
        f"materials.{bucket}": len(materials.get(bucket) or [])
        for bucket in _AUDIT_BUCKETS
        if isinstance(materials.get(bucket), list)
    }
    counts["track_segments"] = sum(
        len(t.get("segments") or [])
        for t in (draft.get("tracks") or [])
        if isinstance(t, dict)
    )
    return counts


def build_draft_mutation_record(
    node_id: str,
    draft_path: Path | str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    sha_before: dict[str, str] | None,
    sha_after: dict[str, str] | None,
    status: str = "succeeded",
    reason: str | None = None,
) -> dict[str, Any]:
    """构造一条审计记录(纯函数;落盘由 :func:`append_draft_mutation` 负责)。"""
    counts_before = _bucket_counts(before)
    counts_after = _bucket_counts(after)
    changed_counts = {
        key: {"before": counts_before.get(key, 0), "after": counts_after.get(key, 0)}
        for key in sorted(set(counts_before) | set(counts_after))
        if counts_before.get(key, 0) != counts_after.get(key, 0)
    }
    return {
        "node_id": node_id,
        "draft_role": draft_role_of(draft_path),
        "draft_path": str(draft_path),
        "sha256_before": (sha_before or {}).get("draft_content.json"),
        "sha256_after": (sha_after or {}).get("draft_content.json"),
        "changed": bool(changed_counts) or (
            (sha_before or {}).get("draft_content.json")
            != (sha_after or {}).get("draft_content.json")
        ),
        "changed_counts": changed_counts,
        "status": status,
        "reason": reason,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def append_draft_mutation(draft_dir: Path | str, record: dict[str, Any]) -> bool:
    """审计记录追加进 ``<draft_dir>/.autovideo_draft_mutations.jsonl``。

    审计是观测通道,**绝不阻断主流程**:任何 IO 异常吞掉返回 False。
    """
    try:
        path = Path(draft_dir) / AUDIT_FILE_NAME
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_draft_mutations(draft_dir: Path | str) -> list[dict[str, Any]]:
    """读回审计记录(文件缺失/损坏返回空列表;join QA 聚合用)。"""
    path = Path(draft_dir) / AUDIT_FILE_NAME
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
        except ValueError:
            continue
    return records


# ---------------------------------------------------------------------------
# R2:写回
# ---------------------------------------------------------------------------
def write_draft(
    draft_path: Path | str,
    draft: dict[str, Any],
    *,
    duration_us: int | None = None,
    audit_node: str | None = None,
) -> dict[str, Any]:
    """目录级双写草稿并返回 ``safe_write_draft`` 的结果字典。

    这里是全仓唯一允许把 ``draft_path`` 折成 ``draft_path.parent`` 的地方 ——
    收敛这条样板的同时,也把上面那条"入参陷阱"收敛成一处,新增节点不会再写错。

    Args:
        draft_path: 草稿**文件**路径;实际写入落到它的父目录。
        draft: 草稿内容 dict。
        duration_us: 给出时同步更新 ``draft_meta_info.json`` / ``root_meta_info.json``
            的 ``tm_duration``;``None`` 表示不更新(默认)。
        audit_node: 阶段 6b 审计节点名(如 ``"node_08"``)。给出时写前/写后各取
            一枚 SHA-256 并把修改记录追加进草稿目录的
            ``.autovideo_draft_mutations.jsonl``(失败降级,不阻断)。
    """
    target = Path(draft_path)
    before_content: dict[str, Any] | None = None
    sha_before: dict[str, str] | None = None
    if audit_node:
        try:
            before_content = load_draft(target)
        except (OSError, ValueError):
            before_content = None
        try:
            sha_before = hash_draft_files(target)
        except OSError:
            sha_before = None
    result = safe_write_draft(target.parent, draft, duration_us=duration_us)
    if audit_node:
        try:
            sha_after = hash_draft_files(target)
        except OSError:
            sha_after = None
        status = "succeeded" if result.get("ok") else "failed"
        record = build_draft_mutation_record(
            audit_node, target, before_content, draft,
            sha_before=sha_before, sha_after=sha_after, status=status,
            reason=result.get("reason") if status == "failed" else None,
        )
        append_draft_mutation(target.parent, record)
        result["mutation"] = record
    return result


def jianying_running_tags(node_tag: str, write_results: Iterable[Any]) -> list[str]:
    """把 ``safe_write_draft`` 返回值里的"剪映在跑"翻成 ``status_log`` 标签。

    剪映进程检测**不阻断**写入(草稿可能被客户端读一半),所以这里只记告警。
    多个 ``write_result``(节点 13 会连写两次:主音轨 + BGM)只产出一条告警,避免
    同一次节点执行刷两条一样的日志。

    Args:
        node_tag: 节点标签(``"node_08"``),用于告警前缀。
        write_results: 一个或多个 ``safe_write_draft`` 返回值;``None`` 与缺字段
            的字典会被安全忽略。

    Returns:
        告警标签列表(0 或 1 条)。
    """
    if any(bool(r and r.get("jianying_running")) for r in write_results):
        return [f"[{node_tag}] 剪映进程在跑,写入仍继续(告警不阻断)"]
    return []


def apply_and_write(
    draft_path: Path | str,
    draft: dict[str, Any],
    node_tag: str,
    *,
    done_tag: str,
    duration_us: int | None = None,
) -> list[str]:
    """写回草稿,并返回要追加进 ``state["status_log"]`` 的 tag 列表。

    覆盖原先 4 个节点(node_08 / 09 / 10 / 11)里这段逐字重复:
        write_result = safe_write_draft(draft_path.parent, draft)
        log = list(state.get("status_log", []) or []) + ["node_xx_xxx_done"]
        if write_result.get("jianying_running"):
            log.append("[node_xx] 剪映进程在跑,写入仍继续(告警不阻断)")

    Args:
        draft_path: 草稿文件路径(实际写入其父目录)。
        draft: 草稿内容 dict。
        node_tag: 节点标签(``"node_08"``),告警前缀;同时作为阶段 6b 审计的
            ``audit_node``(每次写入自动追加 draft_mutation 记录)。
        done_tag: 完成标记(如 ``"node_08_add_subtitles_done"``)。
        duration_us: 可选,透传 :func:`write_draft` 同步时长索引。

    Returns:
        ``[done_tag]`` + 可能的剪映告警,顺序与重构前一致(告警在 done 之后)。
        调用方用法::

            log = list(state.get("status_log", []) or [])
            log.extend(apply_and_write(draft_path, draft, "node_10",
                                       done_tag="node_10_inject_text_fx_done"))
    """
    write_result = write_draft(draft_path, draft, duration_us=duration_us, audit_node=node_tag)
    return [done_tag, *jianying_running_tags(node_tag, (write_result,))]


# ---------------------------------------------------------------------------
# R5:草稿结构小工具(原先 node_07 / 09 / 11 各抄一份)
# ---------------------------------------------------------------------------
def find_video_track(draft: dict) -> dict:
    """取第一条 ``type == "video"`` 的轨道。

    没有视频轨时退回第一条轨;连轨都没有则返回一条空的视频轨(不写回 draft)。
    """
    for track in draft.get("tracks", []):
        if track.get("type") == "video":
            return track
    tracks = draft.get("tracks", [])
    return tracks[0] if tracks else {"type": "video", "segments": []}


def find_text_segment_by_material_id(draft: dict, material_id: str) -> dict | None:
    """在所有 text 轨里找引用该字幕素材的段(原生轨道段 ``material_id`` 键)。

    node_10(动画绑定)/ node_11(贴纸时间范围)共用。找不到返回 None
    (私有形态草稿或字幕没有落轨)。
    """
    for track in draft.get("tracks", []) or []:
        if track.get("type") != "text":
            continue
        for seg in track.get("segments", []) or []:
            if str(seg.get("material_id") or "") == material_id:
                return seg
    return None


def upsert_by_id(items: list[dict], item: dict) -> bool:
    """按 ``id`` 覆盖写入;没有同 id 条目才追加。返回 True 表示本次是新增。

    幂等:同一条目重放多次不会叠出重复项。2026-10 K2=B 加固时,node_09 / node_11
    横跨关卡②、resume 路径变多,纯 append 会让 ``materials.transitions`` /
    ``video_effects`` / ``stickers`` 每重放一次就多一份。
    """
    item_id = item.get("id")
    for idx, existing in enumerate(items):
        if existing.get("id") == item_id:
            items[idx] = item
            return False
    items.append(item)
    return True


__all__ = [
    "AUDIT_FILE_NAME",
    "apply_and_write",
    "append_draft_mutation",
    "build_draft_mutation_record",
    "draft_role_of",
    "find_text_segment_by_material_id",
    "find_video_track",
    "jianying_running_tags",
    "load_draft",
    "read_draft_mutations",
    "upsert_by_id",
    "write_draft",
]
