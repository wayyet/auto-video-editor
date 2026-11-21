"""DraftBuildSpec 读写公共样板(迁移设计文档 §8.1;阶段2)。

每个效果节点(node_08/09/10/09b)只更新 spec 中**自己负责的构建段**(整体替换,
不做条目级 upsert —— 声明式计划每轮全量重算,避免残留 refs,§5.3);
``node_11`` 读全量 spec 交给 SDK 编译器。

spec 文件与草稿目录同住(``<draft_dir>/.autovideo_draft_build_spec.json``,
点前缀,剪映不读;``.autovideo_draft_mutations.jsonl`` 同款先例),中文/英文
草稿目录天然隔离(§5.3:不共享可变文件)。

幂等语义:

- :func:`replace_spec_sections` — 读现有 spec(缺失则按参数新建),**整体替换**
  给出的构建段,原子写回。重放/重试天然幂等。
- 时间线段(timeline)由 node_07 建立基线、node_11 编译前用
  :func:`refresh_timeline_from_draft` 以**当前草稿**重新提取覆盖(保证
  node_08 配音重写等中间变更不被 node_07 时代的快照覆盖)。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from jianying_sdk.spec import (
    AudioClipSpec,
    CanvasSpec,
    DraftBuildSpec,
    SubtitleSpec,
    TextDecorationSpec,
    TimelineSection,
    TransitionSpec,
    StickerSpec,
    VideoAnimationSpec,
    VideoEffectSpec,
)

#: spec 文件名(与草稿同目录;点前缀,剪映不读)
SPEC_FILE_NAME = ".autovideo_draft_build_spec.json"


def spec_path_of(draft_dir: Path | str) -> Path:
    """spec 文件完整路径。"""
    return Path(draft_dir) / SPEC_FILE_NAME


def load_build_spec(draft_dir: Path | str) -> DraftBuildSpec | None:
    """读 spec;文件缺失/损坏返回 None(调用方决定新建或报错)。"""
    path = spec_path_of(draft_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return DraftBuildSpec.model_validate(data)
    except Exception:  # noqa: BLE001 - schema 漂移的旧 spec 视为缺失(§阶段7 兼容检查)
        return None


def write_build_spec(draft_dir: Path | str, spec: DraftBuildSpec) -> Path:
    """原子写回 spec(同盘 mkstemp + os.replace;模式与 sidecar 一致)。"""
    target = spec_path_of(draft_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=target.parent, prefix=".buildspec_", suffix=".json")
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(spec.model_dump_json(indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        if tmp.exists():
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    return target


def replace_spec_sections(
    draft_dir: Path | str,
    *,
    job_id: str | None = None,
    language: str | None = None,
    canvas: CanvasSpec | None = None,
    timeline: TimelineSection | None = None,
    subtitles: list[SubtitleSpec] | None = None,
    text_decorations: list[TextDecorationSpec] | None = None,
    video_effects: list[VideoEffectSpec] | None = None,
    video_animations: list[VideoAnimationSpec] | None = None,
    transitions: list[TransitionSpec] | None = None,
    stickers: list[StickerSpec] | None = None,
    audio_clips: list[AudioClipSpec] | None = None,
) -> DraftBuildSpec:
    """读-改-写 spec:整体替换给出的构建段,其余段原样保留。

    参数为 ``None`` 表示"本节点不拥有该段,不动";传列表(含空)表示
    "该段归本节点,全量替换"。``language``/``job_id`` 仅在新建时生效
    (spec 身份字段,节点不得改写他人身份);例外:英文分支 fork 复制的
    spec 带着中文身份,分支节点经 ``set_language`` 显式接管(阶段3)。
    """
    spec = load_build_spec(draft_dir)
    if spec is None:
        spec = DraftBuildSpec(
            job_id=str(job_id or ""),
            language="zh" if (language or "zh") != "en" else "en",
            canvas=canvas or CanvasSpec(),
        )
    if timeline is not None:
        spec.timeline = timeline
    if audio_clips is not None:
        spec.timeline.audio_clips = audio_clips
    if canvas is not None:
        spec.canvas = canvas
    if subtitles is not None:
        spec.subtitles = subtitles
    if text_decorations is not None:
        spec.text_decorations = text_decorations
    if video_effects is not None:
        spec.video_effects = video_effects
    if video_animations is not None:
        spec.video_animations = video_animations
    if transitions is not None:
        spec.transitions = transitions
    if stickers is not None:
        spec.stickers = stickers
    write_build_spec(draft_dir, spec)
    return spec


def set_spec_language(draft_dir: Path | str, language: str) -> DraftBuildSpec:
    """分支节点接管 fork 复制的 spec 身份(en fork:zh → en)。"""
    spec = load_build_spec(draft_dir)
    if spec is None:
        spec = DraftBuildSpec(job_id="", language="en" if language == "en" else "zh")
    spec.language = "en" if language == "en" else "zh"
    write_build_spec(draft_dir, spec)
    return spec


__all__ = [
    "SPEC_FILE_NAME",
    "load_build_spec",
    "replace_spec_sections",
    "spec_path_of",
    "write_build_spec",
]
