"""字幕文本素材构造 — node_08(中文)与 node_08b(英文)共用。

只负责"一条 materials.texts 条目长什么样",不碰 I/O、不碰 state。
字段顺序/取值与重构前 node_08 内联写法逐项一致(id/content/target_timerange/style/track_name)。
"""

from __future__ import annotations

import copy
from typing import Any


def make_text_material(
    *,
    index: int,
    content: str,
    start_us: int,
    duration_us: int,
    style: dict[str, Any],
    track_name: str,
    id_prefix: str = "text",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一条 ``materials.texts`` 条目(微秒时间轴)。

    Args:
        index: 从 0 开始的序号,生成 id 为 ``f"{id_prefix}-{index + 1}"``。
        content: 字幕文本。
        start_us / duration_us: ``target_timerange`` 起点与时长(微秒)。
        style: 样式 dict;内部做 deepcopy,每条字幕互不污染。
        track_name: 轨道名(中文 ``"Subtitles"``,英文 ``"Subtitles_en"``)。
        id_prefix: id 前缀(中文 ``"text"``,英文 ``"text-en"``)。
        extra: 追加字段(英文用来写 ``text_en``,与 16a 的 marker 约定一致)。
    """
    item: dict[str, Any] = {
        "id": f"{id_prefix}-{index + 1}",
        "content": content,
        "target_timerange": {"start": start_us, "duration": duration_us},
        "style": copy.deepcopy(style),
        "track_name": track_name,
    }
    if extra:
        item.update(extra)
    return item
