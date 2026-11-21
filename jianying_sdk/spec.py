"""DraftBuildSpec 数据契约(迁移设计文档 §5.3;阶段1 落地)。

工作流效果节点(node_08/09/10/09b/11)不再直接改写 ``draft_content.json``,
而是更新各自草稿独立的 **JSON 构建说明**;``node_11`` 的 SDK 编译器读全部
说明,用 pyJianYingDraft 一次性构建草稿,经 ``safe_write_draft`` 安全发布。

关键约定(§5.3,不可违背):

- 时间统一 **微秒(us)**;Canonical Timeline 的毫秒值只在边界处转换。
- 每个效果引用稳定业务 ID(``clip_id`` / ``subtitle_id``),不依赖数组位置。
- 资源未准入时明确写 ``unsupported`` / ``degraded`` 状态,不建空素材假资源。
- 中文/英文各一份独立 spec 文件,不共享可变对象。
- 本契约只描述**要构建什么**;SDK 对象构建细节在 ``compiler.py``。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CanvasSpec(BaseModel):
    """画布尺寸与帧率。"""

    width: int = 1080
    height: int = 1920
    fps: int = 30


class TimelineClipSpec(BaseModel):
    """时间线视频片段(阶段2 TimelineSnapshotExtractor 的标准化输出)。

    - ``source_*`` 是素材内部时间;``target_*`` 是成片时间;二者差异表达变速。
    - ``role="ai_transition"`` 标记 AI 生成转场 MP4 普通片段(迁移文档 §6.5,
      绝不伪装成 TransitionType);``ai_transition_pair`` 保留来源审计。
    """

    clip_id: str
    source_path: str
    source_in_us: int = 0
    source_duration_us: int
    target_start_us: int
    target_duration_us: int
    speed: float = 1.0
    volume: float = 1.0
    muted: bool = False
    order: int
    role: Literal["main", "ai_transition"] = "main"
    ai_transition_pair: dict[str, Any] | None = None
    #: 裁剪/缩放/位置/旋转等需保留字段(阶段2 兼容矩阵逐项启用;SDK 不支持
    #: 的字段由 extractor 阻断,不得静默丢弃 —— §7.2)
    clip_settings: dict[str, Any] | None = None


class TransitionSpec(BaseModel):
    """原生转场(边界 i→i+1 挂到**前一片段**,SDK 语义;迁移文档 §6.3)。"""

    boundary_index: int = Field(description="0-based 边界序号:边界位于 order=boundary_index 与 boundary_index+1 的片段之间")
    from_clip_id: str
    to_clip_id: str
    effect_id: str
    resource_id: str
    name: str = ""
    duration_us: int
    is_overlap: bool = False


class VideoEffectSpec(BaseModel):
    """视频特效(强度为 SDK 口径 0~100;§6.4)。"""

    clip_id: str
    effect_id: str
    resource_id: str
    name: str = ""
    intensity: float = 50.0


class VideoAnimationSpec(BaseModel):
    """视频片段动画(in/out/group;§6.4)。"""

    clip_id: str
    kind: Literal["in", "out", "group"]
    effect_id: str
    resource_id: str
    name: str = ""
    duration_us: int | None = None


class SubtitleSpec(BaseModel):
    """字幕片段(阶段3;样式字段由 style_mapper 转换,原样透传业务决策)。"""

    subtitle_id: str
    text: str
    start_us: int
    end_us: int
    language: Literal["zh", "en"] = "zh"
    #: 字幕样式(style_mapper 输入;auto 原生字段口径,Phase 3 完成映射)
    style: dict[str, Any] | None = None
    #: 折行后的渲染文本(subtitle_wrap 产物;私有键原文进 style 原样保留)
    wrap_content: str | None = None


class TextAnimationEntry(BaseModel):
    """文本动画条目(in/out/loop;SDK 要求先入场/出场后循环,§4.4)。"""

    kind: Literal["in", "out", "loop"]
    effect_id: str
    resource_id: str
    name: str = ""
    duration_us: int | None = None


class ResourceRef(BaseModel):
    """花字/气泡资源引用(带四态验证状态;§6.2)。"""

    effect_id: str
    resource_id: str
    name: str = ""
    verification: Literal[
        "structure_verified",
        "cache_verified",
        "license_verified",
        "client_preview_verified",
        "degraded",
        "unsupported",
    ] = "unsupported"


class TextDecorationSpec(BaseModel):
    """单条字幕的文字装饰(动画 + 花字 + 气泡;§6.2)。"""

    subtitle_id: str
    text_effect: ResourceRef | None = None
    text_bubble: ResourceRef | None = None
    animations: list[TextAnimationEntry] = Field(default_factory=list)


class StickerSpec(BaseModel):
    """贴纸(独立 sticker 轨;§6.6,资源 ID 必须 19 位真实 ID)。"""

    sticker_id: str
    resource_id: str
    start_us: int
    duration_us: int
    keyword: str | None = None
    source_subtitle_id: str | None = None


#: 配音轨名(node_08 中文配音 / node_17 英文配音标记;nodes/_dual_draft 同源引用)。
#: 编译器按 ``AudioClipSpec.role`` 分轨落回这两个轨名,保证 node_12 关卡②/镜像、
#: node_13 音量角色、QA 语言隔离的"轨名识别"语义在 SDK 编译稿上存活(2026-11-22)。
TRACK_VOICEOVER_ZH = "Voiceover_zh"
TRACK_VOICEOVER_EN = "en_voiceover"


class AudioClipSpec(BaseModel):
    """音频片段(BGM / 配音 / 人工音频;编译为 SDK AudioSegment)。

    - 音频轨可有多条(自动 BGM 轨 / node_08 权威配音轨 / 人工添加)。提取器按
      时间线位置合并进 ``timeline.audio_clips``;编译器按 ``role`` 分轨:
      ``voiceover_zh``/``voiceover_en`` 各自落回配音轨名(见上方常量),
      其余(bgm/manual)铺进 ``audio_main``——音轨间无渲染层级语义,合并无损,
      但**轨名语义有主**(下游按轨名识别配音)。
    - ``role`` 由提取器按源轨名判定(配音轨名精确匹配),node_05 原生转换
      sidecar 的 id_map 仅作回退。
    - ``fade_in_us``/``fade_out_us`` 来自 ``materials.audio_fades``(node_13)。
    """

    audio_id: str
    source_path: str
    target_start_us: int
    target_duration_us: int
    source_start_us: int = 0
    source_duration_us: int = 0
    speed: float = 1.0
    volume: float = 1.0
    role: Literal["bgm", "voiceover", "voiceover_zh", "voiceover_en", "manual"] = "manual"
    fade_in_us: int = 0
    fade_out_us: int = 0
    name: str = ""


class TimelineSection(BaseModel):
    """时间线快照来源与片段列表(阶段2)。

    ``blocked_features`` 记录提取时发现的 **SDK 无法忠实重建**的人工高级特征
    (§7.2 字段保留矩阵);编译器见非空即抛 :class:`UnsupportedManualFeatureError`
    暂停编译并保留原草稿,绝不静默生成缺效果的重建稿。
    """

    source_draft_path: str | None = None
    source_snapshot_sha256: str | None = None
    clips: list[TimelineClipSpec] = Field(default_factory=list)
    audio_clips: list[AudioClipSpec] = Field(default_factory=list)
    blocked_features: list[dict[str, Any]] = Field(default_factory=list)


class CompileMeta(BaseModel):
    """编译指纹与状态(§5.3;指纹未变不重复编译)。"""

    input_fingerprint: str | None = None
    last_compiled_fingerprint: str | None = None
    compiler_version: str = "pyjy-adapter-v1"
    last_compiled_at: str | None = None
    last_result: dict[str, Any] | None = None


class DraftBuildSpec(BaseModel):
    """一份草稿的完整构建说明(中文/英文各一份,互不共享)。"""

    schema_version: int = 1
    job_id: str
    language: Literal["zh", "en"] = "zh"
    canvas: CanvasSpec = Field(default_factory=CanvasSpec)
    timeline: TimelineSection = Field(default_factory=TimelineSection)
    subtitles: list[SubtitleSpec] = Field(default_factory=list)
    text_decorations: list[TextDecorationSpec] = Field(default_factory=list)
    video_effects: list[VideoEffectSpec] = Field(default_factory=list)
    video_animations: list[VideoAnimationSpec] = Field(default_factory=list)
    transitions: list[TransitionSpec] = Field(default_factory=list)
    stickers: list[StickerSpec] = Field(default_factory=list)
    compile: CompileMeta = Field(default_factory=CompileMeta)


__all__ = [
    "AudioClipSpec",
    "CanvasSpec",
    "CompileMeta",
    "DraftBuildSpec",
    "ResourceRef",
    "StickerSpec",
    "SubtitleSpec",
    "TextAnimationEntry",
    "TextDecorationSpec",
    "TimelineClipSpec",
    "TimelineSection",
    "TransitionSpec",
    "VideoAnimationSpec",
    "VideoEffectSpec",
]
