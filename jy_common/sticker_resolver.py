"""贴纸 resource_id 解析 — Week 3 步骤 11 使用(对应原文档 4.6 节)。

Week 3:根据字幕文本中的关键词,在 ``templates/sticker_template.json`` 的
``sticker_by_keyword`` 中查找匹配的占位 resource_id;Week 4 接入
``jianying-editor/scripts/sync_jy_assets.py`` 的思路后替换为真实 resource_id。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load_keyword_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data.get("sticker_by_keyword", {})


def resolve_sticker_resource_id(
    subtitle_text: str,
    template_path: str | Path = "templates/sticker_template.json",
) -> str | None:
    """根据字幕文本查找匹配的贴纸 resource_id。

    规则(Week 3 占位):
    1. 在 ``sticker_by_keyword`` 中按子串匹配字幕文本
    2. 第一个命中的关键词对应的 resource_id 即返回值
    3. 未命中时返回 None(Week 3 可接受 — 节点 11 会跳过该条字幕)
    4. 模板库缺失时返回 None

    Args:
        subtitle_text: 字幕内容(中文/英文皆可)
        template_path: 贴纸模板库路径,缺省 templates/sticker_template.json

    Returns:
        命中的 resource_id 字符串,无匹配或缺模板时返回 None。
    """
    keyword_map = _load_keyword_map(Path(template_path))
    if not keyword_map:
        return None

    # 按键长度倒序匹配,优先匹配长关键词(避免 "赞" 抢匹配 "赞赏")
    for keyword in sorted(keyword_map.keys(), key=len, reverse=True):
        if keyword and keyword in subtitle_text:
            return keyword_map[keyword]

    # 默认兜底
    return keyword_map.get("默认")