"""LangGraph 工作流状态(对照附件 1.1 节)。

字段命名与 draft_content.json 内部字段(canvas_config / materials.videos /
tracks)不冲突——前者是 LangGraph 状态,后者是剪映草稿文件内部结构,二者层
级不同。

Week 4 改动:
- ``status_log`` / ``error_log`` 加自定义 reducer(append-only + 去重),
  解决 fan-in 时"两分支同时写同一字段"的并发冲突,同时兼容既有节点
  ``{**state, "status_log": [...full_list]}`` 写法。

Week 5 改动:
- 新增 ``en_audio_path`` / ``final_video_path`` / ``heartbeat_id`` 字段
  (Week 5 计划 §1.1)。节点必须 ``.get(key, default)`` 读取,防止旧
  checkpoint resume 时 KeyError。

Phase 5(阶段一/二)改动(对照 docs/integration/video-agent-kit集成
auto-video-editor设计执行计划.md 第六节):
- 新增 ``assembly_*`` 系列字段,贯穿 assembly_discover_and_probe →
  assembly_build_timeline → assembly_validate_render_qc →
  assembly_repair_loop → assembly_write_report 6 节点的产物路径与
  质检状态。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, NotRequired, Optional

from typing_extensions import TypedDict


# 加密状态取值,与 DraftStatus.value 一一对应
EncryptionStatus = Literal["plaintext", "encrypted", "not_found"]

# 写入策略取值,与 VersionStrategy.value 一一对应
StrategyChoice = Literal["strategy_a_version_lock", "strategy_b_oneway_write"]

# 封面比例取值(Week 4 新增)
CoverRatio = Literal["9:16", "16:9", "4:3"]


# ---------------------------------------------------------------------------
# Week 4:append-only reducer,解决 fan-in 时"两分支同时写同一字段"的并发冲突
# ---------------------------------------------------------------------------
def _append_unique(existing: list[str] | None, new: list[str] | None) -> list[str]:
    """Append-only reducer。

    支持两种返回风格(Week 3 既有节点 + Week 4 新节点):
    - 节点返回 delta ``["new_tag"]`` → 直接 append
    - 节点返回 full-list ``[...existing, "new_tag"]`` → 检测前缀,只 append tail

    去重:若 tail 的所有元素已在 existing 中,则不 append(避免双写)。
    """
    existing = list(existing or [])
    new = list(new or [])
    if not new:
        return existing
    # 检测 full-list 模式:new 的前缀等于 existing
    if len(new) >= len(existing) and new[: len(existing)] == existing:
        tail = new[len(existing):]
    else:
        tail = new
    if not tail:
        return existing
    # 去重
    seen = set(existing)
    out = list(existing)
    for item in tail:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


# ---------------------------------------------------------------------------
# Phase 4:``last-wins`` reducer(plan_v4 §5 阶段 0 / LangGraph 并发问题)
# ---------------------------------------------------------------------------
# 多个 storyline 节点可能在同一 tick 写同一 ``storyline_*`` 字段(例如 fan-in 到
# plan_timeline_pro 的多个上游都想 ``append_status_tag``),LastValue channel 默认
# 不允许多次写并抛 ``InvalidUpdateError``。本 reducer 取最后一次写入,沿用 LangGraph
# 标准「默认无 reducer 字段只能写一次」的语义但放宽到「last-wins」。
def _last_wins(existing: Any, new: Any) -> Any:
    """``existing`` / ``new`` 都可能是 None;取最后一次非空值。

    与 Week 4 的 ``_append_unique``(append-only + 去重)不同,本 reducer 不累积,
    符合 Path 字段「最终以最后写入者为准」的语义。
    """
    if new is None:
        return existing
    return new


# ---------------------------------------------------------------------------
# Week 4 新增:字幕段结构(节点 8 写 zh,节点 16 补 en)
# ---------------------------------------------------------------------------
class SubtitleSegment(TypedDict, total=False):
    """一行字幕段。start_ms / end_ms 使用毫秒,便于节点 16 写 SRT。"""

    index: int
    start_ms: int
    end_ms: int
    text_zh: str
    text_en: NotRequired[Optional[str]]


# ---------------------------------------------------------------------------
# Week 4 新增:封面资源结构(节点 14 写 zh_path+text_bbox,节点 15 补 en_path)
# ---------------------------------------------------------------------------
class CoverAsset(TypedDict, total=False):
    ratio: CoverRatio
    zh_path: str
    en_path: NotRequired[Optional[str]]
    text_bbox: NotRequired[dict]  # {"x","y","w","h","font_path","font_size"}


class WorkflowState(TypedDict, total=False):
    # ===== Week 2 已有字段(保留不动)=====
    # 全局
    session_id: str
    video_input_path: str

    # 步骤 1
    cache_cleaned: bool
    cache_cleaned_paths: list[str]

    # 步骤 2
    openstoryline_pid: Optional[int]
    openstoryline_mcp_endpoint: Optional[str]
    openstoryline_web_url: Optional[str]
    openstoryline_ready: bool

    # 步骤 3
    preview_opened: bool

    # 步骤 4
    shot_plan: Optional[dict]

    # 步骤 5(依赖阶段 B 的加密/版本状态)
    draft_path: Optional[str]
    draft_encryption_status: Optional[EncryptionStatus]
    draft_version_strategy: Optional[StrategyChoice]

    # 通用
    # Week 4:加 reducer,允许 fan-in 双分支同时写(append 语义,Week 3 既有测试兼容)
    error_log: Annotated[list[str], _append_unique]

    # ===== Week 3 新增字段(对应原文档 3.2 节)=====
    snapshot2_path: str               # 步骤7产出快照②路径,供后续英文分支 Week 4+ 使用
    reorder_notified: bool            # 关卡①副作用幂等标记(本设计不依赖,见 graph.py 注释)
    bgm_notified: bool                # 关卡②副作用幂等标记(同上)
    retry_counts: dict[str, int]      # 护栏节点重试计数,键如 "node_07"
    # Week 4:加 reducer,允许 fan-in 双分支同时写
    status_log: Annotated[list[str], _append_unique]
    volume_adjusted: bool             # 步骤13 占位节点标记:Week 3 永远 False
    _draft_dir_override: str          # 内部:节点 5 wrapper 用,从 state 读 draft_dir(测试用)

    # ===== Week 4 新增字段(对应原文档 3.1 节)=====
    # 节点 8 把 ASR 结果**同时**写进 state,供节点 16 翻译使用(不依赖 materials.texts)
    asr_segments_zh: list[SubtitleSegment]
    asr_segments_zh_written: bool      # 幂等标记:节点 8 已写过 ASR 结果到 state

    # 英文分支:从 snapshot② 复制独立草稿副本的目录路径
    draft_dir_en_branch: NotRequired[Optional[str]]

    # 封面资产列表(节点 14 写 zh_path+text_bbox,节点 15 补 en_path)
    covers: NotRequired[list[CoverAsset]]

    # 翻译产物(节点 16 写)
    subtitle_segments_en: NotRequired[list[SubtitleSegment]]
    subtitle_srt_path: NotRequired[Optional[str]]

    # 关卡③ 触发标记(节点 16 写)
    checkpoint3_triggered: NotRequired[bool]

    # 英文 TTS 音频路径(节点 17 写,Week 4 固定 None)
    en_dub_audio_path: NotRequired[Optional[str]]

    # 汇合节点 QA 闸门结果(join_before_delivery 写)
    join_qa_issues: NotRequired[list[str]]

    # ===== Week 5 新增字段(对齐第 5 周计划 §1.1 / §4.1)=====
    # 节点 17 写,验收脚本读。Week 5 仍是 stub,产出 en_dub.wav 占位空 wav。
    en_audio_path: NotRequired[Optional[str]]
    # 占位字段 — Week 5 暂不实写,阶段五 acceptance_check.py 读。
    final_video_path: NotRequired[Optional[str]]
    # build_graph() 启动时生成(uuid),各节点需要时读(Week 5 计划 §4.1)。
    heartbeat_id: NotRequired[Optional[str]]

    # ===== Week 5 新增:Layout 检测结果(node_16a 写)=====
    # layout 异常列表(每条 {"index","text","width_px","max_width_px","reason"})
    layout_issues: NotRequired[list[dict]]
    # layout_issues 非空 → 关卡③ 触发
    layout_issues_detected: NotRequired[bool]

    # ===== Phase 1 新增:FireRed-OpenStoryline 集成字段(plan §7.2)=====
    # 全部 NotRequired,旧 checkpoint resume 兼容
    # FireRed session ID(= job_id + "-storyline",由 Adapter 注入 X-Storyline-Session-Id 头)
    storyline_session_id: NotRequired[Optional[str]]
    # MCP 传输方式(开发期 stdio / 生产期 streamable-http)
    storyline_transport: NotRequired[Literal["stdio", "streamable-http"]]
    # list_tools() 缓存的 tool name(防上游静默升级;phase 0 inventory 锁定基线)
    storyline_tools_snapshot: NotRequired[list[str]]
    # Canonical Timeline(经 Pydantic 校验,直接喂 mapper.canonical_to_draft)
    storyline_plan: NotRequired[Optional[dict]]
    # FireRed artifact 索引(只存 id + summary + hash + version,不存完整 JSON)
    storyline_artifacts: NotRequired[list[dict]]
    # 错误码(ADR-007 5 类之一)
    storyline_error_code: NotRequired[Optional[str]]
    # outputs/{job_id}/ 路径(用于幂等键命中后直接读 draft_content.json)
    storyline_outputs_root: NotRequired[Optional[str]]
    # Phase 3 复用 Skill(默认空)
    reuse_skill_name: NotRequired[Optional[str]]

    # ===== Phase 4 新增:auto-mode 19 节点图字段(plan_v4 §3.2)=====
    # 全部 NotRequired,旧 checkpoint resume 兼容;统一通过 state.get() 读以防
    # 老 checkpoint 缺字段。状态日志统一用 status_log / error_log(Week 4 reducer
    # 已支持 fan-in 去重),不允许 storyline_status_log / storyline_error_log。
    # 注:本计划与上面 storyline_plan / storyline_artifacts / storyline_error_code
    # / storyline_outputs_root 已在 Phase 1 落地,本阶段不在此列重复定义。
    # ``Annotated[..., _last_wins]`` 让 fan-in(plan_timeline_pro 5 个上游)能
    # 在同一 tick 写入同一字段而不抛 ``InvalidUpdateError``,取最后一次非空值。
    # TypedDict(total=False) 把所有键视为 Optional(NotRequired 语义);带
    # ``_last_wins`` reducer 的字段允许多源并发写,不带的仍走 LastValue 默认
    # (例如 ``storyline_qa_retry_count`` 是 qa_gate 唯一写入者)。
    storyline_media_artifact: Annotated[Optional[str], _last_wins]            # load_media
    storyline_shots_artifact: Annotated[Optional[str], _last_wins]            # split_shots
    storyline_understanding_artifact: Annotated[Optional[str], _last_wins]    # understand_clips
    storyline_filtered_clips: Annotated[Optional[str], _last_wins]            # filter_clips
    storyline_groups_artifact: Annotated[Optional[str], _last_wins]           # group_clips
    storyline_script_artifact: Annotated[Optional[str], _last_wins]           # generate_script
    storyline_voiceover_artifact: Annotated[Optional[str], _last_wins]        # generate_voiceover
    storyline_bgm_selection: Annotated[Optional[dict], _last_wins]            # select_bgm
    storyline_transition_plan: Annotated[Optional[dict], _last_wins]          # recommend_transition
    storyline_text_style_plan: Annotated[Optional[dict], _last_wins]          # recommend_text
    storyline_timeline_plan: Annotated[Optional[str], _last_wins]             # plan_timeline_pro / join
    storyline_asr_artifact: Annotated[Optional[str], _last_wins]              # local_asr(条件)
    storyline_rough_cut_artifact: Annotated[Optional[str], _last_wins]        # speech_rough_cut(条件)
    storyline_ai_transition_artifact: Annotated[Optional[str], _last_wins]    # generate_ai_transition(ADR-005 默认关闭)
    storyline_web_topic_artifact: Annotated[Optional[str], _last_wins]        # search_web_topic(条件)
    storyline_render_smoke_test_path: Annotated[Optional[str], _last_wins]    # render_video(旁支)
    storyline_qa_retry_count: NotRequired[int]            # qa_gate 唯一写入者(无需 reducer)
    # storyline_targets:{ target_duration_ms, ratio, ... },qa_gate 用
    storyline_targets: NotRequired[Optional[dict]]

    # ===== Phase 5 新增:assembly QC 通道(video-agent-kit 移植,ADR-1~5) =====
    # 产物统一落 outputs/<job_id>/assembly/ 下(plan §6),命名照抄 video-edit-assembly
    # 文件契约。所有字段用 _last_wins(同 storyline_*),原因同 §Phase 4 注解。
    # ``assembly_qc_retry_count`` 是 ``assembly_repair_loop`` 唯一写入者,无需 reducer。
    assembly_media_artifact: Annotated[Optional[str], _last_wins]            # inspect_media/analyze_media 汇总结果
    assembly_transcript_artifact: Annotated[Optional[str], _last_wins]       # speech_transcribe 结果
    assembly_ingest_artifact: Annotated[Optional[str], _last_wins]           # video_ingest 结果(contact sheet)
    assembly_timeline_path: Annotated[Optional[str], _last_wins]             # 组装出的 timeline.json(plan §第七节 7.3)
    assembly_timeline_validation_path: Annotated[Optional[str], _last_wins]  # validate_timeline 输出
    assembly_preview_path: Annotated[Optional[str], _last_wins]              # render_preview 输出的 mp4 路径
    assembly_qc_report_path: Annotated[Optional[str], _last_wins]            # qc_preview 输出
    assembly_report_path: Annotated[Optional[str], _last_wins]               # 最终 report.md
    assembly_qc_status: Annotated[Optional[str], _last_wins]                 # "pass" | "pass_with_warnings" | "escalated"
    assembly_qc_retry_count: NotRequired[int]                                # 修复循环重试计数
