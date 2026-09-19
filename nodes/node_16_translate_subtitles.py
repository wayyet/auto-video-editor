"""node_16_translate_subtitles — 共享纯函数 + 向后兼容 shim(Week 5 拆分后)。

Week 5 改动(对齐第 5 周计划 §1.1 / §2.1):
原节点 16(翻译 + layout 校验 + 关卡③ interrupt + 写 SRT)被拆成两个独立节点:

- ``node_16a_translate_and_check``(在 ``node_16a_translate_and_check.py``):
  只翻译 + 写 marker + 写 ``state["subtitle_segments_en"]`` + 检测 layout → 写
  ``state["layout_issues"]`` / ``state["layout_issues_detected"]``。**不**含
  interrupt,resume 重放时由 marker 守住,自然幂等(附件"关键发现③")。

- ``node_checkpoint3_layout_review``(在 ``node_checkpoint3_layout_review.py``):
  只读 ``state["layout_issues"]``,做 ``interrupt({"checkpoint": "③", ...})``,
  resume 后从草稿读修正结果,写 ``state["subtitle_segments_en"]``。完全无副作用,
  天然幂等。

本文件:
1. 保留两个新节点共享的纯函数(layout 校验 / 草稿读写 / SRT 序列化 / 关卡③ payload);
2. 提供 ``node_16_translate_subtitles`` 的向后兼容 shim — 它顺序调用
   ``node_16a_translate_and_check`` + ``node_checkpoint3_layout_review``(同步
   模拟,不触发真实 interrupt),保证现有 ``tests/unit/test_node_16_translate_subtitles.py``
   在 Day 1 阶段不被破坏。Day 2 ``graph.py`` 切换到新节点后,这个 shim 仅供单元
   测试使用,Week 6+ 可删除。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from langgraph.types import interrupt  # 模块级别 import:让 ``patch("nodes.node_16_translate_subtitles.interrupt")`` 仍能工作

from draft_ops.atomic_writer import safe_write_draft
from draft_ops.atomic_writer_file import atomic_write_file
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
    # safe_write_draft 内部已做 verify,失败时整目录回退 + raise
    safe_write_draft(draft_dir, draft)


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
    """构造关卡③ interrupt payload — 纯函数,便于单测。

    Week 5:``checkpoint`` 字段统一为 ``"③"``(Week 4 已是 "③"),增加 ``step`` /
    ``legacy_id`` 字段便于 ``resume_all_pending`` 匹配与日志检索。
    """
    return {
        "checkpoint": "③",
        "legacy_id": "checkpoint3_layout_review",
        "step": 16,
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
# Week 5:写 SRT(被 node_16a 调用,在 layout 校验之后;关卡③ 触发时,resume
# 后的 segments_en 来自人工修正,本函数被 checkpoint3_layout_review 在
# resume 后调用)
# ---------------------------------------------------------------------------
def write_srt_from_segments(draft_dir: Path, segments: list[SubtitleSegment]) -> str:
    """把 segments 序列化为 SRT 并 atomic_write_file,返回写入路径字符串。"""
    srt_content = _segments_to_srt(segments)
    srt_path = draft_dir / "subtitle_en.srt"
    atomic_write_file(srt_path, srt_content.encode("utf-8"))
    return str(srt_path)


# ---------------------------------------------------------------------------
# 向后兼容 shim — Week 5 Day 1 阶段让现有 ``tests/unit/test_node_16_translate_subtitles.py``
# 不被破坏。Day 2 graph.py 切换到 ``node_16a`` + ``node_checkpoint3`` 后,
# 本 shim 仅供单元测试使用,Week 6+ 可删除。
# ---------------------------------------------------------------------------
def node_16_translate_subtitles(state: WorkflowState) -> dict:
    """向后兼容 shim(Week 5 拆分过渡):按顺序执行"翻译+layout 校验+条件
    interrupt 模拟+写 SRT",对外行为与 Week 4 一致,但实现委托给 16a /
    checkpoint3 内部的纯函数。

    关键差异(相对 Week 4 原实现):
    - 不真正触发 ``langgraph.types.interrupt``;若 layout 有 issues,直接
      走 ``_do_after_resume``(读取草稿)分支。这是单测环境,没有 LangGraph
      重放语义,resume 由 patch interrupt 控制。
    - 实际生产路径(graph.py)Day 2 起会拆成两个独立节点,本 shim 仅保留
      给尚未迁移的单元测试使用。
    """
    from nodes.node_16a_translate_and_check import node_16a_translate_and_check

    # Step 1:翻译 + layout 校验
    step1 = node_16a_translate_and_check(state)
    segments_en = step1.get("subtitle_segments_en") or []
    layout_issues = step1.get("layout_issues") or []
    draft_dir_p = Path(state.get("draft_dir_en_branch") or "")

    # 若 node_16a 跳过(asr/draft_dir 缺失),整个透传,避免覆盖 skip 信息
    if "node_16a_translate_skipped" in (step1.get("status_log") or []):
        return step1

    # Step 2:条件关卡③(同步模拟,不真挂起)
    checkpoint3_triggered = False
    if layout_issues:
        interrupt(_build_interrupt_payload(state, layout_issues))
        resume_out = _do_after_resume(state)
        segments_en = resume_out["segments_en"]
        checkpoint3_triggered = resume_out["checkpoint3_triggered"]

    # Step 3:写 SRT
    srt_path: Optional[str] = None
    if draft_dir_p and draft_dir_p.exists():
        srt_path = write_srt_from_segments(draft_dir_p, segments_en)

    return {
        "subtitle_segments_en": list(segments_en),
        "subtitle_srt_path": srt_path,
        "checkpoint3_triggered": checkpoint3_triggered,
        "status_log": ["node_16_translate_subtitles_done"],
    }
