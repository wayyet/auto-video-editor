"""``subtitle_render`` 薄包装层。

实际实现见 :mod:`video_edit_capabilities.subtitle_build`(历史决定:不拆分
helpers,见 ``subtitle_build.py`` 文件 docstring)。本模块只负责 re-export
``subtitle_render`` 函数,对外保持计划 §3.2 描述的 *4 文件* 结构。

设计依据(计划 §6.2 第 5 步):

- 内部使用 :func:`video_edit_capabilities.subtitle_build.subtitle_render` 完成
  完整渲染;依赖的 helpers ``_ffmpeg_has_libass`` 等价于
  :func:`video_edit_capabilities.ffproc.require_ffmpeg`(filters=("subtitles",))。
- ``subprocess.run`` 全部走 :func:`video_edit_capabilities.ffproc.run_proc`
  而非直接 :func:`subprocess.run`,继承 ``-nostdin`` 注入。
"""
from __future__ import annotations

from .subtitle_build import subtitle_render

__all__ = ["subtitle_render"]
