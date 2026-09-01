"""node_15_localize_covers_en — 中文封面本地化为英文(Week 4 新增,对照计划 §4.3)。

职责:把中文封面 3 张分别本地化为英文版,产出 ``en_path`` 写入 state。

并行模式:``asyncio.gather`` 节点内并发(3 张互不依赖)。

降级策略(对齐计划 §4.3):某张图失败 → ``en_path=None``、累积 error_log,
**不**抛异常中断整图。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from draft_ops.atomic_writer_file import atomic_write_file
from mcp_clients.firered_image_edit_client import call_firered_edit
from state import WorkflowState


# ---------------------------------------------------------------------------
# 标题读取(节点 15 复用节点 14 的逻辑)
# ---------------------------------------------------------------------------
def _get_titles(state: WorkflowState) -> tuple[str, str]:
    """从 state['draft_path'] 的 draft_content.json 读 title_zh;title_en 由 mock 提供。

    Returns:
        (title_zh, title_en) 元组。
    """
    draft_path = state.get("draft_path")
    title_zh = "默认标题"
    if draft_path:
        try:
            draft = json.loads(Path(draft_path).read_text(encoding="utf-8"))
            texts = draft.get("materials", {}).get("texts", [])
            if texts and texts[0].get("content"):
                title_zh = str(texts[0]["content"])
        except (json.JSONDecodeError, OSError, KeyError, IndexError):
            pass
    # Week 4 用固定映射,Week 5 接翻译服务
    title_en = f"[EN] {title_zh}"
    return title_zh, title_en


def _build_prompt(title_zh: str, title_en: str, bbox: dict | None) -> str:
    """构造 FireRed-Image-Edit prompt,复用 text_bbox 位置信息。"""
    if bbox:
        return (
            f"Replace the Chinese title '{title_zh}' with the English text "
            f"'{title_en}', keeping the same position (x={bbox.get('x')}, "
            f"y={bbox.get('y')}, font_size={bbox.get('font_size')}) and visual style. "
            f"Output only the modified image."
        )
    return (
        f"Replace the Chinese title '{title_zh}' with the English text "
        f"'{title_en}', keeping the same position and visual style."
    )


# ---------------------------------------------------------------------------
# 单图本地化(同步函数,供 asyncio.to_thread 调用)
# ---------------------------------------------------------------------------
def _localize_one_cover(cover: dict, title_zh: str, title_en: str) -> str | None:
    """本地化单张封面 → 返回 en_path(失败返回 None)。

    失败语义(对齐计划 §4.3):en_path=None,调用方负责累积 error_log。
    """
    zh_path = Path(cover["zh_path"])
    if not zh_path.exists():
        return None

    # en_path 与 zh_path 同目录,替换 _zh → _en
    en_path = zh_path.with_name(zh_path.name.replace("_zh.png", "_en.png"))
    bbox = cover.get("text_bbox")
    prompt = _build_prompt(title_zh, title_en, bbox)

    try:
        call_firered_edit(
            input_image=zh_path,
            prompt=prompt,
            output_image=en_path,
        )
        return str(en_path)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def node_15_localize_covers_en(state: WorkflowState) -> dict:
    """LangGraph 节点:并发本地化 3 张封面。

    同步包装(同 ``node_14_make_covers``),便于 LangGraph ``g.invoke()`` 调用。
    """
    return asyncio.run(_node_15_localize_covers_en_async(state))


async def _node_15_localize_covers_en_async(state: WorkflowState) -> dict:
    """异步实现。"""
    covers = list(state.get("covers") or [])
    if not covers:
        return {"status_log": ["node_15_localize_covers_skipped"]}

    title_zh, title_en = _get_titles(state)
    en_paths = await asyncio.gather(*[
        asyncio.to_thread(_localize_one_cover, c, title_zh, title_en)
        for c in covers
    ])

    merged: list[dict] = []
    errors: list[str] = []
    for cover, en_path in zip(covers, en_paths):
        new_cover = dict(cover)
        new_cover["en_path"] = en_path
        merged.append(new_cover)
        if en_path is None:
            ratio = cover.get("ratio", "unknown")
            errors.append(f"[node_15] 封面本地化失败:ratio={ratio}")

    return {
        "covers": merged,
        "error_log": errors,
        "status_log": ["node_15_localize_covers_en_done"],
    }