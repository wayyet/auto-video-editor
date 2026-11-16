"""node_15_localize_covers_en — 中文封面本地化为英文(Week 4 新增,对照计划 §4.3)。

职责:把中文封面 3 张分别本地化为英文版,产出 ``en_path`` 写入 state。

并行模式:``asyncio.gather`` 节点内并发(3 张互不依赖)。

降级策略(对齐计划 §4.3):某张图失败 → ``en_path=None``、累积 error_log,
**不**抛异常中断整图。

2026-11-17 P2(封面标题真翻译,修订计划 §5.6):
- 标题来源与 node_14 收敛到 ``nodes._cover_title.resolve_zh_title``
  (文本轨最早 start,不依赖 ``materials.texts[0]`` 数组顺序);
- 英文标题走默认翻译客户端**真实翻译**,``[EN] `` 前缀拼接删除;
- 翻译不可用(stub)/失败 → ``cover_translation_status="degraded"``、全部
  ``en_path=None`` + error_log —— **绝不生成伪英文标题**。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from jy_common.translate_client import LLMTranslateClient, get_default_client
from mcp_clients.firered_image_edit_client import call_firered_edit
from nodes._cover_title import resolve_zh_title
from state import WorkflowState


# ---------------------------------------------------------------------------
# 标题读取 + 真实翻译
# ---------------------------------------------------------------------------
def _get_titles(state: WorkflowState) -> tuple[str, str, str]:
    """读 title_zh(与 node_14 同一 resolver)并真实翻译;返回 (zh, en, status)。

    status:``real`` = 真实翻译成功;``stub`` = 默认翻译客户端是 Mock 桩;
    ``degraded`` = 真实客户端但翻译失败/空译文。stub/degraded 时 ``title_en``
    为空串 —— 调用方不得拼假英文标题。
    """
    draft_path = state.get("draft_path")
    title_zh = "默认标题"
    if draft_path:
        try:
            draft = json.loads(Path(draft_path).read_text(encoding="utf-8"))
            title_zh = resolve_zh_title(draft)
        except (json.JSONDecodeError, OSError):
            pass

    client = get_default_client()
    if not isinstance(client, LLMTranslateClient):
        return title_zh, "", "stub"
    try:
        translated = client.translate([
            {"index": 0, "start_ms": 0, "end_ms": 0, "text_zh": title_zh}
        ])
        title_en = str((translated[0] or {}).get("text_en") or "").strip()
    except Exception as exc:  # noqa: BLE001 — 封面翻译非关键路径,失败降级
        print(f"[node_15] 封面标题翻译失败: {exc!r}")
        return title_zh, "", "degraded"
    if not title_en:
        return title_zh, "", "degraded"
    return title_zh, title_en, "real"


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

    title_zh, title_en, translation_status = _get_titles(state)

    errors: list[str] = []
    if title_en:
        en_paths = list(await asyncio.gather(*[
            asyncio.to_thread(_localize_one_cover, c, title_zh, title_en)
            for c in covers
        ]))
    else:
        # 修订计划 §5.6:标题翻译不可用(stub/degraded)→ 不生成伪英文封面,
        # 全部 en_path=None 降级,由 covers QA 呈现缺英文封面。
        en_paths = [None] * len(covers)
        errors.append(
            f"[node_15] 封面标题翻译不可用(status={translation_status}),"
            "英文封面降级不生成(绝不生成 [EN] 假标题)"
        )

    merged: list[dict] = []
    for cover, en_path in zip(covers, en_paths):
        new_cover = dict(cover)
        new_cover["en_path"] = en_path
        merged.append(new_cover)
        if en_path is None and title_en:
            ratio = cover.get("ratio", "unknown")
            errors.append(f"[node_15] 封面本地化失败:ratio={ratio}")

    return {
        "covers": merged,
        "error_log": errors,
        "cover_translation_status": translation_status,
        "status_log": ["node_15_localize_covers_en_done"],
    }