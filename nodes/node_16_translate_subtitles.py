"""node_16_translate_subtitles — 共享纯函数 helper(英文字幕草稿读写 / SRT 序列化)。

历史:Week 5 曾把原节点 16(翻译 + layout 校验 + 关卡③ interrupt + 写 SRT)
拆为 ``node_16a_translate_and_check``(翻译)+ ``node_checkpoint3_layout_review``
(关卡③ interrupt),本文件保留两者共享的纯函数——含 layout 校验
(``validate_layout`` 及字体辅助)与关卡③ payload 构造。2026-11-15 关卡③
整体删除(第六期),上述两块及其专属依赖已一并移除。

生产 import 方(2026-11-15 起):
- ``node_16a_translate_and_check``:``_marker_exists`` /
  ``_read_segments_from_draft`` / ``_save_marker`` / ``_write_marker`` /
  ``write_srt_from_segments``。

2026-11-12 noop 治理阶段4:原文件底部的向后兼容 shim
``node_16_translate_subtitles()``(未注册进图,仅旧单元测试调用)及其专用
helper ``_do_after_resume`` 已删除(设计文档 §6 阶段4;删除前已 grep 全仓
确认零生产引用)。本文件其余部分不可整文件删除。
"""

from __future__ import annotations

import json
from pathlib import Path

from draft_ops.atomic_writer_file import atomic_write_file
from jy_common.native_text import text_display_text
from state import SubtitleSegment


# ---------------------------------------------------------------------------
# SRT 时间码
# ---------------------------------------------------------------------------
def _ms_to_srt_time(ms: int) -> str:
    """毫秒 → SRT 时间码 HH:MM:SS,mmm。"""
    if ms < 0:
        ms = 0
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{int(ms):03d}"


def _segments_to_srt(segments: list[SubtitleSegment]) -> str:
    """生成 SRT 文件内容。"""
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        start_ms = int(seg.get("start_ms", 0))
        end_ms = int(seg.get("end_ms", start_ms))
        text_en = (seg.get("text_en") or "").strip()
        lines.append(str(i))
        lines.append(f"{_ms_to_srt_time(start_ms)} --> {_ms_to_srt_time(end_ms)}")
        lines.append(text_en)
        lines.append("")  # 空行分隔
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 草稿读写
# ---------------------------------------------------------------------------
def _read_segments_from_draft(draft_dir: Path) -> list[SubtitleSegment]:
    """从 en_branch 草稿还原 SubtitleSegment 列表(双形态,2026-11-07 升级)。

    - **原生形态**(人工在剪映编辑保存后,私有键被剪映丢弃):计时权威在
      **text 轨道段** ``segment.target_timerange``,文本在 material 的 content
      JSON 里 —— 按 material_id 关联读取,按 start 排序重建 index;
    - **旧形态**(本链路自产、未经剪映保存):``texts[].target_timerange`` +
      ``text_en``/content 纯文本。
    """
    draft_file = draft_dir / "draft_content.json"
    if not draft_file.exists():
        return []
    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    texts = draft.get("materials", {}).get("texts", [])
    texts_by_id = {
        str(t.get("id")): t for t in texts if isinstance(t, dict) and t.get("id")
    }

    native_rows: list[tuple[int, int, str]] = []
    for track in draft.get("tracks") or []:
        if not isinstance(track, dict) or track.get("type") != "text":
            continue
        for seg in track.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            mat = texts_by_id.get(str(seg.get("material_id") or ""))
            if mat is None:
                continue
            tr = seg.get("target_timerange") or {}
            start_ms = int(tr.get("start", 0) or 0) // 1_000
            end_ms = start_ms + int(tr.get("duration", 0) or 0) // 1_000
            native_rows.append((start_ms, end_ms, text_display_text(mat)))
    if native_rows:
        native_rows.sort(key=lambda r: r[0])
        return [
            {"index": i, "start_ms": s, "end_ms": e, "text_en": txt}
            for i, (s, e, txt) in enumerate(native_rows)
        ]

    out: list[SubtitleSegment] = []
    for i, t in enumerate(texts):
        if not isinstance(t, dict):
            continue
        tr = t.get("target_timerange", {})
        start_ms = int(tr.get("start", 0) or 0) // 1_000
        end_ms = start_ms + int(tr.get("duration", 0) or 0) // 1_000
        text_en = t.get("text_en") or text_display_text(t)
        out.append({
            "index": i,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text_en": text_en,
        })
    return out


def _write_marker(draft_dir: Path, segments: list[SubtitleSegment]) -> None:
    """写 subtitle_en.json marker(中断前可重放操作)。

    Week 5 升级:从 ``atomic_write_file(draft_file, json.dumps(draft))``(单写且
    不走双文件)切到 ``safe_write_draft(draft_dir, draft)``,与节点 5/7/8/9/10/11/13
    保持一致 — content 与 info 双写 + 校验 + 失败回退。
    """
    draft_file = draft_dir / "draft_content.json"
    if not draft_file.exists():
        return
    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    texts = draft.setdefault("materials", {}).setdefault("texts", [])
    # 按 index 对齐,只写 text_en
    for seg in segments:
        idx = int(seg.get("index", -1))
        if 0 <= idx < len(texts):
            texts[idx]["text_en"] = seg.get("text_en", "")
    # write_draft 内部已做 verify,失败时整目录回退 + raise;
    # 阶段 6b:经 _draft_io 汇合点写回,自动追加 draft_mutations 审计。
    from nodes._draft_io import write_draft

    write_draft(draft_dir / "draft_content.json", draft, audit_node="node_16")


def _marker_exists(draft_dir: Path, expected_count: int) -> bool:
    """marker 检查:subtitle_en.json 存在且含与预期段数一致的 text_en 字段。"""
    marker = draft_dir / "subtitle_en.json"
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return isinstance(data, list) and len(data) == expected_count


def _save_marker(draft_dir: Path, segments: list[SubtitleSegment]) -> None:
    """保存 subtitle_en.json 标记文件(段数 + text_en 摘要)。"""
    marker = draft_dir / "subtitle_en.json"
    atomic_write_file(
        marker,
        json.dumps(segments, ensure_ascii=False, indent=2).encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# 写 SRT(被 node_16a 在正常路径调用;2026-11-15 关卡③删除前,resume 后的
# 重写也走本函数)
# ---------------------------------------------------------------------------
def write_srt_from_segments(draft_dir: Path, segments: list[SubtitleSegment]) -> str:
    """把 segments 序列化为 SRT 并 atomic_write_file,返回写入路径字符串。"""
    srt_content = _segments_to_srt(segments)
    srt_path = draft_dir / "subtitle_en.srt"
    atomic_write_file(srt_path, srt_content.encode("utf-8"))
    return str(srt_path)
