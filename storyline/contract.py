"""Canonical Timeline 与 StorylinePlan 的 Pydantic 模型。

数据契约层(主项目内部):
- ``SourceMedia``:源素材(音/视频/图片),``file_uri`` 走 ``file:///E:/...`` 形式,
  整数毫秒 ``duration_ms``。
- ``Clip``:单个时间轴片段,显式区分源范围 ``source_in_ms / source_out_ms`` 与
  时间轴落位 ``timeline_in_ms / timeline_out_ms``,避免引入浮点累计误差。
- ``CanonicalTimeline``:完整抓取契约(整数毫秒 + file_uri + 不变量校验)。
- ``StorylinePlan``:FireRed ``plan_timeline_pro`` 工具返回的原始 plan 拍平后
  的中转结构;Phase 0/1 用宽松字段集(``extra=allow``)接住上游漂移,Phase 4
  用 ``model_validator`` 强校验。

不变量校验(``CanonicalTimeline``):
1. ``source_out_ms > source_in_ms``(源范围非空)
2. ``timeline_out_ms > timeline_in_ms``(时间轴范围非空)
3. ``clips[*].timeline_in_ms`` 单调非降(顺序拼接)
4. ``source_out_ms <= source_media[media_id].duration_ms``(源不溢出)
5. 所有 ``source_media_id`` 命中 ``source_media[*].media_id``(引用闭合)
6. 所有毫秒字段 >= 0(整数,非负)
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------------------
# 原子结构
# ---------------------------------------------------------------------------
class SourceMedia(BaseModel):
    """源素材条目。``file_uri`` 走 ``file:///E:/path`` 形式,大文件不进 MCP 参数。"""

    model_config = ConfigDict(extra="forbid")

    media_id: str = Field(..., description="本地唯一 ID,形如 media_0001")
    file_uri: str = Field(..., description="file:/// 形式 URI;Windows 用 file:///E:/...")
    sha256: Optional[str] = Field(None, description="可选 sha256,用于幂等键")
    duration_ms: Annotated[int, Field(ge=0, description="整数毫秒时长")]
    media_type: Literal["video", "image", "audio", "unknown"] = Field(
        "video", description="FireRed 引入时按扩展名识别"
    )
    width: Optional[int] = Field(None, ge=0)
    height: Optional[int] = Field(None, ge=0)
    fps: Optional[float] = Field(None, ge=0.0)

    @field_validator("file_uri")
    @classmethod
    def _file_uri_scheme(cls, v: str) -> str:
        if not v.startswith("file:///"):
            raise ValueError(f"file_uri must start with 'file:///', got: {v!r}")
        return v


class Clip(BaseModel):
    """单个时间轴片段。源范围 / 时间轴范围各自独立毫秒整数。"""

    model_config = ConfigDict(extra="forbid")

    clip_id: str = Field(..., description="本地唯一 ID,形如 clip_0001")
    source_media_id: str = Field(..., description="引用 source_media.media_id")
    source_in_ms: Annotated[int, Field(ge=0, description="源片段起点(整数毫秒)")]
    source_out_ms: Annotated[int, Field(ge=0, description="源片段终点(整数毫秒)")]
    timeline_in_ms: Annotated[int, Field(ge=0, description="时间轴落位起点(整数毫秒)")]
    timeline_out_ms: Annotated[int, Field(ge=0, description="时间轴落位终点(整数毫秒)")]
    transcript: Optional[str] = Field(None, description="该片段字幕 / ASR 文本")
    scene_id: Optional[str] = Field(None, description="FireRed split_shots 给出的 scene/shot ID")
    semantic_tags: list[str] = Field(
        default_factory=list, description="understand_clips 给出的语义标签"
    )
    content_hash: Optional[str] = Field(None, description="可选 content_hash,用于幂等键")

    @model_validator(mode="after")
    def _validate_ranges(self) -> "Clip":
        if self.source_out_ms <= self.source_in_ms:
            raise ValueError(
                f"clip {self.clip_id}: source_out_ms ({self.source_out_ms}) "
                f"must be > source_in_ms ({self.source_in_ms})"
            )
        if self.timeline_out_ms <= self.timeline_in_ms:
            raise ValueError(
                f"clip {self.clip_id}: timeline_out_ms ({self.timeline_out_ms}) "
                f"must be > timeline_in_ms ({self.timeline_in_ms})"
            )
        return self


class TimelineAudio(BaseModel):
    """音频轨(BGM + 配音)。可选,允许 null。"""

    model_config = ConfigDict(extra="forbid")

    bgm_ref: Optional[str] = Field(None, description="BGM 资源 ID,形如 bgm_0001")
    voiceover: Optional[str] = Field(None, description="配音资源 ID,形如 voiceover_0001")


class TimelineSubtitles(BaseModel):
    """字幕文本。中文必填,英文 Phase 2 由翻译节点补。"""

    model_config = ConfigDict(extra="forbid")

    zh: Optional[str] = Field(None, description="中文字幕文本")
    en: Optional[str] = Field(None, description="英文字幕文本(Phase 2 由翻译节点补)")


class TimelineOptions(BaseModel):
    """时间轴选项。``enable_ai_transition`` 默认 false(ADR-005)。"""

    model_config = ConfigDict(extra="forbid")

    enable_ai_transition: bool = Field(
        False, description="FireRed plan_timeline_ai_transition;默认关闭"
    )
    enable_voiceover: bool = Field(True, description="是否生成配音;为 false 时跳过 TTS")
    max_duration_ms: Optional[int] = Field(
        None, ge=0, description="目标总时长上限(整数毫秒),null/0 表示不限"
    )


# ---------------------------------------------------------------------------
# Canonical Timeline
# ---------------------------------------------------------------------------
class CanonicalTimeline(BaseModel):
    """主项目内部契约:整数毫秒 + file_uri + 不变量校验。

    字段严格按 plan §6.1;``schema_version`` 锁定为 ``1.0`` 便于 Phase 4
    升级时多版本兼容。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = Field("1.0", description="契约版本")
    job_id: str = Field(..., description="LangGraph job_id,= state.session_id")
    created_at_ms: int = Field(..., ge=0, description="epoch 毫秒")
    source_media: list[SourceMedia] = Field(default_factory=list)
    clips: list[Clip] = Field(default_factory=list)
    audio: TimelineAudio = Field(default_factory=TimelineAudio)
    subtitles: TimelineSubtitles = Field(default_factory=TimelineSubtitles)
    options: TimelineOptions = Field(default_factory=TimelineOptions)

    @model_validator(mode="after")
    def _validate_timeline_invariants(self) -> "CanonicalTimeline":
        media_by_id = {s.media_id: s for s in self.source_media}
        if not self.clips:
            # 空时间轴允许(用于仅素材导入场景),但 source_media 必须非空
            if not self.source_media:
                raise ValueError("CanonicalTimeline: both source_media and clips empty")
            return self

        prev_timeline_in: int | None = None
        for clip in self.clips:
            # 1. media_id 命中
            if clip.source_media_id not in media_by_id:
                raise ValueError(
                    f"clip {clip.clip_id}: source_media_id {clip.source_media_id!r} "
                    f"not in source_media ids {sorted(media_by_id.keys())!r}"
                )
            # 2. 源范围不溢出
            media = media_by_id[clip.source_media_id]
            if clip.source_out_ms > media.duration_ms:
                raise ValueError(
                    f"clip {clip.clip_id}: source_out_ms ({clip.source_out_ms}) "
                    f"exceeds media duration_ms ({media.duration_ms})"
                )
            # 3. timeline_in_ms 单调非降
            if prev_timeline_in is not None and clip.timeline_in_ms < prev_timeline_in:
                raise ValueError(
                    f"clip {clip.clip_id}: timeline_in_ms ({clip.timeline_in_ms}) "
                    f"breaks monotonic (prev was {prev_timeline_in})"
                )
            prev_timeline_in = clip.timeline_in_ms
        return self


# ---------------------------------------------------------------------------
# StorylinePlan:FireRed plan_timeline_pro 输出拍平后的中转结构
# ---------------------------------------------------------------------------
class GroupArtifact(BaseModel):
    """StorylinePlan.groups[] 的一个段落(plan_timeline_pro 输出形状)。"""

    model_config = ConfigDict(extra="allow")  # 上游字段可能漂移,宽松接住

    group_id: str
    start_ms: Optional[int] = Field(None, ge=0)
    end_ms: Optional[int] = Field(None, ge=0)
    media_refs: list[dict[str, Any]] = Field(default_factory=list)
    voiceover_ref: Optional[str] = None
    subtitle_text: Optional[str] = None


class StorylineTimeline(BaseModel):
    """``tool_excute_result.timeline`` 形状(基于 plan_timeline_pro 源码核验)。"""

    model_config = ConfigDict(extra="allow")

    duration_ms: Optional[int] = Field(None, ge=0)
    groups: list[GroupArtifact] = Field(default_factory=list)
    bgm_ref: Optional[str] = None
    transitions: list[dict[str, Any]] = Field(default_factory=list)


class StorylineToolResult(BaseModel):
    """``tool_excute_result`` 拍平结构。"""

    model_config = ConfigDict(extra="allow")

    timeline: Optional[StorylineTimeline] = None
    media: Optional[list[dict[str, Any]]] = None
    clip_captions: Optional[list[dict[str, Any]]] = None
    group_scripts: Optional[list[dict[str, Any]]] = None
    bgm: Optional[dict[str, Any]] = None
    voiceover: Optional[list[dict[str, Any]]] = None


class StorylinePlan(BaseModel):
    """FireRed MCP ``call_tool`` 返回的 plan 拍平结构。

    字段与 register_tools.py 的 wrapper 输出一致:
    ``{artifact_id, tool_excute_result, summary, isError}``。
    """

    model_config = ConfigDict(extra="allow")

    tool: str = Field(..., description="MCP tool name,形如 plan_timeline_pro")
    artifact_id: str
    tool_excute_result: StorylineToolResult = Field(default_factory=StorylineToolResult)
    summary: Optional[str] = None
    isError: bool = False


# ---------------------------------------------------------------------------
# 错误码(ADR-007)
# ---------------------------------------------------------------------------
class StorylineErrorCode:
    """6 类错误码,贯穿 OpenStorylineMCPClient / node_02 / node_04 / node_05。"""

    PROCESS_START_FAILED = "PROCESS_START_FAILED"
    MCP_CONNECT_FAILED = "MCP_CONNECT_FAILED"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    TOOL_EXECUTION_TIMEOUT = "TOOL_EXECUTION_TIMEOUT"
    CONTRACT_INVALID = "CONTRACT_INVALID"


class ContractInvalid(Exception):
    """OpenStoryline 产出的数据不满足 CanonicalTimeline / StorylinePlan 约束时抛出。

    2026-09 迁移解耦:原 ``mcp_clients.openstoryline_client.ContractInvalid``
    移到这里,供 node_05 mapper / node_04 plan_reader 使用,与 MCP SDK 彻底解耦。
    携带 ``error_code=StorylineErrorCode.CONTRACT_INVALID`` 用于 graph 路由决策。
    """

    def __init__(self, message: str, **ctx: Any) -> None:
        super().__init__(message)
        self.error_code = StorylineErrorCode.CONTRACT_INVALID
        self.ctx = ctx


__all__ = [
    "SourceMedia",
    "Clip",
    "TimelineAudio",
    "TimelineSubtitles",
    "TimelineOptions",
    "CanonicalTimeline",
    "GroupArtifact",
    "StorylineTimeline",
    "StorylineToolResult",
    "StorylinePlan",
    "StorylineErrorCode",
    "ContractInvalid",
]