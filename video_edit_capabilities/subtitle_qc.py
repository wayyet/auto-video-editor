"""``subtitle_qc`` 薄包装层。

实际实现见 :mod:`video_edit_capabilities.subtitle_build`(不拆分 helpers,与
``subtitle_render.py`` 同源)。

设计依据(计划 §6.2 第 5 步):entry 函数 ``subtitle_qc`` 返回
``ToolResult(data={"tool": "subtitle_qc", "status": ..., "issues": [...]})``,
issues 列表包含 ``overlap`` / ``fps_mismatch`` / ``cue_too_long`` /
``text_width_overflow`` / ``missing_glyphs`` 等子类(源端 ``check_cues`` 决定)。

节点代码 (e.g. ``node_16a_translate_and_check`` 阶段四) 调用::

    from video_edit_capabilities.subtitle_qc import subtitle_qc
    ctx = RunContext()
    result = subtitle_qc({"subtitles_path": "...", "video_path": "..."}, ctx)
    if result.data.get("status") == "fail":
        ...

或 :func:`from video_edit_capabilities.subtitle_build import subtitle_qc`
直接拿到底层 helper。
"""
from __future__ import annotations

from .subtitle_build import subtitle_qc

__all__ = ["subtitle_qc"]
