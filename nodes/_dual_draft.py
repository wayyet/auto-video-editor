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
    "dual_draft_targets",
    "en_draft_file",
    "missing_en_note",
    "en_draft_file_missing_path",
]