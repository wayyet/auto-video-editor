"""DraftBuildSpec → pyJianYingDraft ScriptFile 统一编译器(迁移设计文档 §5;阶段1)。

编译模式(§0 约束 A):**读取时间线 → DraftBuildSpec → 空白 SDK 草稿完整重建
→ 安全发布**。编译器在一次构建里创建所有 SDK 对象与受控轨道:

- 受控轨道(video_main / subtitles / stickers)由 ``append_track`` 新建;
  **绝不**在模板导入轨道上追加任何效果(§0 约束 A)。
- 原生转场挂到边界的**前一片段**(``segments[i].add_transition``,SDK 语义;
  §4.5/§6.3 —— 旧实现挂后一段是行为差异,编译器禁止沿用)。
- AI 转场 MP4 是普通 ``VideoMaterial`` + ``VideoSegment`` 片段(§0 约束 B),
  绝不伪装成 TransitionType。
- 指纹一致跳过编译(:func:`jianying_sdk.fingerprint.needs_compile`);指纹变化
  从空白草稿完整重建,无增量 upsert(§5.3)。

阶段1 交付:骨架 + 无效果最小草稿(空时间线 + 三条受控空轨道)。
阶段2 起逐段补齐 clips / 字幕 / 文本装饰 / 视频效果 / 贴纸的编译段。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from jianying_sdk._sdk_loader import sdk
from jianying_sdk.fingerprint import compute_build_fingerprint
from jianying_sdk.publisher import validate_sdk_payload
from jianying_sdk.spec import DraftBuildSpec

#: 编译器版本(写入 spec.compile.compiler_version 与摘要;契约变更须 bump)
COMPILER_VERSION = "pyjy-adapter-v1"

#: 受控轨道名(§8.1;轨道名同时出现在草稿 JSON 里,便于人工识别我方轨道)
TRACK_VIDEO_MAIN = "video_main"
TRACK_SUBTITLES = "subtitles"
TRACK_STICKERS = "stickers"


@dataclass
class CompileResult:
    """一次编译的产物与摘要(payload 经 publisher 发布;对象本身不落盘)。"""

    payload: dict[str, Any]
    fingerprint: str
    duration_us: int
    #: 编译摘要(轨道名/各轨道段数/素材桶计数),写回 spec.compile.last_result
    summary: dict[str, Any] = field(default_factory=dict)


def new_script(spec: DraftBuildSpec):
    """按 spec 画布创建空白 ``ScriptFile``(纯内存,不触盘)。"""
    jy = sdk()
    return jy.ScriptFile(
        int(spec.canvas.width),
        int(spec.canvas.height),
        int(spec.canvas.fps),
        maintrack_adsorb=True,
    )


def append_base_tracks(script) -> dict[str, Any]:
    """创建三条受控轨道:主视频轨(底)、字幕轨、贴纸轨(渲染层级按轨道序)。"""
    jy = sdk()
    refs = {
        "video": script.append_track(jy.TrackSpec(jy.TrackType.video, name=TRACK_VIDEO_MAIN)),
        "text": script.append_track(jy.TrackSpec(jy.TrackType.text, name=TRACK_SUBTITLES)),
        "sticker": script.append_track(jy.TrackSpec(jy.TrackType.sticker, name=TRACK_STICKERS)),
    }
    return refs


def compile_spec(
    spec: DraftBuildSpec,
    *,
    force: bool = False,
) -> CompileResult:
    """把一份 DraftBuildSpec 编译为 SDK 草稿载荷(内存,不触盘)。

    指纹一致且未 ``force`` 时抛 :class:`FingerprintUnchangedError`(调用方
    应跳过发布而不是产出重复草稿)。

    Raises:
        FingerprintUnchangedError: 指纹与 last_compiled_fingerprint 一致。
        NotImplementedError: spec 声明了阶段1 尚未支持的构建段(防半成品)。
    """
    fingerprint = compute_build_fingerprint(spec)
    if not force and spec.compile.last_compiled_fingerprint == fingerprint:
        raise FingerprintUnchangedError(fingerprint)

    if spec.timeline.clips or spec.subtitles or spec.transitions or spec.video_effects or spec.video_animations or spec.stickers or spec.text_decorations:
        raise NotImplementedError(
            "时间线片段/字幕/效果的编译在阶段2 起逐段交付(阶段1 只交付"
            "无效果最小草稿:空时间线 + 三条受控空轨道);请使用空的"
            " DraftBuildSpec(仅 job_id/canvas)验收阶段1"
        )

    script = new_script(spec)
    append_base_tracks(script)

    payload = json.loads(script.dumps())
    validate_sdk_payload(payload)

    counts = {
        "tracks": {
            TRACK_VIDEO_MAIN: 0,
            TRACK_SUBTITLES: 0,
            TRACK_STICKERS: 0,
        },
        "materials_total": sum(
            len(v) for v in payload.get("materials", {}).values() if isinstance(v, list)
        ),
    }
    summary = {
        "compiler_version": COMPILER_VERSION,
        "fingerprint": fingerprint,
        "compiled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": counts,
    }
    return CompileResult(
        payload=payload,
        fingerprint=fingerprint,
        duration_us=0,
        summary=summary,
    )


class FingerprintUnchangedError(RuntimeError):
    """spec 指纹与上次编译一致 —— 调用方应跳过本次编译/发布。"""


__all__ = [
    "COMPILER_VERSION",
    "CompileResult",
    "FingerprintUnchangedError",
    "TRACK_STICKERS",
    "TRACK_SUBTITLES",
    "TRACK_VIDEO_MAIN",
    "append_base_tracks",
    "compile_spec",
    "new_script",
]
