"""node_16_translate_subtitles — 英文字幕翻译 + 关卡③ interrupt(Week 4 新增)。

对齐计划 §4.4:

关键设计:
1. **翻译调用**(对齐用户决策 Q1):读 ``state["asr_segments_zh"]``,调
   ``jy_common.translate_client.translate_segments()``,不读 ``materials.texts``。
2. **幂等写入**(避免 resume 重放覆盖人工修正):
   - 检查 ``<draft_dir_en_branch>/subtitle_en.json``(marker 文件)是否存在
     且含与预期段数一致的内容 → 若存在,**不**重写字幕,直接进入 layout 校验
3. **Layout 校验**(用 PIL ImageFont):
   - 校验字体可用性 + 每段 ``getbbox(text).width > max_width_px``
4. **条件 interrupt**(对齐 Week 3 关卡①/② 模式):
   - issues 非空 → ``interrupt({"checkpoint": "③", ...})``
   - resume 后:**重读草稿**而非使用 interrupt 返回值(对齐主计划 5.2 节坑③)
5. **副作用写 SRT**(放在 interrupt 后):
   - ``_ms_to_srt_time`` 把毫秒转 SRT 时间码
   - ``atomic_write_file`` 写 ``<draft_dir_en_branch>/subtitle_en.srt``

重放安全:interrupt 前仅做"翻译 + 写 subtitle_en.json(marker)"两步可重放操作;
interrupt 后所有"读草稿 + 写 SRT"严格用幂等检查。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from langgraph.types import interrupt

from draft_ops.atomic_writer_file import atomic_write_file
from jy_common.translate_client import translate_segments
from state import SubtitleSegment, WorkflowState


# ---------------------------------------------------------------------------
# Layout 校验
# ---------------------------------------------------------------------------
DEFAULT_FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)


def _try_load_font(font_path: str | None):
    """加载 PIL 字体,失败时回退到系统默认字体,最后回退到 PIL 默认。"""
    from PIL import ImageFont

    if font_path and Path(font_path).exists():
        try:
            return ImageFont.truetype(font_path, 48)
        except OSError:
            pass
    for c in DEFAULT_FONT_CANDIDATES:
        if Path(c).exists():
            try:
                return ImageFont.truetype(c, 48)
            except OSError:
                continue
    return ImageFont.load_default()


def validate_layout(
    segments: list[SubtitleSegment],
    font_path: Optional[str],
    font_size: int,
    max_width_px: int,
) -> list[dict]:
    """逐段校验英文字幕是否超宽。

    Returns:
        issues 列表,空表示通过。每条含 ``{"index", "text", "width_px",
        "reason"}``。
    """
    issues: list[dict] = []
    font = _try_load_font(font_path)
    if font is None:
        # PIL 完全无字体可用 → 致命问题
        issues.append({
            "index": -1,
            "text": "<font>",
            "reason": f"missing_font:{font_path}",
        })
        return issues

    from PIL import Image, ImageDraw

    dummy_img = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(dummy_img)
    for seg in segments:
        text_en = (seg.get("text_en") or "").strip()
        if not text_en:
            continue
        bbox = draw.textbbox((0, 0), text_en, font=font)
        width_px = bbox[2] - bbox[0]
        if width_px > max_width_px:
            issues.append({
                "index": seg.get("index", -1),
                "text": text_en,
                "width_px": width_px,
                "max_width_px": max_width_px,
                "reason": "text_too_wide",
            })
    return issues


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
    """从 en_branch 草稿读 materials.texts 还原 SubtitleSegment 列表。

    转换规则:从 start/duration 微秒 → start_ms/end_ms 毫秒;text_en 若存在则
    保留,否则用 content 作为占位。
    """
    draft_file = draft_dir / "draft_content.json"
    if not draft_file.exists():
        return []
    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    texts = draft.get("materials", {}).get("texts", [])
    out: list[SubtitleSegment] = []
    for i, t in enumerate(texts):
        tr = t.get("target_timerange", {})
        start_ms = int(tr.get("start", 0)) // 1_000
        end_ms = start_ms + int(tr.get("duration", 0)) // 1_000
        text_en = t.get("content", "")
        out.append({
            "index": i,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text_en": text_en,
        })
    return out


def _write_marker(draft_dir: Path, segments: list[SubtitleSegment]) -> None:
    """写 subtitle_en.json marker(中断前可重放操作)。"""
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
    atomic_write_file(
        draft_file,
        json.dumps(draft, ensure_ascii=False, indent=2).encode("utf-8"),
    )


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
# Interrupt payload
# ---------------------------------------------------------------------------
def _build_interrupt_payload(state: WorkflowState, issues: list[dict]) -> dict:
    """构造关卡③ interrupt payload — 纯函数,便于单测。"""
    return {
        "checkpoint": "③",
        "reason": "英文字幕排版异常",
        "issues": issues,
        "draft_dir": state.get("draft_dir_en_branch"),
    }


def _do_after_resume(state: WorkflowState) -> dict:
    """resume 后逻辑:重读草稿(可能已被人工修正),返回 segments_en。

    纯函数(便于单测)。Week 3 节点 6/12 用同模式。
    """
    draft_dir = Path(state.get("draft_dir_en_branch") or "")
    segments_en = _read_segments_from_draft(draft_dir) if draft_dir.exists() else []
    return {
        "segments_en": segments_en,
        "checkpoint3_triggered": True,
    }


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def node_16_translate_subtitles(state: WorkflowState) -> dict:
    """LangGraph 节点:翻译 + 关卡③ interrupt + 写 SRT。

    返回**只含变更字段**,避免 fan-in 时与其他分支并发写同一字段。
    ``status_log`` / ``error_log`` 由 reducer(``_append_unique``)合并。

    Args:
        state: 需含 ``asr_segments_zh`` / ``draft_dir_en_branch``。
    """
    segments_zh = list(state.get("asr_segments_zh") or [])
    if not segments_zh:
        return {
            "error_log": ["[node_16] asr_segments_zh 缺失,跳过翻译"],
            "status_log": ["node_16_translate_skipped"],
        }

    draft_dir = state.get("draft_dir_en_branch")
    if not draft_dir:
        return {
            "error_log": ["[node_16] draft_dir_en_branch 缺失,跳过翻译"],
            "status_log": ["node_16_translate_skipped"],
        }

    draft_dir_p = Path(draft_dir)

    # ----- interrupt 前:翻译 + 写 marker(可重放)-----
    if _marker_exists(draft_dir_p, expected_count=len(segments_zh)):
        # 已有 marker,跳过重写,直接读草稿
        segments_en = _read_segments_from_draft(draft_dir_p)
    else:
        translated = translate_segments(segments_zh)
        # 同步 start_ms / end_ms(translate_client 可能不返回)
        for i, (src, dst) in enumerate(zip(segments_zh, translated)):
            dst.setdefault("start_ms", src.get("start_ms", 0))
            dst.setdefault("end_ms", src.get("end_ms", 0))
            dst.setdefault("index", src.get("index", i))
        # 写入 draft(materials.texts[i].text_en)与 marker
        _write_marker(draft_dir_p, translated)
        _save_marker(draft_dir_p, translated)
        segments_en = translated

    # ----- layout 校验 -----
    issues = validate_layout(
        segments=segments_en,
        font_path=None,
        font_size=48,
        max_width_px=1500,
    )

    # ----- 条件:issues 非空 → interrupt -----
    checkpoint3_triggered = False
    if issues:
        interrupt(_build_interrupt_payload(state, issues))
        # resume 后:重读草稿(可能已被人工修正),**不**用 interrupt 返回值
        resume_out = _do_after_resume(state)
        segments_en = resume_out["segments_en"]
        checkpoint3_triggered = resume_out["checkpoint3_triggered"]

    # ----- interrupt 后:写 SRT(走 atomic_write_file)-----
    srt_content = _segments_to_srt(segments_en)
    srt_path = draft_dir_p / "subtitle_en.srt"
    atomic_write_file(srt_path, srt_content.encode("utf-8"))

    return {
        "subtitle_segments_en": list(segments_en),
        "subtitle_srt_path": str(srt_path),
        "checkpoint3_triggered": checkpoint3_triggered,
        "status_log": ["node_16_translate_subtitles_done"],
    }