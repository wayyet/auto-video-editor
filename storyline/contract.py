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
7. 逐片段速度 ``源跨度/目标跨度 >= STORYLINE_MIN_CLIP_SPEED``(2026-11-11
   模糊修复阶段3;speed 由区间推导不落存储,短素材拉长冒充画面判契约违例)
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
    # 2026-10-20 修 D9:生产端 plan_timeline_pro 本来就带这四个字段,
    # 但契约没声明 → extra="forbid" 直接判 extra_forbidden,草稿永远生成不出来。
    # 显式声明成可选字段,既保住契约的严格性(未知字段仍然拒绝),
    # 又不丢生产端已经产出的信息。
    voiceover_id: Optional[str] = Field(None, description="该片段所属配音 ID(剪映配音轨映射)")
    transition_in: Optional[str] = Field(None, description="入场转场(fade_in / cut 等)")
    transition_out: Optional[str] = Field(None, description="出场转场(fade_out / cut 等)")
    subtitle_style: Optional[dict[str, Any]] = Field(
        None, description="该片段的字幕样式(recommend_text 产出)"
    )
    # 2026-11-12 noop 治理阶段2b:AI 转场合成片段的溯源标记
    # ("{from_clip_id}->{to_clip_id}");普通片段为 null。mapper 按普通视频
    # 片段处理本字段,仅 QA/诊断消费。
    ai_transition_pair: Optional[str] = Field(
        None, description="AI 转场片段溯源:from_clip_id->to_clip_id(合成片段专用)"
    )

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


class VoiceoverEntry(BaseModel):
    """单段配音的结构化记录(2026-11-09 阶段 2,效果缺失修复计划)。

    取代"把 group_id→voiceover_id 的 JSON 字符串当文件 ref"的旧口径:
    mapper/native 转换按这里的真实 ``path`` 产出可播放的配音素材。
    """

    model_config = ConfigDict(extra="forbid")

    voiceover_id: str = Field(..., description="配音 ID,形如 voiceover_0001")
    group_id: str = Field(..., description="所属分组 ID,形如 group_0001")
    path: str = Field("", description="WAV 文件本机路径;空串=未产出(失败/空旁白)")
    duration_ms: int = Field(0, ge=0, description="音频时长(整数毫秒;WAV 头口径)")
    provider: str = Field("stub", description="合成方:stub / firered 等")
    stub: bool = Field(False, description="是否静音占位(StubTTSClient 产物)")
    error: Optional[str] = Field(None, description="合成失败原因(成功为 null)")


class TimelineAudio(BaseModel):
    """音频轨(BGM + 配音)。可选,允许 null。"""

    model_config = ConfigDict(extra="forbid")

    bgm_ref: Optional[str] = Field(None, description="BGM 资源 ID,形如 bgm_0001")
    voiceover: Optional[str] = Field(
        None, description="配音资源 ID,形如 voiceover_0001"
    )
    # 阶段 2(2026-11-09):结构化配音记录(真实 WAV path/时长/provider);
    # 旧 ``voiceover`` JSON 字符串保留作联调快照,mapper 不再把它当文件 ref。
    voiceovers: Optional[list[VoiceoverEntry]] = Field(
        None, description="逐段配音记录(真实 WAV 路径;阶段 2 起 mapper 消费)"
    )


class TimelineSubtitles(BaseModel):
    """字幕文本。中文必填,英文 Phase 2 由翻译节点补。"""

    model_config = ConfigDict(extra="forbid")

    zh: Optional[str] = Field(None, description="中文字幕文本")
    en: Optional[str] = Field(None, description="英文字幕文本(Phase 2 由翻译节点补)")


class TimelineOptions(BaseModel):
    """时间轴选项。

    ``enable_ai_transition`` 表示时间线里是否已挂上**真实**(非 stub)AI 转场,默认 false;
    它**不是**环境开关 ``STORYLINE_ENABLE_AI_TRANSITION``(那个只决定图要不要走 AI 转场节点)。
    """

    model_config = ConfigDict(extra="forbid")

    enable_ai_transition: bool = Field(
        False, description="是否已挂上真实(非 stub)AI 转场;默认 false"
    )
    enable_voiceover: bool = Field(True, description="是否生成配音;为 false 时跳过 TTS")
    max_duration_ms: Optional[int] = Field(
        None, ge=0, description="目标总时长上限(整数毫秒),null/0 表示不限"
    )


class AITransitionItem(BaseModel):
    """AI 转场清单条目(``storyline_generate_ai_transition`` 产出)。

    2026-10 随"不可达节点接入"新增。2026-11-12 noop 治理阶段2 起:
    真实生成条目带 ``video_path``(MP4 实测产物)与 ``status``
    (generated / degraded / skipped_* / awaiting_approval / provider_unconfigured),
    ``is_stub=True`` 只出现在历史 stub 数据里(现役节点不再产生)。
    ``plan_timeline_ai_transition`` 只物化 ``is_stub=False 且 video_path 非空且
    status=generated`` 的条目;其余保留作审计。
    """

    model_config = ConfigDict(extra="forbid")

    from_clip_id: str = Field(..., description="前一个 clip 的 clip_id")
    to_clip_id: str = Field(..., description="后一个 clip 的 clip_id")
    duration_ms: Annotated[int, Field(ge=0, description="转场时长(整数毫秒)")]
    provider: str = Field("stub", description="生成方:stub / 真实客户端名")
    is_stub: bool = Field(True, description="是否占位结果(现役节点不再产生 stub)")
    error: Optional[str] = Field(None, description="生成失败原因(成功为 null)")
    artifact: Optional[str] = Field(None, description="转场产物文件路径(审计)")
    # 2026-11-12 阶段2:真实生成产物与状态(additive,向后兼容)
    video_path: Optional[str] = Field(
        None, description="生成的 MP4 实测产物路径;未生成/降级时为 null"
    )
    status: Optional[str] = Field(
        None,
        description="条目状态:generated / degraded / skipped_* / awaiting_approval / provider_unconfigured",
    )
    # 以下为生成审计字段(ffprobe 实测/溯源;跳过类条目为 null)
    width: Optional[int] = Field(None, ge=0, description="产物宽度(px,实测)")
    height: Optional[int] = Field(None, ge=0, description="产物高度(px,实测)")
    fps: Optional[float] = Field(None, ge=0.0, description="产物帧率(实测)")
    sha256: Optional[str] = Field(None, description="产物文件 sha256(实测)")
    request_id: Optional[str] = Field(None, description="Provider 侧任务 ID(溯源)")
    cache_hit: Optional[bool] = Field(
        None, description="是否命中生成缓存(qa 重试防重复付费的审计证据)"
    )
    prompt_method: Optional[str] = Field(
        None, description="提示词生成方式:text_llm / deterministic_fallback"
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
    # 2026-10-20 修 D9:producer 记录的排期方法与告警,节点会读它们写
    # status_log。显式声明,不再靠 extra 透传。
    method: Optional[str] = Field(None, description="时间线分配方法名(tts_align / empty 等)")
    warnings: list[str] = Field(default_factory=list, description="producer 产出的告警列表")
    # 2026-10:storyline_plan_timeline_ai_transition 挂上的 AI 转场清单(默认空;mapper 不读)。
    ai_transitions: list[AITransitionItem] = Field(
        default_factory=list, description="AI 转场清单(默认空;mapper 暂不消费)"
    )

    @model_validator(mode="after")
    def _validate_timeline_invariants(self) -> "CanonicalTimeline":
        media_by_id = {s.media_id: s for s in self.source_media}
        if not self.clips:
            # 空时间轴允许(用于仅素材导入场景),但 source_media 必须非空
            if not self.source_media:
                raise ValueError("CanonicalTimeline: both source_media and clips empty")
            return self

        # 2026-11-11 模糊修复阶段3:逐片段速度不变量(不新增存储字段,speed 由
        # source/target 推导,唯一推导规则见 §4.4)。短素材被拉长冒充有效画面
        # (speed 低于 STORYLINE_MIN_CLIP_SPEED,实机事故:0.883s → 11.593s)
        # 直接判契约违例 → node_05/qa_gate/resume 校验统一 fail-closed。
        from config import STORYLINE_MIN_CLIP_SPEED

        min_speed = float(STORYLINE_MIN_CLIP_SPEED)
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
            # 4. 逐片段速度下限(CLIP_SPEED_INVALID)
            if min_speed > 0:
                target_ms = clip.timeline_out_ms - clip.timeline_in_ms
                source_ms = clip.source_out_ms - clip.source_in_ms
                derived_speed = source_ms / target_ms if target_ms > 0 else 0.0
                if derived_speed < min_speed - 1e-6:
                    raise ValueError(
                        f"clip {clip.clip_id}: 源跨度 {source_ms}ms 拉长到 {target_ms}ms"
                        f"(speed={derived_speed:.4f})低于最低速度阈值 {min_speed}"
                        "(CLIP_SPEED_INVALID:禁止停帧/注水冒充有效画面;"
                        "请由 planner 重分配、更换素材或缩短目标时长)"
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
    携带 ``error_code=StorylineErrorCode.CONTRACT_INVALID``;qa_gate 捕获后把它
    写入 ``state["storyline_error_code"]`` 作**审计留痕**(2026-11-12 noop 治理
    阶段5批0 修正:graph 路由实际以 status_log / storyline_qa_route_signal 为准,
    原文"用于 graph 路由决策"为注释失真)。
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
    "AITransitionItem",
    "CanonicalTimeline",
    "GroupArtifact",
    "StorylineTimeline",
    "StorylineToolResult",
    "StorylinePlan",
    "StorylineErrorCode",
    "ContractInvalid",
]