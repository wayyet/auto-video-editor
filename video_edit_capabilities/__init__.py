"""``video_edit_capabilities`` — 9 个 video-agent-kit 0.4.3 MCP 工具的纯函数搬入。

计划定位(见 ``docs/integration/video-agent-kit九个MCP工具迁移至auto-video-editor设计执行计划.md``):

- ``visual_evidence.py``  — ``video_watch_segment`` + ``video_read_frames``
- ``media_operation.py``  — ``video_basic_operation``
- ``subtitle_scout.py``   — ``subtitle_scout``
- ``subtitle_build.py``   — ``subtitle_build``
- ``subtitle_render.py``  — ``subtitle_render``
- ``subtitle_qc.py``      — ``subtitle_qc``
- ``speech_synthesize.py``— ``speech_synthesize`` + ``tts_generate``(阶段三,本周未搬)

约定:
- 入参 ``args: dict`` + ``ctx: RunContext``,返回 ``ToolResult`` —— 完全沿用
  video-agent-kit 内部调用约定,但 **不** 引入 MCP 协议依赖。RunContext /
  ToolResult 复用 ``assembly_capabilities/`` 的精简版,不复制定义。
- 与 ``assembly_capabilities/`` 8 工具类似:每个工具可以独立 import、独立单测;
  节点代码在 imports 时按需引入。
- ``result.py`` 与 ``run_context.py`` 的 import 来源: ``from assembly_capabilities.result``
  与 ``from assembly_capabilities.run_context``,**不** 复制。
- 共享辅助模块(``ffproc.py`` / ``fonts.py`` / ``render.py`` / ``timeline.py``)复制到本
  包内,内部 import 改用 ``from . import ffproc as _ffproc`` 形式,避免依赖
  ``assembly_capabilities`` 内部 helper 路径(将来两边独立演进时不互相牵制)。

逐步搬入顺序:阶段一(纯函数工具) + 阶段二(字幕四件套)。
"""
from __future__ import annotations

__all__: list[str] = []
