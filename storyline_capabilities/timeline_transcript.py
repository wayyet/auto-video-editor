"""把 ASR / 口播粗剪文本回填到时间线 ``clips[*].transcript``(2026-10 接入 ASR 旁支)。

只写文字,**不**改 clip 数量、镜头范围、时长 —— "驱动时间线 / 口播剪辑"是另一个项目。

输入约定(都是 ``storyline_local_asr`` / ``storyline_speech_rough_cut`` 的 JSON 产物):

- ``rough_cut``: ``{"media_id": str?, "segments": [{"text", "start", "end", "media_id"?}, ...]}``
- ``asr``:       ``{"media_id": str?, "segments": [{"text", "start", "end", "media_id"?}, ...]}``

时间单位按**毫秒**处理,且是**源素材时间轴**(与 ``clip.source_in_ms / source_out_ms`` 同一把尺)。
funasr 的 ``sentence_info[].start/end`` 是毫秒;真机验收(计划 P7)时要用真实样本再确认一次。

分配规则:
1. 有可用的粗剪段就用粗剪(文本已去口水词),否则用原始 ASR 段。
2. 每个文本段只分给**重叠最多**的那个 clip(平分秋色取更靠前的),不重复分配。
3. 同一个 clip 内的多个文本段按开始时间排序,用单个空格连接。
4. 已有 ``transcript`` 的 clip 不覆盖。
5. 段带 ``media_id``(或文档顶层带)时,只分给 ``source_media_id`` 相同的 clip;
   两者都没有时,仅当时间线只引用**一个**素材才分配(否则无法判断归属,宁可不填)。

永不抛:任何字段缺失 / 类型不对都当作"没有可用文本"。
"""
from __future__ import annotations

from typing import Any, Optional


def _as_ms(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _segments(doc: Any) -> list[dict]:
    if not isinstance(doc, dict):
        return []
    segs = doc.get("segments")
    if not isinstance(segs, list):
        return []
    return [s for s in segs if isinstance(s, dict)]


def _usable(doc: Any) -> list[tuple[int, int, str, str]]:
    """把文档里的段整理成 ``(start, end, text, media_id)``;非法段丢弃。"""
    doc_media = str((doc or {}).get("media_id") or "") if isinstance(doc, dict) else ""
    out: list[tuple[int, int, str, str]] = []
    for s in _segments(doc):
        text = str(s.get("text") or "").strip()
        start = _as_ms(s.get("start"))
        end = _as_ms(s.get("end"))
        if not text or start is None or end is None or end <= start:
            continue
        out.append((start, end, text, str(s.get("media_id") or doc_media)))
    return out


def fill_clip_transcripts(
    timeline: dict,
    *,
    rough_cut: Optional[dict] = None,
    asr: Optional[dict] = None,
) -> int:
    """就地回填 ``timeline["clips"][*]["transcript"]``,返回被回填的 clip 个数。"""
    clips = timeline.get("clips") if isinstance(timeline, dict) else None
    if not isinstance(clips, list) or not clips:
        return 0

    usable = _usable(rough_cut) or _usable(asr)
    if not usable:
        return 0

    media_ids = {
        str(c.get("source_media_id") or "") for c in clips if isinstance(c, dict)
    }

    buckets: dict[int, list[tuple[int, str]]] = {}
    for start, end, text, seg_media in usable:
        if seg_media and seg_media not in media_ids:
            continue  # 转写的素材不在时间线里
        if not seg_media and len(media_ids) != 1:
            continue  # 无法判断归属
        best_idx = -1
        best_overlap = 0
        for idx, clip in enumerate(clips):
            if not isinstance(clip, dict):
                continue
            if seg_media and str(clip.get("source_media_id") or "") != seg_media:
                continue
            c_in = _as_ms(clip.get("source_in_ms"))
            c_out = _as_ms(clip.get("source_out_ms"))
            if c_in is None or c_out is None:
                continue
            overlap = min(end, c_out) - max(start, c_in)
            if overlap > best_overlap:
                best_idx, best_overlap = idx, overlap
        if best_idx >= 0:
            buckets.setdefault(best_idx, []).append((start, text))

    filled = 0
    for idx, items in buckets.items():
        clip = clips[idx]
        if clip.get("transcript"):
            continue
        items.sort(key=lambda it: it[0])
        clip["transcript"] = " ".join(text for _, text in items)
        filled += 1
    return filled


__all__ = ["fill_clip_transcripts"]
