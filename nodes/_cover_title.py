"""封面标题 canonical resolver — node_14(中文封面)/ node_15(英文封面)共用。

2026-11-17 P2(封面标题真翻译,修订计划 §5.6):原实现 node_14/node_15 各自读
``materials.texts[0]`` 当标题 —— 数组顺序不是稳定的标题标识(素材整表替换/
剪映回写都可能改顺序),两侧可能取到不同文本。收敛为唯一解析函数:

1. **原生形态优先**:文本轨 segment 的 ``target_timerange`` 是计时权威,按
   start 升序 + material_id 稳定排序取最早一条(开场第一句 = 标题语义);
2. **旧形态兜底**:materials.texts 自带 ``target_timerange``(链路自产、未经
   剪映保存)按同规则取;
3. 全部缺失 → ``"默认标题"`` 占位(与原行为一致)。

双形态文本解析复用 :func:`jy_common.native_text.text_display_text`(原生
content 是 JSON 字符串,2026-11-11 阶段4修复口径)。
"""

from __future__ import annotations

from typing import Any

from jy_common.native_text import text_display_text

#: 无任何文本素材时的占位标题(与 node_14 原行为一致)
DEFAULT_TITLE = "默认标题"


def resolve_zh_title(draft: dict[str, Any]) -> str:
    """从草稿 dict 解析中文 canonical title(确定性:最早 start + id 稳定序)。"""
    texts = (draft.get("materials") or {}).get("texts") or []
    by_id = {
        str(t.get("id")): t
        for t in texts
        if isinstance(t, dict) and t.get("id")
    }

    # 1) 原生形态:文本轨段计时权威
    rows: list[tuple[int, str, str]] = []
    for track in draft.get("tracks") or []:
        if not isinstance(track, dict) or track.get("type") != "text":
            continue
        for seg in track.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            mat = by_id.get(str(seg.get("material_id") or ""))
            if mat is None:
                continue
            text = text_display_text(mat).strip()
            if not text:
                continue
            tr = seg.get("target_timerange") or {}
            rows.append((int(tr.get("start", 0) or 0), str(seg.get("material_id")), text))
    if rows:
        rows.sort(key=lambda r: (r[0], r[1]))
        return rows[0][2]

    # 2) 旧形态:materials.texts 自带 target_timerange
    for i, mat in enumerate(texts):
        if not isinstance(mat, dict):
            continue
        text = text_display_text(mat).strip()
        if not text:
            continue
        tr = mat.get("target_timerange") or {}
        rows.append(
            (int(tr.get("start", 0) or 0), str(mat.get("id") or f"idx-{i}"), text)
        )
    if rows:
        rows.sort(key=lambda r: (r[0], r[1]))
        return rows[0][2]

    return DEFAULT_TITLE
