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
"""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired, Optional

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
