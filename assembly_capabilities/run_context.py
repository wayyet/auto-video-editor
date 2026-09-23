"""精简版 ``RunContext``(按 ADR-4:为单次管线调用场景定制)。

原 video-agent-kit 0.4.3 ``mcp/ve_tools/run_context.py`` 的 ``RunContext`` 设计
目标是 MCP 长会话(单进程跨多次工具调用),会记忆 active_video_path /
transcript_text / _visually_ingested_video_keys / _transcripts_by_video 等。

本仓库使用场景是 LangGraph 单节点单次调用,**节点之间通过 state 字段传产物
路径**,不需要跨调用的内存记忆,所以本实现:
1. 保留 ``project_dir`` / ``output_dir`` / ``work_dir``(节点调用前用 ``with``
   上下文替换),保留 ``resolve`` / ``virtualize`` / ``file_fingerprint`` 三个
   路径处理方法。
2. 删除全部 ``active_*`` / ``remember_*`` / ``_transcripts_by_video`` 等长
   会话记忆字段与 ``reset_active_state`` / ``has_visual_ingest_history`` 等
   长会话管理方法。
3. 删除 ``clean_env`` / ``project_dir`` / ``env_files`` / ``check_endpoint`` /
   ``is_allowed_host`` 等云端 endpoint 处理(本仓库完全本地,不调云)。
4. ``session_kind`` 保留,但只识别 ``"pipeline"``,不再用 ``"cli"`` /
   ``"mcp"``(无 MCP)。
5. ``reject_input_output_collision`` 保留——所有剪辑/渲染工具都依赖它,
   防止 output 覆盖 input。
"""
from __future__ import annotations

import os
from pathlib import Path


class RunContext:
    """本仓库精简版 RunContext。"""

    def __init__(self, session_kind: str = "pipeline") -> None:
        if session_kind != "pipeline":
            # 本仓库只识别 "pipeline";CLI / MCP 场景与本仓库无关,直接拒绝。
            raise ValueError(
                f"RunContext.session_kind must be 'pipeline' in this project, got {session_kind!r}"
            )
        self.session_kind = session_kind
        self.project_dir = Path.cwd().resolve()
        self.output_dir = self.project_dir / "out"
        self.work_dir = self.project_dir / ".video_agent"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # ---- 路径处理(原 RunContext.resolve / virtualize / file_fingerprint)----
    def resolve(self, path: str | Path) -> Path:
        p = Path(path)
        if p.is_absolute():
            return p.resolve()
        return (self.project_dir / p).resolve()

    def virtualize(self, path: str | Path) -> str:
        p = Path(path).resolve()
        try:
            return str(p.relative_to(self.project_dir))
        except ValueError:
            return str(p)

    @staticmethod
    def file_fingerprint(path: Path | None) -> str | None:
        """path|size|mtime_ns 指纹; 文件不存在/不可 stat 返回 None。"""
        if path is None:
            return None
        resolved = Path(path).resolve()
        try:
            stat = resolved.stat()
        except OSError:
            return None
        return f"{resolved}|{stat.st_size}|{stat.st_mtime_ns}"


def reject_input_output_collision(input_path: Path | None, output_path: Path | None) -> None:
    """若 ``output_path`` 会覆盖 ``input_path`` 则抛 ValueError。

    剪映/渲染类工具都用 ``-y`` 强制覆盖,若用户(或 LLM)把 output 指向源文件,
    会静默销毁源素材。Resolved-path 比较同时抓住字符串相同与 ``./a.mp4``
    vs ``out/../a.mp4`` 这两种情形。
    """
    if input_path is None or output_path is None:
        return
    try:
        if Path(input_path).resolve() == Path(output_path).resolve():
            raise ValueError(
                f"refusing to write output over the input file ({output_path}); "
                "choose a different output_path"
            )
    except OSError:
        return
