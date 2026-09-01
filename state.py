"""LangGraph 工作流状态(对照附件 1.1 节)。

字段命名与 draft_content.json 内部字段(canvas_config / materials.videos /
tracks)不冲突——前者是 LangGraph 状态,后者是剪映草稿文件内部结构,二者层
级不同。
"""

from __future__ import annotations

from typing import Literal, Optional

from typing_extensions import TypedDict


# 加密状态取值,与 DraftStatus.value 一一对应
EncryptionStatus = Literal["plaintext", "encrypted", "not_found"]

# 写入策略取值,与 VersionStrategy.value 一一对应
StrategyChoice = Literal["strategy_a_version_lock", "strategy_b_oneway_write"]


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
    error_log: list[str]

    # ===== Week 3 新增字段(对应原文档 3.2 节)=====
    snapshot2_path: str               # 步骤7产出快照②路径,供后续英文分支 Week 4+ 使用
    reorder_notified: bool            # 关卡①副作用幂等标记(本设计不依赖,见 graph.py 注释)
    bgm_notified: bool                # 关卡②副作用幂等标记(同上)
    retry_counts: dict[str, int]      # 护栏节点重试计数,键如 "node_07"
    status_log: list[str]             # 各节点完成打点,联调测试校验重跑
    volume_adjusted: bool             # 步骤13 占位节点标记:Week 3 永远 False
    snapshot2_path: str               # (重复声明,保留兼容 Week 4 引用)
    _draft_dir_override: str          # 内部:节点 5 wrapper 用,从 state 读 draft_dir(测试用)
