"""FireRed-OpenStoryline 工具注册表移植层(对照文档,2026-09 解耦版)。

⚠️ 迁移后本文件**仅作文档参考**,不参与 graph 运行时调用。

- 不再有运行时调用方(:class:`OpenStorylineMCPClient` 已删除,见
  ``mcp_clients/openstoryline_client.py`` 的删除记录)。
- 仅供人工核对「openstoryline/src/open_storyline/nodes/core_nodes/ 下节点接口」
  与 ``TOOL_REGISTRY`` 是否一致;新工具上下线时手工同步这里。
- 真正部署时以 ``openstoryline/agent_fastapi.py`` 运行时 ``list_tools()`` 输出为准
  (Phase 0 锁定必需工具清单已固化)。

模块用途(历史):
- 把 FireRed 的 MCP 工具清单(node_name / NodeMeta.name / require_prior_kind / 默认
  入参 schema)以**纯数据**形式搬进 auto-video-editor,允许主项目在不引入
  ``open_storyline`` Python 包的情况下做能力探测与契约编排。
- 提供 :class:`ToolSpec` + :data:`TOOL_REGISTRY` 给 :class:`OpenStorylineMCPClient`
  在 ``__aenter__`` 阶段做 ``list_tools()`` 必备 capability 校验。

事实源(2026-09-17 源码核验 ``E:\\\\Documents\\\\kuaishou\\\\FireRed-OpenStoryline``):
- ``src/open_storyline/mcp/register_tools.py``(wrapper + register)
- ``src/open_storyline/nodes/core_nodes/*.py``(每个 Node 的 ``meta = NodeMeta(...)``)
- ``config.toml`` ``available_nodes``(白名单)

Phase 0 警告:**真正部署时仍以 ``list_tools()`` 运行时输出为准**,本表是降级基线,
若上游重命名/上下线,Phase 4 用 ``tests/integration/test_storyline_tools_snapshot.py``
做快照比对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ToolSpec:
    """单个 FireRed MCP 工具的静态描述。

    Attributes:
        name: MCP tool name(snake_case),与 ``NodeMeta.name`` 对齐。
        node_class: FireRed 节点类名(参考用,不参与 MCP 调用)。
        node_kind: ``NodeMeta.node_kind``,用于 require_prior_kind 比对。
        require_prior_kind: 必须先完成的节点 kind 列表(参考 ADR-004)。
        description: 工具说明,来自 ``NodeMeta.description``。
        input_keys: 已知入参 key 集合(由 ``input_schema`` 的 Field 推导)。
    """

    name: str
    node_class: str
    node_kind: str
    require_prior_kind: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""
    input_keys: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# 工具注册表(2026-09-17 源码核验版;21 个 Node + 2 个内置)
# ---------------------------------------------------------------------------
TOOL_REGISTRY: dict[str, ToolSpec] = {
    # ============== 媒体索引 / 检索 ==============
    "load_media": ToolSpec(
        name="load_media",
        node_class="LoadMediaNode",
        node_kind="load_media",
        require_prior_kind=(),
        description="Loads and indexes input media. Entry point with no dependencies.",
        input_keys=("artifact_id", "inputs"),
    ),
    "search_media": ToolSpec(
        name="search_media",
        node_class="SearchMediaNode",
        node_kind="search_media",
        require_prior_kind=(),
        description="Search Pexels for stock video / image material.",
        input_keys=("artifact_id", "query", "count"),
    ),
    "search_web_topic": ToolSpec(
        name="search_web_topic",
        node_class="SearchWebTopicNode",
        node_kind="search_web_topic",
        require_prior_kind=(),
        description="Search topic reference material from xiaohongshu / douyin.",
        input_keys=("artifact_id", "query", "sites"),
    ),

    # ============== 分割 / 理解 / 粗剪 ==============
    "split_shots": ToolSpec(
        name="split_shots",
        node_class="SplitShotsNode",
        node_kind="split_shots",
        require_prior_kind=("load_media",),
        description="TransNetV2 镜头边界检测,输出 split_shots artifact。",
        input_keys=("artifact_id", "media_artifact_id"),
    ),
    "local_asr": ToolSpec(
        name="local_asr",
        node_class="LocalASRNode",
        node_kind="local_asr",
        require_prior_kind=("load_media",),
        description="本地 ASR(FireRedASR2S :8009)生成字幕时间戳。",
        input_keys=("artifact_id", "media_artifact_id"),
    ),
    "speech_rough_cut": ToolSpec(
        name="speech_rough_cut",
        node_class="SpeechRoughCutNode",
        node_kind="speech_rough_cut",
        require_prior_kind=("local_asr",),
        description="基于 ASR 的语音驱动粗剪。",
        input_keys=("artifact_id", "asr_artifact_id"),
    ),
    "understand_clips": ToolSpec(
        name="understand_clips",
        node_class="UnderstandClipsNode",
        node_kind="understand_clips",
        require_prior_kind=("load_media", "split_shots"),
        description="VLM 逐镜头解读语义。",
        input_keys=("artifact_id", "shots_artifact_id"),
    ),
    "filter_clips": ToolSpec(
        name="filter_clips",
        node_class="FilterClipsNode",
        node_kind="filter_clips",
        require_prior_kind=("understand_clips",),
        description="按 criteria 过滤 clips。",
        input_keys=("artifact_id", "understanding_artifact_id", "criteria"),
    ),
    "group_clips": ToolSpec(
        name="group_clips",
        node_class="GroupClipsNode",
        node_kind="group_clips",
        require_prior_kind=("filter_clips",),
        description="把 clips 合并为可叙述段落。",
        input_keys=("artifact_id", "clips_artifact_id"),
    ),

    # ============== 文案 / 配音 / 字体 / BGM ==============
    "generate_script": ToolSpec(
        name="generate_script",
        node_class="GenerateScriptNode",
        node_kind="generate_script",
        require_prior_kind=("understand_clips",),
        description="基于 understand_clips + group_clips 生成文案。",
        input_keys=("artifact_id", "understanding_artifact_id", "style"),
    ),
    "script_template_recommendation": ToolSpec(
        name="script_template_recommendation",
        node_class="ScriptTemplateRecomendation",
        node_kind="script_template_rec",
        require_prior_kind=("generate_script",),
        description="推荐文案模板。",
        input_keys=("artifact_id", "script_artifact_id"),
    ),
    "generate_voiceover": ToolSpec(
        name="generate_voiceover",
        node_class="GenerateVoiceoverNode",
        node_kind="tts",
        require_prior_kind=("generate_script",),
        description="基于 generate_script 生成配音(多 provider)。",
        input_keys=("artifact_id", "script_artifact_id", "voice"),
    ),
    "select_bgm": ToolSpec(
        name="select_bgm",
        node_class="SelectBGMNode",
        node_kind="music_rec",
        require_prior_kind=("understand_clips",),
        description="按情绪/语义挑选 BGM。",
        input_keys=("artifact_id", "understanding_artifact_id", "mood"),
    ),
    "recommend_transition": ToolSpec(
        name="recommend_transition",
        node_class="RecommendTransitionNode",
        node_kind="transition_rec",
        require_prior_kind=("group_clips",),
        description="推荐段间转场。",
        input_keys=("artifact_id", "groups_artifact_id"),
    ),
    "recommend_text": ToolSpec(
        name="recommend_text",
        node_class="RecommendTextNode",
        node_kind="text_rec",
        require_prior_kind=("generate_script",),
        description="推荐字体 / 花字样式。",
        input_keys=("artifact_id", "script_artifact_id"),
    ),

    # ============== 时间线 / 渲染 ==============
    "plan_timeline": ToolSpec(
        name="plan_timeline",
        node_class="PlanTimelineNode",
        node_kind="plan_timeline",
        require_prior_kind=("split_shots", "group_clips"),
        description="基础时间线规划(Week 4 用)。",
        input_keys=("artifact_id",),
    ),
    "plan_timeline_pro": ToolSpec(
        name="plan_timeline_pro",
        node_class="PlanTimelineProNode",
        node_kind="plan_timeline_pro",
        require_prior_kind=("split_shots", "group_clips", "generate_script", "tts", "music_rec"),
        description="Pro 时间线规划(支持 BGM 卡点 + TTS 对齐 + 目标总时长)。",
        input_keys=("artifact_id",),
    ),
    "plan_timeline_ai_transition": ToolSpec(
        name="plan_timeline_ai_transition",
        node_class="PlanTimelineAITransitionNode",
        node_kind="plan_timeline_ai_transition",
        require_prior_kind=("generate_ai_transition", "plan_timeline_pro"),
        description="在 plan_timeline_pro 基础上叠加 AI 转场(默认关闭,ADR-005)。",
        input_keys=("artifact_id",),
    ),
    "generate_ai_transition": ToolSpec(
        name="generate_ai_transition",
        node_class="GenerateAITransitionNode",
        node_kind="generate_ai_transition",
        require_prior_kind=("split_shots",),
        description="调用 AI 视频生成接口(DashScope / Hailuo)做 AI 转场。",
        input_keys=("artifact_id", "prev_shot", "next_shot", "prompt"),
    ),
    "render_video": ToolSpec(
        name="render_video",
        node_class="RenderVideoNode",
        node_kind="render_video",
        require_prior_kind=("load_media", "plan_timeline", "transition_rec", "text_rec"),
        description="MoviePy 端到端渲染(Phase 4 只做 capability probe,不接剪映交付)。",
        input_keys=("artifact_id", "timeline_artifact_id", "output_path"),
    ),

    # ============== 内置工具 ==============
    "read_node_history": ToolSpec(
        name="read_node_history",
        node_class="(builtin)",
        node_kind="builtin",
        require_prior_kind=(),
        description="按 artifact_id 拉回历史执行结果(JSON)。",
        input_keys=("artifact_id", "query_artifact_id"),
    ),
    "write_skills": ToolSpec(
        name="write_skills",
        node_class="(builtin)",
        node_kind="builtin",
        require_prior_kind=(),
        description="保存 Agent Skill(Markdown 格式)到 ``.storyline/skills/<name>/``。",
        input_keys=("skill_name", "skill_dir", "skill_content"),
    ),
}


# ---------------------------------------------------------------------------
# 必需 capability(Phase 0 锁定,运行时缺失即报错)
# ---------------------------------------------------------------------------
# 不含 AI Transition 工具(ADR-005 默认关闭),不含 write_skills(Phase 3 才启用)。
REQUIRED_TOOLS: tuple[str, ...] = (
    "load_media",
    "split_shots",
    "understand_clips",
    "generate_script",
    "plan_timeline_pro",
    "select_bgm",
    "generate_voiceover",
    "render_video",        # Phase 4 才真渲染,Phase 1 仅 capability probe
    "read_node_history",
)

# 可选 capability(缺失不报错,但 Adapter 不可调用)
OPTIONAL_TOOLS: tuple[str, ...] = (
    "generate_ai_transition",
    "plan_timeline_ai_transition",
    "write_skills",
    "search_media",
    "search_web_topic",
    "local_asr",
    "speech_rough_cut",
    "filter_clips",
    "group_clips",
    "script_template_recommendation",
    "recommend_transition",
    "recommend_text",
    "plan_timeline",
)


# ---------------------------------------------------------------------------
# 节点类型 → MCP 工具名 反向索引
# ---------------------------------------------------------------------------
NODE_KIND_TO_TOOL: dict[str, str] = {
    spec.node_kind: spec.name for spec in TOOL_REGISTRY.values()
}

NODE_CLASS_TO_TOOL: dict[str, str] = {
    spec.node_class: spec.name for spec in TOOL_REGISTRY.values()
}


# ---------------------------------------------------------------------------
# 配置 schema 镜像(移植自 FireRed config.toml + config.py)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FireredConfigKeys:
    """FireRed config.toml 必需 Key 校验清单(供 node_02 启动前 fail-fast)。"""

    required_sections: tuple[str, ...] = (
        "developer",
        "project",
        "llm",
        "vlm",
        "local_mcp_server",
        "skills",
        "split_shots",
        "understand_clips",
        "script_template",
        "generate_voiceover",
        "select_bgm",
        "recommend_text",
        "plan_timeline",
        "plan_timeline_pro",
    )
    mcp_keys: tuple[str, ...] = (
        "server_name",
        "server_cache_dir",
        "server_transport",
        "connect_host",
        "port",
        "path",
        "available_node_pkgs",
        "available_nodes",
    )


def list_capability(*, include_ai_transition: bool = False) -> list[str]:
    """返回当前必需 capability 清单。

    Args:
        include_ai_transition: True 时把 ``generate_ai_transition`` /
            ``plan_timeline_ai_transition`` 加入清单;False 时仅返回 ``REQUIRED_TOOLS``
            基线。
    """
    caps = list(REQUIRED_TOOLS)
    if include_ai_transition:
        caps += ["generate_ai_transition", "plan_timeline_ai_transition"]
    return caps


def lookup_tool(tool_name: str) -> Optional[ToolSpec]:
    """按 tool_name 查 :class:`ToolSpec`,找不到返回 None。"""
    return TOOL_REGISTRY.get(tool_name)


def lookup_by_node_kind(node_kind: str) -> Optional[ToolSpec]:
    """按 FireRed 节点 kind(``NodeMeta.node_kind``)反查 MCP tool name。"""
    target = NODE_KIND_TO_TOOL.get(node_kind)
    return TOOL_REGISTRY.get(target) if target else None


__all__ = [
    "ToolSpec",
    "TOOL_REGISTRY",
    "REQUIRED_TOOLS",
    "OPTIONAL_TOOLS",
    "NODE_KIND_TO_TOOL",
    "NODE_CLASS_TO_TOOL",
    "FireredConfigKeys",
    "list_capability",
    "lookup_tool",
    "lookup_by_node_kind",
]