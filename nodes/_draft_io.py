"""nodes 层草稿读写的公共样板(冗余收敛 R1 + R2)。

收敛两件逐字重复的样板,让"读-改-写"这条链在节点层只剩**业务差异**:

- **R1 读盘** — 6 份一字不差的 ``_load_draft``(node_07 / 08 / 09 / 10 / 11 / 13)
  统一为 :func:`load_draft`。
- **R2 写回** — 5 处 ``safe_write_draft(...)`` + "剪映进程在跑"告警拼接,统一为
  :func:`apply_and_write`(节点 08/09/10/11)与 :func:`jianying_running_tags`
  (节点 13:写入发生在公开 API :func:`nodes.node_13_adjust_volume.jianying_adjust_volume`
  内部,拿不到 ``write_result``,所以只复用告警标签生成)。

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
from pathlib import Path
from typing import Any, Iterable

from draft_ops.atomic_writer import safe_write_draft


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
# R2:写回
# ---------------------------------------------------------------------------
def write_draft(
    draft_path: Path | str,
    draft: dict[str, Any],
    *,
    duration_us: int | None = None,
) -> dict[str, Any]:
    """目录级双写草稿并返回 ``safe_write_draft`` 的结果字典。

    这里是全仓唯一允许把 ``draft_path`` 折成 ``draft_path.parent`` 的地方 ——
    收敛这条样板的同时,也把上面那条"入参陷阱"收敛成一处,新增节点不会再写错。

    Args:
        draft_path: 草稿**文件**路径;实际写入落到它的父目录。
        draft: 草稿内容 dict。
        duration_us: 给出时同步更新 ``draft_meta_info.json`` / ``root_meta_info.json``
            的 ``tm_duration``;``None`` 表示不更新(默认)。
    """
    return safe_write_draft(Path(draft_path).parent, draft, duration_us=duration_us)


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
        node_tag: 节点标签(``"node_08"``),告警前缀。
        done_tag: 完成标记(如 ``"node_08_add_subtitles_done"``)。
        duration_us: 可选,透传 :func:`write_draft` 同步时长索引。

    Returns:
        ``[done_tag]`` + 可能的剪映告警,顺序与重构前一致(告警在 done 之后)。
        调用方用法::

            log = list(state.get("status_log", []) or [])
            log.extend(apply_and_write(draft_path, draft, "node_10",
                                       done_tag="node_10_inject_text_fx_done"))
    """
    write_result = write_draft(draft_path, draft, duration_us=duration_us)
    return [done_tag, *jianying_running_tags(node_tag, (write_result,))]


__all__ = [
    "load_draft",
    "write_draft",
    "jianying_running_tags",
    "apply_and_write",
]
